"""User-owned model configuration. Never copy credentials from the old agent."""
from dataclasses import dataclass, field
import importlib.util
import os
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3.8-flash"
    timeout: float = 60
    max_tokens: int = 3000
    thinking: bool = False
    max_evidence_chars: int = 40000

    def validate(self, demo=False):
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型入口必须是无用户名、密码、查询参数的HTTPS根地址。")
        if not self.model.strip() or not 1 <= self.timeout <= 180 or not 128 <= self.max_tokens <= 8192:
            raise ValueError("模型名、超时或输出上限无效。")
        if self.thinking:
            raise ValueError("本版仅验证非思考模式，请保持THINKING_ENABLED=False。")
        if not demo and (not self.api_key.strip() or self.api_key.startswith(("YOUR_", "<"))):
            raise ValueError("请在settings.py填写API_KEY或设置RELEASE_API_KEY；离线演示使用--demo。")


def load_settings() -> Settings:
    values = {}
    path = PROJECT_ROOT / "settings.py"
    if path.exists():
        spec = importlib.util.spec_from_file_location("release_guard_user_settings", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        values = vars(module)
    base = os.environ.get("RELEASE_BASE_URL", values.get("BASE_URL", Settings.base_url)).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")]
    return Settings(
        api_key=os.environ.get("RELEASE_API_KEY", values.get("API_KEY", "")).strip(),
        base_url=base, model=os.environ.get("RELEASE_MODEL", values.get("MODEL", Settings.model)).strip(),
        timeout=float(values.get("REQUEST_TIMEOUT_SECONDS", 60)),
        max_tokens=int(values.get("MAX_OUTPUT_TOKENS", 3000)),
        thinking=values.get("THINKING_ENABLED", False),
    )
