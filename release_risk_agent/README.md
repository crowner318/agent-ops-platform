# 发布风险评审 Agent 1.1

2026-11-06更新：统一网页支持完整模拟场景和用户自填材料，可自行填写服务、环境和变更编号。CLI演示：`python run.py start --demo --data-dir data/demo_complete --change-id safe`。本地数据适配器接受明确布尔来源标记，synthetic=false只表示用户提供、尚未独立核实；不自动连接生产。规则阻断、缺项和人工复核边界保留，原始资料与历史报告未改。离线模式不加载模型密钥配置。全套96项：95项通过、1项stdio权限测试跳过；默认MCP inprocess路径已验收。见旁边的 `演示修复说明.md`。

发布前读取变更、配置、测试与依赖容量，结合操作手册和历史事故生成待复核报告。**MCP、LangChain、LangGraph均实际接入；Redis提供真实客户端实现，默认演示显式使用本地内存协调。**

这是独立的新项目，不修改旧IT运维Agent。它不部署软件、不执行回滚，也不批准生产发布。当前业务材料全部为固定模拟数据；真实模型模式需要你填写新项目密钥。

## 1. 安装

要求Python 3.11+；本次验证环境为Windows、Python 3.14。

```powershell
cd 你的路径\release_risk_agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果已有`uv`工具，也可用`uv venv .venv`创建环境，再使用环境中的Python安装。不要把旧项目填好的settings.py覆盖到这里；新项目配置字段不同。

`requirements.txt`固定直接依赖版本。`requirements-tested-lock.txt`是本次Windows验证环境的完整依赖快照，包含测试依赖和Windows专用包，不能直接当作Linux通用锁文件。

## 2. 不用密钥，先完整跑一次

以下命令都在项目目录执行。为方便使用，可先在PowerShell激活环境：

```powershell
.\.venv\Scripts\Activate.ps1
python run.py --demo --change-id risk-pool --review-id review-001
```

希望直接体验资料完整的新版演示，请显式选择独立材料包。它补齐了发布目的、变更说明、虚构的演示负责人、未执行的回退方案和验证计划，原有负例材料保持不变：

```powershell
python run.py start --demo --data-dir data/demo_complete --change-id safe --review-id complete-safe-001
python run.py resume --data-dir data/demo_complete --review-id complete-safe-001 --decision acknowledge --reviewer 演示复核人 --comment "阅读模拟材料，不构成生产批准"
```

`data/demo_complete/fixtures.json`的全部证据仍为`synthetic=true`。负责人、测试与容量均为明确的演示设定，不是实际业务提交、生产观测或外部CI结果。不要把演示包当成已审批发布材料。`safe`演示应用字段校验及测试版本升级，连接池50→50是有说明的不变基线；`risk-pool`与`legacy-demo`故意保留50→5误配和40对20容量阻断；`missing`故意缺少CI与容量，仍会保持不完整，不能靠填写目的或确认已读掩盖。

网页完整演示可复用同一数据目录和这四个变更编号。演示脚本根据当前资料与规则组织结果：正常材料不硬凑风险，阻断材料明确指出阻断，实际缺失仍会列出；一般加强建议放在流程核查项。真实模型也会收到这些完整字段，但仍需对回复完整校验，不保证模型不会提出进一步建议。

若系统不允许激活脚本，直接用`.\.venv\Scripts\python.exe`代替`python`。

第一次运行应生成报告并显示`waiting_human`，表示等待人工复核。它是流程正常暂停，不是程序卡住。然后执行：

```powershell
python run.py resume --review-id review-001 --decision acknowledge --reviewer 张三 --comment "已阅读风险和证据"
python run.py status --review-id review-001
```

`risk-pool`有数据库连接预算超额阻断，确认后仍为`blocked`，退出码为2，这是预期结果。`acknowledge`只表示已阅读评审，不是同意上线。

其他人工意见：`request_changes`要求修改，`reject`拒绝本报告。每份评审只产生一次最终人工意见；相同意见可重试补齐业务记录，不能借重试改写原意见。材料修改后应创建新review-id。

## 3. 四个演示案例

| change-id | 环境 | 用途 | 预期 |
|---|---|---|---|
| risk-pool | staging | 池上限50→5，8副本总连接需求40，可用预算20 | 产生骤降警告和容量阻断；ack后仍blocked |
| safe | staging | 完整包演示应用校验逻辑与测试升级，池上限不变、CI通过、预算充足 | 无当前规则发现，仍必须人工复核；ack后review_acknowledged |
| missing | staging | 缺少CI与容量材料 | 记录缺材料，模型分析不猜测；ack后incomplete |
| legacy-demo | demo | 与旧项目演示环境同范围，验证历史报告导入 | 可检索人工确认的旧报告；不自动跨环境复用 |

```powershell
python run.py start --demo --change-id safe --review-id review-safe
python run.py resume --review-id review-safe --decision acknowledge --reviewer 张三
python run.py start --demo --change-id missing --review-id review-missing
python run.py list
```

## 4. 切换真实Qwen模型

编辑项目内`settings.py`：

```python
API_KEY = "填写自己的百炼密钥"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = "qwen3.8-flash"
THINKING_ENABLED = False
```

也可使用`RELEASE_API_KEY`、`RELEASE_BASE_URL`、`RELEASE_MODEL`环境变量覆盖。接口地址、地域与密钥需对应。程序默认关闭外部跟踪，模型通信适配器不自动重放网络请求。只有首次回复的问题全部为`historical_only_risk`（某条当前风险仅引用历史资料）时，允许同一材料、同一规则下最多一次重新生成；两次共用`REQUEST_TIMEOUT_SECONDS`异步等待预算。网络异常、越权或未知引用不会触发这个机会，失败仍保留不完整评审。

重新生成不调用工具、不更换证据、不修改程序规则；允许`risks=[]`，把只有历史依据的建议移到`process_checks`，禁止为了通过检查机械添加当前证据编号。第二次仍执行完整结构、范围、引用和质量检查，人审及blocker规则不变。`attempt_details`逐次记录实际用量与质量问题，合计用量仅汇总已报告计数，缺失值仍为未知。

```powershell
python run.py check-config
python run.py start --change-id risk-pool --review-id real-review-001
```

`check-config`不发送模型请求、不显示密钥。真实调用会把当前材料发往配置的模型服务；接入真实数据前需先去敏。

**已有真实Qwen有效报告，历史实验与新版演示包分开保留。**用户运行的`qwen-safe-lh-001`首次被历史引用不足校验拦下；之后`qwen-safe-lh-002`首轮通过，并由用户署名`lh`确认阅读，状态为`review_acknowledged`。该旧报告中的补充建议针对当时原始safe材料，不会因增加新版演示包而被改写。新版完整材料包目前按离线流程验证，不把新增演示数据或提示词改动冒充成新一轮在线实验。离线演示仍是固定脚本，用量未知。

更新代码后用新的评审编号重新验证，例如在Windows命令提示符中运行：

```cmd
".\.venv\Scripts\python.exe" run.py start --change-id safe --review-id qwen-safe-lh-002
```

原`qwen-safe-lh-001`保留原检查结果；`resume`只提交人工意见，不会重新调用模型。对含质量问题的报告执行`acknowledge`后仍为`incomplete`，不能借确认阅读消除问题。新运行应检查`quality_issues`是否为空，再由本人阅读报告。

客户端显式使用`http_socket_options=()`，保留已提供的httpx客户端及其代理配置，避免依赖默认注入TCP参数的代理警告。`LangChainBetaWarning`提示MCP适配接口仍为beta，不代表本次请求失败。

## 5. 使用Redis

Redis是用于缓存、临时锁和排队任务的服务。默认`local`模式只在本进程内保存协调数据，不是Redis，也不提供跨进程锁。

如果本机已有Docker，可在项目目录启动仅绑定本机地址的演示Redis：

```powershell
docker compose up -d
python run.py start --demo --change-id safe --coordination redis --review-id redis-review-001
```

默认连接`redis://127.0.0.1:6379/0`。自定义入口通过`RELEASE_REDIS_URL`环境变量提供；切换Redis时连接失败会明确报错，不会静默退回本地。

排队运行：

```powershell
python run.py enqueue --change-id risk-pool --coordination redis
python run.py worker --coordination redis --demo --max-jobs 1
```

消息处理到人工暂停后就确认队列消息，人工继续通过`resume`单独处理。超时未确认的消息可重新领取；业务数据库和已有图检查点用于避免不必要重复运行。消息重投仍须按原评审的demo/API模式继续。

当前机器未检测到Redis服务或Docker，Redis命令路径使用`fakeredis`模拟服务行为测试，**尚未验证真实Redis服务、故障切换或跨主机部署**。Docker配置只适合本机学习，不是生产安全配置。

## 6. 与旧项目结合

先以未审核候选导入旧报告，不读取旧密钥，也不改旧文件：

```powershell
python run.py import-legacy --source "D:\Project\agent\it_ops_agent\runtime\reports\789fa90f3bb4446a-6dbb357e.json"
```

旧报告`complete=true`不等于人工确认。只有你实际审核后，才显式添加`--confirmed`重新导入同一个文件：

```powershell
python run.py import-legacy --source "D:\Project\agent\it_ops_agent\runtime\reports\789fa90f3bb4446a-6dbb357e.json" --confirmed
python run.py start --demo --change-id legacy-demo --review-id linked-review-001
```

导入记录保存在**新项目**的`data/legacy_cases.json`。旧报告为`order-service/demo`，不会进入`staging`评审；`legacy-demo`专门演示同范围结合。模拟旧数据仍然是模拟数据。

## 7. MCP确实在哪里

MCP是模型应用连接工具的协议。项目实际启动FastMCP服务对象，由官方LangChain `MCPAdapter`发现7个工具并转换成LangChain工具，再通过该工具入口调用，非直接函数调用冒充MCP。

默认`inprocess`是官方支持的进程内传输，适合本地演示，仍经过MCP客户端/服务端和工具发现。独立进程模式保留：

```powershell
$env:RELEASE_MCP_TRANSPORT = "stdio"
python run.py start --demo --change-id safe --review-id stdio-review-001
```

`stdio`通过子进程标准输入输出通信。本次环境限制了Windows命名管道，因此该模式测试被明确跳过；没有把进程内通过说成独立进程通过。恢复默认：

```powershell
Remove-Item Env:RELEASE_MCP_TRANSPORT
```

独立服务启动入口：`python -m release_guard.mcp_server --data-dir data`，它等待MCP客户端通信，不是普通交互菜单。

LangChain的`langchain.mcp`目前为beta接口，启动时可能显示`LangChainBetaWarning`。因此本项目固定已测试版本；该提示本身不是运行失败。

## 8. 中断恢复与报告

```powershell
python run.py retry --review-id review-001
```

`retry`会从尚未完成的自动节点继续；如果已经到人工暂停，不会重新取证或重复模型调用。若图已完成人审但业务表写入中断，相同的`resume`意见或`retry`可按稳定记录编号补写，不重复增加人工记录。

图检查点与业务库不是同一事务，恢复机制用于对账，不承诺整条链“恰好执行一次”。本地CLI中的复核人是用户声明，不具备企业登录或签名认证。

主要文件：

- `runtime/checkpoints.sqlite`：LangGraph工作进度。
- `runtime/reviews.sqlite3`：评审、事件及追加式人工记录。
- `runtime/reports/*.md`与`*.json`：当前可读报告及结构化结果；人审后更新同份评审报告，历史事件另存数据库。

退出码：0为等待人工或已确认阅读；2为阻断、需补材料、要求修改或拒绝；1为启动/参数/服务错误；130为用户中断。退出0也不代表可以生产发布。

## 9. 测试和资料

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

自动测试含实际MCP进程内协议、原生LangChain链、原生LangGraph人审、SQLite新进程恢复、Redis模拟命令和旧项目导入，测试本身不使用真实模型密钥。另有上述用户真实Qwen调用记录，应与模拟HTTP测试和离线演示分开报告。

`docs/00_阅读导航.md`包含通俗总报告、工具/框架细节、简历、面试问答、代码定位及实际验证记录；`examples/`提供去敏的演示结果。验证数量以`docs/05_测试与实验记录.md`为准。

本项目没有实现Java/Spring Boot后台、MySQL服务、真实Git/CI/监控连接器或生产执行器。代码中提供的边界与字段便于后续接入，但不能写成已完成的集成。

官方依据：[MCP接入](https://docs.langchain.com/oss/python/langchain/mcp)、[LangGraph暂停](https://docs.langchain.com/oss/python/langgraph/interrupts)、[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[Redis消息流](https://redis.io/docs/latest/develop/data-types/streams/)。
