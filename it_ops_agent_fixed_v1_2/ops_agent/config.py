"""应用配置，不在日志、报告或轨迹中记录密钥。"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.util
import types
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _is_qwen_host(host: str) -> bool:
    return host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
    }


def normalize_base_url(value: str) -> str:
    """补齐已知百炼入口；不猜测任意字符串，也不把HTTP改为HTTPS。"""
    if not isinstance(value, str):
        raise ValueError("DEEPSEEK_BASE_URL 必须是字符串。")
    value = value.strip().rstrip("/")
    if "://" not in value:
        # 仅自动修正官方模型主机，防止将误填的Key视作一个网络地址。
        host = value.split("/", 1)[0].lower()
        if re.fullmatch(r"[a-z0-9.-]+", host) and _is_qwen_host(host):
            value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        raise ValueError("DEEPSEEK_BASE_URL 格式无效；请填写HTTPS接口根地址。") from None
    if parsed.scheme == "https" and _is_qwen_host(host) and not parsed.path:
        value += "/compatible-mode/v1"
    # 可粘贴完整Chat Completions地址，但内部统一保存接口根地址。
    if value.endswith("/chat/completions"):
        value = value[:-len("/chat/completions")]
    return value


@dataclass(frozen=True)
class ClientConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    thinking: bool = False
    reasoning_effort: str = "high"
    timeout: float = 60
    max_tokens: int = 4096
    provider: str = "auto"

    def __post_init__(self):
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))

    @property
    def resolved_provider(self) -> str:
        if self.provider != "auto":
            return self.provider
        host = (urlsplit(self.base_url).hostname or "").lower()
        if _is_qwen_host(host):
            return "qwen"
        if host == "api.deepseek.com":
            return "deepseek"
        if self.model.lower().startswith("qwen"):
            return "qwen"
        return "deepseek" if self.model.lower().startswith("deepseek") else "openai"

    @property
    def provider_label(self) -> str:
        return {"qwen": "Qwen/阿里云百炼", "deepseek": "DeepSeek", "openai": "兼容API"}[self.resolved_provider]

    def validate(self) -> None:
        if not self.api_key.strip() or self.api_key.startswith(("YOUR_", "<")):
            raise ValueError("请先在 settings.py 填写 DEEPSEEK_API_KEY，或设置同名环境变量；离线体验请使用 --demo。")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("DEEPSEEK_BASE_URL 必须是完整且无用户名/密码的HTTPS地址，例如 https://工作空间域名/compatible-mode/v1；API Key应填在DEEPSEEK_API_KEY。")
        if parsed.query or parsed.fragment:
            raise ValueError("DEEPSEEK_BASE_URL 不允许 query 或 fragment。")
        if not self.model.strip() or self.timeout <= 0 or not 1 <= self.max_tokens <= 32768:
            raise ValueError("模型名、超时或 MAX_OUTPUT_TOKENS 配置无效。")
        if self.provider not in {"auto", "deepseek", "qwen", "openai"}:
            raise ValueError("API_PROVIDER 仅支持 auto/deepseek/qwen/openai。")
        allowed = {"low", "medium", "high", "xhigh", "max"} if self.resolved_provider == "qwen" else {"low", "high", "max"}
        if self.reasoning_effort not in allowed:
            raise ValueError("当前服务不支持此REASONING_EFFORT配置。")


def _load_settings():
    """Load trusted operator configuration lazily; demo/import never loads secrets.

    OPS_SETTINGS_FILE is a local deployment environment setting, not a model,
    web request or tool argument. The file is executed as trusted Python config.
    """
    configured = os.environ.get("OPS_SETTINGS_FILE", "").strip()
    if not configured:
        return importlib.import_module("settings")
    path = Path(configured)
    if not path.is_absolute() or path.suffix.lower() != ".py" or not path.is_file():
        raise ValueError("OPS_SETTINGS_FILE 必须指向可信的本地绝对路径Python配置文件。")
    try:
        source = path.read_bytes()
        if len(source) > 262144:
            raise ValueError()
        module = types.ModuleType("_ops_operator_settings")
        module.__file__ = str(path.resolve())
        # Do not create a bytecode copy containing secrets alongside external config.
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        return module
    except Exception:
        raise ValueError("OPS_SETTINGS_FILE 配置加载失败；未输出配置内容或密钥。") from None


def load_client_config() -> ClientConfig:
    settings = _load_settings()
    return ClientConfig(
        api_key=os.environ.get("DEEPSEEK_API_KEY", settings.DEEPSEEK_API_KEY).strip(),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", settings.DEEPSEEK_BASE_URL).rstrip("/"),
        model=os.environ.get("DEEPSEEK_MODEL", settings.DEEPSEEK_MODEL).strip(),
        thinking=settings.THINKING_ENABLED,
        reasoning_effort=settings.REASONING_EFFORT,
        timeout=settings.REQUEST_TIMEOUT_SECONDS,
        max_tokens=settings.MAX_OUTPUT_TOKENS,
        provider=os.environ.get("API_PROVIDER", getattr(settings, "API_PROVIDER", "auto")).strip().lower(),
    )
