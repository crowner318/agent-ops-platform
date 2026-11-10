# IT故障诊断Agent 1.2

2026-11-06更新：统一网页支持完整模拟场景与用户自填资料。CLI演示：`python run.py --demo --data-dir data/demo_complete --mode single`（或swarm）。自定义服务/环境的本地资料由操作者加`--local-input`精确绑定，网页自动处理；工具权限和预算保留，默认范围不放宽。原始资料保留作缺项对照，完整演示含38条当前记录；用户自供材料另标记未核实。全套155项测试通过。网页使用本目录修复版代码，D盘原源码未修改。见旁边的 `演示修复说明.md`。

支持 **qwen3.8-flash** 与 DeepSeek Chat Completions；保持原有配置变量和SQLite数据结构。

本版修复真实Qwen运行暴露的文本工具计划、JSON说明前缀、顶层数组超限和Swarm职责重复问题。它仍是读取本地数据的原型，默认模拟数据不代表真实生产故障。

## 快速运行

需要Python 3.10+：

```powershell
python -m pip install -r requirements.txt
python run.py --demo
```

模型模式在 `settings.py` 填写自己的Key和入口：

```python
DEEPSEEK_API_KEY = "你的阿里云百炼Key"
DEEPSEEK_BASE_URL = "https://你的工作空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_MODEL = "qwen3.8-flash"
API_PROVIDER = "auto"
THINKING_ENABLED = False
```

变量名 `DEEPSEEK_*` 保留以兼容旧版本，同样适用于Qwen。阿里云裸工作空间主机将补齐HTTPS和接口路径；Qwen使用 `enable_thinking`，DeepSeek官方使用 `thinking`。环境变量覆盖文件配置，地域/工作空间的入口与Key须对应。

```powershell
python run.py --check-config  # 不联网、不显示Key
python run.py                 # 默认简单单服务任务走single
python run.py --mode swarm    # 显式启用3个Agent协作
```

分发包中Key为空；升级已有项目应保留自己的 `settings.py`、`data/` 和 `runtime/`。

## 本版执行过程

1. 程序在固定服务、环境与时间窗内批量预取指标、日志、链路和变更；这是程序数据读取，**不是伪造模型工具调用**。
2. 模型直接使用共享证据，需要时补查Runbook或缺失项。基础取证没有当前遥测时不调用模型。
3. 简单单服务问题由diagnosis完成候选、验证、风险评估与复盘；“复盘”一词不会单独触发Swarm。
4. 显式Swarm或复杂关键词走并行研判与诊断，然后汇总。当前每次运行仍只绑定一个服务；复杂度路由不会自动授权其他服务。
5. Swarm诊断仅取证和提出候选，硬性禁止风险评估和复盘工具；这两个工具仅供后续汇总Agent使用。汇总Agent不得重复取证。
6. 最终模型仅返回候选、建议、缺失证据和流程核查JSON，事实与时间覆盖由程序从观测记录生成。

当前8个工具：`collect_evidence`（批量），以及原7个 `query_metrics`、`search_logs`、`analyze_traces`、`get_changes`、`search_runbooks`、`assess_remediation`、`build_postmortem`。

`collect_evidence` 的 `kinds` 可选 metrics/logs/traces/changes/runbooks；triage只能批量采集metrics/logs。独立查询可在同轮批量提出，避免一个指标一轮模型请求。

## 缓存和上下文

- 缓存仅覆盖本次registry绑定的数据快照，不跨会话、服务、环境或时间窗复用。
- 同工具与等价默认参数归一化，例如 `query_metrics({})` 与 `query_metrics(metric="all")`。
- 并发相同查询使用singleflight，叶子工具只执行一次；不同查询可并行；失败不会永久缓存。
- `cache_hit`、`duration_ms` 和 `cache_stats()` 区分缓存请求和实际执行。
- 同一证据正文在工具协议中只首次发送，后续响应引用ID；最终报告保留canonical记录。
- Swarm汇总传递结构化专家摘要，不重复拼接完整事实表。历史案例与实践也有文本长度限制。

这是本地固定快照缓存。以后接入实时监控时，应定义新鲜度、失效与重查策略，不能直接把本次快照语义当成持续实时监控。

## 报告约束

模型最终JSON包含四个必需键：

```json
{
  "hypotheses": [{"text": "待验证机制", "evidence_ids": ["实际取得的ID"], "validation": ["验证步骤"]}],
  "recommendations": [{"text": "待评审建议", "evidence_ids": []}],
  "missing_evidence": ["缺失数据"],
  "process_checks": ["待核查流程问题"]
}
```

程序生成事实段，保留原始值、单位、采样时间和来源ID，并显示最后观测时间。点错误率不能推算整体窗口可用性；查询截止时间不能当作故障持续终点。

严格检查JSON结构、未知证据、候选缺少当前证据，以及已定义的过度推断模式，如无依据确认根因/恢复、排除泄漏、断言流程缺失和未校准置信度。违规项从正文移除，并记录 `quality_issues`，运行标记需要复核。自由Markdown最终答复也不会直接作为已通过的报告。

流程问题仅以待核查项展示，恢复阈值仅为待评审建议。规则检查**不是通用语义证明**，不能保证发现所有错误或证明因果关系。

1.2的兼容处理：

- 优先原生`tool_calls`。若模型仅在content中返回完整`[{"id":"...","name":"...","args":{...}}]`计划，运行时严格校验整个数组，再经过与原生调用相同的工具白名单、角色权限、参数和预算检查。只接受本轮模型输出；不会执行日志/Runbook中的计划文本，更不会执行任意命令。
- 保留原始assistant消息；文本计划执行结果通过`user.runtime_tool_observations`传回，不伪造供应商原生工具调用历史。
- 只允许去除单个JSON对象外的普通说明文字或单层json围栏。重复键、NaN、多段JSON、代码包装、数组中的对象仍拒绝。
- 四个顶层数组超过schema数量时，先验证全部元素结构，再按模型顺序保留前N项；记录`top_level_array_limited`和`dropped_count`。不会截短字符串、补造字段或放宽证据检查。
- 结构或模型表述未通过检查时，每Agent最多请求一次最终JSON纠正（格式与质量共用一次机会），占用原6步/180秒预算；这一步不开放工具。模型收到问题代码和位置，修订后仍须重新通过全部检查；失败不标记完成。非思考模式客户端启用JSON对象模式，该模式只约束JSON语法，具体schema仍由程序校验。
- `protocol_events`记录兼容与整理，`format_repair_requests`和`quality_repair_requests`分别记录纠正次数。正常整理不计质量失败；最终仍有证据、语义或协议错误则标记未完成。终端显示整体与各阶段状态。

## 使用方法

```powershell
python run.py --demo --mode single
python run.py --demo --mode swarm
python run.py --demo --no-prefetch  # 关闭预取，供执行路径对照；不是重现旧版
python run.py --service payment-service --demo
python run.py --session incident_001 -q "分析订单服务异常并生成复盘"
```

示例数据固定对应 **2026-10-19 09:55–10:20 +08:00**，不会随当天日期变化。其他数据请提供相应时间窗：

```powershell
python run.py --data-dir ./data --service order-service --environment demo --start "2026-10-19T09:55:00+08:00" --end "2026-10-19T10:20:00+08:00"
```

退出码：0=已生成待审核结论，1=启动/配置失败，2=未完成或有质量问题。0不表示根因或恢复已经确认。

## 执行指标

终端、Markdown和JSON均输出执行摘要。`runtime/reports/*.json` 的 `execution` 包含：

| 字段 | 口径 |
|---|---|
| elapsed_ms | 开始处理到保存案例/汇总的墙钟耗时，不含最终报告文件写入 |
| model_steps | 逻辑模型请求，包括失败；并行Agent数量不能直接作为加速倍数 |
| api_attempts | 真实HTTP尝试次数，包含重试；无遥测的自定义客户端返回unknown |
| model_tool_requests | 模型提出的工具调用尝试，包含被拒绝调用；批量工具按一次请求计 |
| format_repair_requests | 有预算时发起的最终JSON结构纠正次数；每Agent最多1次 |
| quality_repair_requests | 有预算时发起的模型表述纠正次数；与格式纠正共用每Agent最多1次预算 |
| protocol_events | 文本计划识别、说明去除、顶层数组限量等记录 |
| stage_status | 每个Agent是否完成及其质量问题数；后续阶段通过不会掩盖上游失败 |
| prefetch_requests | 程序预取批次，与模型调用分开 |
| cache.backend_executions | 叶子工具实际执行次数，不计批量包装层 |
| cache.cache_hits | 叶子缓存命中；单飞和批量重复请求不会重复执行后台查询 |
| usage | API已报告的prompt/completion/total/cached prompt token小计；缺失用null |
| usage_complete | 所有模型调用是否均报告prompt/completion/total三个计数 |
| cache_usage_complete | 所有模型调用是否均报告缓存命中token数 |
| usage_partial_calls | 仅报告部分基本计数的调用数 |
| model_calls | 各步骤耗时、成功状态、重试次数和API用量，不含正文或凭据 |

统计写入SQLite的 `model_call` 事件。离线脚本不发送HTTP，token数为unknown，不能用脚本耗时或步骤数冒充真实模型性能。未配置计费单价，本版不估算费用。

离线确定性路径中，默认single为3模型步骤，显式Swarm为5步骤；这只是协议/流程回归，真实Qwen的步骤、耗时和质量以重新运行所得指标为准。

## 四层记忆与训练数据

L0是YAML身份和约束及Python权限校验；L1是同会话同scope近期问答；L2是人工确认案例的本地词/字符检索；L3是人工从已确认案例沉淀的实践。未实现向量数据库或自动记忆晋升。

```powershell
python run.py --list-cases
python run.py --confirm-case case-实际ID
python run.py --add-practice case-实际ID "已审核的实践内容"
python run.py --export-sft runtime/confirmed_sft.jsonl
```

人工确认只覆盖保存案例时已经存在的轨迹，同会话未来运行仍需再审核。导出排除脚本演示、未确认、未完成或质量检查不通过的轨迹；私有reasoning_content不持久化。人工确认前应核查语义和数据质量，单纯结构校验不等于训练质量。

1.2保留原始模型轨迹用于排查，SFT导出副本的最终回复使用通过检查的规范四键JSON，避免将说明前缀或超限数组作为训练目标。
经历格式或质量纠正的轨迹保留原始失败回复，因此不会自动进入SFT导出；这些轨迹需额外人工整理，避免将失败中间回复作为训练目标。

本项目提供推理和SFT候选轨迹采集，未执行权重微调或RL训练；托管API调用不等于可训练该模型权重。

## 接入与边界

默认读取 `data/demo_data.json`。保留唯一ID、service/environment、带时区timestamp；Runbook有有效期且标记为历史知识。在线适配需参数化查询、只读权限、结果限制及超时，不执行模型任意命令。

`harness.yaml` 规定每Agent最多6个模型步骤、12次模型工具请求和12000字符的结果限制；批量请求内部最多5类叶子读取，实际数量单独统计。Swarm共享180秒截止点，外部阻塞IO仍需各适配器超时，边界检查不等于OS强制终止。

程序没有生产执行器。Key、常见凭据文本递归脱敏，模型私有推理仅在内存协议回传；真实数据仍应按你的规范预先去敏。模型模式会把问题、所需证据和相关历史发送给配置的API。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用假客户端和模拟数据，无需真实Key，覆盖缓存/单飞、批量与权限、Qwen/DeepSeek协议、调用遥测、结构化结论、时间覆盖、过度推断拦截、部分失败证据、记忆隔离和SFT导出。tests/fixtures/qwen_real_responses.json保存两次失败实验中的模拟数据响应，回归验证真实失败形态；这些回放不等于新的在线模型实验。

官方协议：[阿里云兼容入口](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope)、[Qwen结构化输出](https://help.aliyun.com/en/model-studio/qwen-structured-output)、[DeepSeek工具调用](https://api-docs.deepseek.com/guides/tool_calls/)。
