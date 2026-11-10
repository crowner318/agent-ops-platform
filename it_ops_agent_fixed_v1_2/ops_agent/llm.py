"""标准库Chat Completions客户端，适配DeepSeek及Qwen参数。"""
from __future__ import annotations

from copy import deepcopy
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import ClientConfig
from .harness import redact


class LLMError(RuntimeError):
    pass


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不将 Authorization 转发到重定向目标。
        return None


class ChatCompletionClient:
    def __init__(self, config: ClientConfig, opener=None, sleep=time.sleep):
        config.validate()
        self.config = config
        self.opener = opener or build_opener(_RejectRedirect())
        self.sleep = sleep
        self._telemetry: list[dict] = []
        self._telemetry_lock = threading.Lock()

    def telemetry(self) -> list[dict]:
        """Return a thread-safe snapshot with no requests, credentials or reasoning."""
        with self._telemetry_lock:
            return deepcopy(self._telemetry)

    def supports_json_mode(self) -> bool:
        """JSON-object correction is available only without provider thinking."""
        return not self.config.thinking

    @staticmethod
    def _read_usage(raw: object) -> tuple[dict, bool]:
        # API-omitted counts remain unknown. In particular, never derive total or
        # cache counts by subtracting other fields or estimating from the text.
        raw = raw if isinstance(raw, dict) else {}

        def count(value: object) -> int | None:
            return value if type(value) is int and value >= 0 else None

        details = raw.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached = count(raw.get("prompt_cache_hit_tokens"))
        if cached is None:
            cached = count(details.get("cached_tokens"))
        usage = {
            "prompt_tokens": count(raw.get("prompt_tokens")),
            "completion_tokens": count(raw.get("completion_tokens")),
            "total_tokens": count(raw.get("total_tokens")),
            "cached_prompt_tokens": cached,
        }
        return usage, any(value is not None for value in usage.values())

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 tool_choice: str = "auto", timeout: float | None = None,
                 response_format: dict | None = None) -> dict:
        started = time.perf_counter()
        usage, _ = self._read_usage(None)
        record = {
            "model": str(redact(self.config.model)).replace(self.config.api_key, "[REDACTED]"),
            "provider": self.config.resolved_provider,
            "latency_ms": None, "attempts": 0, "success": False,
            "http_status": None, "usage": usage, "usage_reported": False,
            "response_format": "none",
        }
        try:
            if response_format is not None:
                if not isinstance(response_format, dict) or response_format != {"type": "json_object"}:
                    raise ValueError('response_format 仅支持 {"type":"json_object"} 或 None。')
                record["response_format"] = "json_object"
                if not self.supports_json_mode():
                    raise ValueError("当前思考模式配置不支持 JSON 对象模式；请使用非思考配置请求格式纠正。")
                # Keep validated options stable even if the caller later mutates
                # its dictionary. No arbitrary schema or provider options pass.
                response_format = {"type": "json_object"}
            message = self._complete_impl(messages, tools, tool_choice, timeout, record, response_format)
            record["success"] = True
            return message
        finally:
            # A logical completion contributes exactly one record, including
            # exhausted budgets, invalid responses and exceptions. Mutable state
            # is local to this call, so parallel workers cannot swap usage.
            record["latency_ms"] = round(max(0, time.perf_counter() - started) * 1000, 3)
            with self._telemetry_lock:
                self._telemetry.append(record)

    def _complete_impl(self, messages: list[dict], tools: list[dict] | None,
                       tool_choice: str, timeout: float | None, record: dict,
                       response_format: dict | None) -> dict:
        cfg = self.config
        budget = min(cfg.timeout, timeout if timeout is not None else cfg.timeout)
        if budget <= 0:
            raise LLMError("模型请求时间预算已耗尽。")
        deadline = time.monotonic() + budget
        body = {
            "model": cfg.model, "messages": messages, "stream": False,
            "max_tokens": cfg.max_tokens,
        }
        if response_format is not None:
            body["response_format"] = response_format
        provider = cfg.resolved_provider
        if provider == "qwen":
            body["enable_thinking"] = cfg.thinking
            if cfg.thinking and cfg.model.lower().startswith("qwen3.8"):
                body["preserve_thinking"] = True
        elif provider == "deepseek":
            body["thinking"] = {"type": "enabled" if cfg.thinking else "disabled"}
        if cfg.thinking:
            body["reasoning_effort"] = ({"high": "xhigh", "max": "xhigh"}.get(
                cfg.reasoning_effort, cfg.reasoning_effort) if provider == "qwen" else cfg.reasoning_effort)
        else:
            body["temperature"] = 0.2
        if tools:
            body.update(tools=tools, tool_choice=tool_choice)
        request = Request(
            cfg.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + cfg.api_key,
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError("模型请求超时，未完成诊断。")
            try:
                record["attempts"] += 1
                with self.opener.open(request, timeout=remaining) as response:
                    status = getattr(response, "status", None)
                    record["http_status"] = status if type(status) is int else None
                    raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise LLMError("API 响应过大。")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise LLMError("API 响应必须是 JSON 对象。")
                record["usage"], record["usage_reported"] = self._read_usage(payload.get("usage"))
                choice = payload["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise LLMError("模型输出达到 token 上限，回答不完整；可增大 MAX_OUTPUT_TOKENS。")
                if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                    raise LLMError("模型未正常完成输出，不能将部分内容视为完整诊断。")
                original = choice["message"]
                if not isinstance(original, dict):
                    raise LLMError("API message 字段必须是对象。")
                if bool(original.get("tool_calls")) != (choice["finish_reason"] == "tool_calls"):
                    raise LLMError("API 的终止状态与工具调用内容不一致。")
                if original.get("role", "assistant") != "assistant":
                    raise LLMError("API 返回了非 assistant 消息。")
                message = {"role": "assistant", "content": original.get("content")}
                # 该字段仅在内存中的本次工具循环保留；存储层会剔除。
                if cfg.thinking and "reasoning_content" in original:
                    message["reasoning_content"] = original["reasoning_content"]
                if original.get("tool_calls"):
                    message["tool_calls"] = original["tool_calls"]
                if not message.get("content") and not message.get("tool_calls"):
                    raise LLMError("模型返回空内容，未生成诊断。")
                return message
            except HTTPError as exc:
                record["http_status"] = exc.code if type(exc.code) is int else None
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    exc.close()
                    delay = min(2 ** attempt, max(0, deadline - time.monotonic()))
                    self.sleep(delay)
                    continue
                detail = ""
                try:
                    error = json.loads(exc.read(4096)).get("error", {})
                    if isinstance(error, dict):
                        detail = str(redact(str(error.get("message", ""))))[:300]
                except (ValueError, UnicodeError, AttributeError):
                    pass
                finally:
                    exc.close()
                # 不输出 response headers / request，避免凭据出现在错误信息里。
                detail = detail.replace(cfg.api_key, "[REDACTED]")
                tips = {401: "API Key 无效", 402: "账户余额不足", 403: "无访问权限",
                        404: "请检查模型名和 API 地址", 429: "请求受限，请稍后重试"}
                raise LLMError(f"{cfg.provider_label} HTTP {exc.code}：{tips.get(exc.code, '请求失败')}。{detail}") from None
            except (URLError, TimeoutError, OSError) as exc:
                # 网络中断不自动重放：服务端可能已处理并计费。
                raise LLMError(f"无法完成 {cfg.provider_label} 请求（{type(exc).__name__}），请检查网络。") from None
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"{cfg.provider_label} 返回格式无效（{type(exc).__name__}）。") from None
        raise LLMError("重试预算已耗尽。")


# 保留旧导入，现有脚本无需迁移类名。
DeepSeekClient = ChatCompletionClient
