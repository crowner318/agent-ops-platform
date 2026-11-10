"""在这里填写 API Key。也可用同名环境变量覆盖，勿提交填写后的文件。"""

DEEPSEEK_API_KEY = ""  # 填写阿里云百炼API Key；变量名保留以兼容旧配置。
# 若使用工作空间域名，请替换为控制台提供的完整URL，末尾保留 /compatible-mode/v1。
DEEPSEEK_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_MODEL = "qwen3.8-flash"
API_PROVIDER = "auto"  # 自动区分Qwen/DeepSeek，也可显式指定qwen。

# 默认关闭思考以降低演示成本；启用后会在工具循环内完整回传 reasoning_content，
# 但不会显示或持久化该字段。
THINKING_ENABLED = False
REASONING_EFFORT = "high"
REQUEST_TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 4096
