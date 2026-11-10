# Java 存储、Redis 报告和 Kafka 任务队列

网页提交任务后，Python 工作台登记任务并投递到 Kafka。两个独立 Python worker 从同一队列取任务并运行 Agent。Java 服务使用 SQLite 保存任务、案例及删除状态，报告正文和下载数据保存在 Redis。

Python 执行故障诊断、MCP 取证和 LangGraph 工作流。LangGraph 的检查点和运维 Agent 的历史记忆保存在共享的 `worker_runtime` 卷中。网页和 worker 使用同一 Python 镜像，两个 worker 各有独立的启动配置和健康心跳。

## 任务如何排队

故障诊断、发布评审和人工复核都走同一条队列。Java 用一次 SQLite 事务同时保存任务和待发送记录。Kafka 临时断开时，任务停留在排队状态，连接恢复后工作台会补发。消息只传任务编号和调度编号，worker 再通过 Java 服务读取执行所需的数据。

每个 worker 一次执行一个任务。领取动作由 SQLite 原子更新，重复收到同一个调度编号时，worker 会跳过已领取或已完成的任务。执行中意外中断且无法确认结果时，后台会在领取超过 20 分钟后标记中断，由使用者决定是否重试，避免自动再次调用模型。直接运行本机兼容启动器时默认使用本地线程队列。

已在独立 Docker 环境验证 Kafka 停机期间提交任务、网页重启、Kafka 恢复后执行、重复消息去重，以及两个 Agent 的报告写入 Redis 和下载。验收使用离线模型。

## 启动

已有本机 Python 环境时，可双击 `start-docker.cmd`。首次启动会生成内部存储令牌，模型密钥留空。启动器保留现有 `.env`，构建后等待 Kafka、worker 和 Java 存储就绪，再显示工作台地址。重复启动会复用数据卷。

启动器先检查 `DOCKER_EXE`，再查找 `PATH`、Windows 注册表和 Docker Desktop 的默认安装目录。它会把 Docker 工具目录传给自己的子进程，供 Docker 查找镜像凭据工具。若 Docker 安装在自定义目录且注册信息缺失，可在 PowerShell 中为当前窗口指定路径。默认安装位置与安装选项见 [Docker Desktop Windows 安装文档](https://docs.docker.com/desktop/setup/install/windows-install/)。

```powershell
$env:DOCKER_EXE = 'D:\Docker\resources\bin\docker.exe'
.\start-docker.cmd
```

新电脑若只装了Docker而没有Python，可按下面步骤手动填写`.env`后直接执行Compose。下面是完整手动流程：

1. 安装并启动官方[Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)，使用Linux容器引擎，确认`docker version`与`docker compose version`能够成功执行。
2. 先关闭本机旧工作台的8765服务；容器工作台使用相同端口。保留下面几个目录的相邻关系：

```text
outputs/
  agent_platform/
  agent_storage_java/
  agent_workbench/
  it_ops_agent_fixed_v1_2/
  release_risk_agent/
```

3. 在`agent_platform`目录打开PowerShell，准备配置：

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
python -c "import secrets; print(secrets.token_hex(32))"
```

没有Python时，可在PowerShell中生成随机值：

```powershell
$tokenBytes = New-Object byte[] 32
$tokenGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
$tokenGenerator.GetBytes($tokenBytes)
([BitConverter]::ToString($tokenBytes)).Replace("-", "").ToLowerInvariant()
$tokenGenerator.Dispose()
```

把生成的随机值填到`.env`的`STORAGE_SERVICE_TOKEN=`后面。这是工作台调用Java存储服务的内部令牌，与模型API Key不同。`.env`保存在本机，不加入Git，也不写入镜像。

4. 保持`MODEL_API_KEY=`为空即可使用离线脚本演示。需要真实模型时再填写自己的兼容接口配置：

```dotenv
MODEL_API_KEY=填写你自己的密钥
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen3.8-flash
MODEL_PROVIDER=qwen
```

这些值在容器运行时传入两个Python Agent，不使用构建参数。网页继续默认离线模式，只有选择真实模型并提交任务才调用模型。

5. 校验并启动：

```powershell
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
docker compose ps -a
```

健康检查通过后打开 [统一工作台](http://127.0.0.1:8765)。第一次构建需要下载镜像、Maven 依赖和 Python 依赖。Python 镜像使用 3.14，Kafka 客户端使用 `confluent-kafka==2.16.1` 的 Linux wheel。[Confluent 客户端发行文件](https://pypi.org/project/confluent-kafka/2.16.1/#files)

Kafka 使用 Apache Kafka 4.1.2 的 KRaft 单节点镜像。`kafka-init` 等 broker 健康后创建 `agent.jobs.v1`，共 4 个分区。再次运行时复用已有主题并核对分区数。`kafka-init` 以状态码 0 退出代表初始化完成。Java 存储和主题就绪后启动 worker，两份 worker 心跳有效后再启动网页。[Apache Kafka 镜像文档](https://kafka.apache.org/41/getting-started/docker/) · [Docker Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)

## 在网页怎么验收

1. 检查工作台的 Java、SQLite、Redis、Kafka 状态。队列模式应为 Kafka，报告存储应为 Redis。
2. 运行一次离线故障诊断，确认报告、JSON/Markdown下载正常。
3. 运行正常发布示例并手动确认阅读；再运行风险示例，确认容量阻断仍保留。
4. 创建或查看案例，在回收站/案例库执行永久删除前，必须输入当前记录的完整ID确认。
5. 执行`docker compose restart`后，报告与案例应保留。再核对已永久删除的案例或报告没有因为历史扫描重新出现。

新容器的数据卷与本机旧运行目录独立，不自动挂载C盘/D盘历史记录。旧项目文件保留在原位置；首次容器部署不会凭空读取旧数据库。

## 数据保存与永久删除

| 数据 | 管理组件 | Docker卷 |
|---|---|---|
| 业务任务、案例、删除墓碑 | Java + SQLite | `storage_data` |
| 报告JSON与Markdown | Java访问Redis | `redis_data` |
| Python内部检查点、运维记忆、任务输入与worker文件 | Python Agent | `worker_runtime` |
| 任务消息与消费者进度 | Kafka | `kafka_data` |

Redis 同时开启 AOF 和 RDB。AOF 是追加写入日志，每秒刷盘。RDB 是数据快照，当前配置在距上次保存至少 60 秒且已有数据变更时触发。报告没有自动过期，`noeviction` 避免内存不足时自动淘汰报告。AOF 每秒刷盘仍有故障窗口，备份应单独保存。[Redis持久化说明](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)

“移到回收站”可以恢复。“永久删除”不可恢复：业务记录的内容与关联Redis报告会按删除流程清理；保留最小墓碑用于防止扫描旧文件时重新导入。Python内部记忆/检查点属于独立数据，不应仅因删除网页报告就宣称所有历史痕迹均已擦除。若需要清除其他副本，按根目录永久删除说明逐项核对。

不要使用`docker compose down -v`来整理案例，它会移除整套数据库与报告卷。普通停止与重启保留卷：

```powershell
docker compose stop
docker compose up -d --wait
```

删除容器但保留数据卷：

```powershell
docker compose down
docker compose up -d --wait
```

## 备份与恢复

先停服务，再备份 SQLite、Redis、Kafka 和 worker 的数据卷。若希望先跑完已接收的任务，可在页面等待任务结束。以下命令在 `agent_platform` 中执行。

```powershell
docker compose stop
New-Item -ItemType Directory -Path backups -Force
docker compose cp storage:/data backups/storage
docker compose cp redis:/data backups/redis
docker compose cp workbench:/data/worker backups/worker
docker compose cp kafka:/var/lib/kafka/data backups/kafka
docker compose up -d --wait
```

备份包含案例、输入材料和报告，按业务资料保管。恢复前先保留当前卷的一份备份；在隔离的新Compose项目中执行`docker compose create`，把对应备份复制到停止的容器相同目录，核对SQLite与Redis属于同一次备份后再启动。恢复到非root容器时，需要恢复SQLite与worker目录的UID/GID为10001；Redis官方入口会处理自己的数据目录。不要边运行边覆盖数据库或AOF目录。

## 日志、健康与端口

```powershell
docker compose ps
docker compose logs --tail=80 storage
docker compose logs --tail=80 kafka kafka-init worker
docker compose logs --tail=80 workbench
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --describe --topic agent.jobs.v1
```

日志仍可能包含业务问题或证据，请在对外分享前脱敏。模型密钥、存储令牌不应进入日志；也不要把`docker compose config`完整展开结果发给别人，那里可能包含运行时环境值。检查配置建议用`config --quiet`。

本机的 USTC 镜像在查询基础镜像时返回了 `EOF`。项目的 Dockerfile 已改用 Docker Hub 官方仓库 `registry-1.docker.io/library`。第一次启动会由 Docker 引擎先下载缺少的基础镜像，再开始构建。以后会复用本地镜像。

宿主机只发布 `127.0.0.1:8765`。Kafka 使用内部地址 `kafka:9092`，Java 与 Redis 同样在 Compose 内网通信。工作台和 worker 另接模型请求网络。Kafka 单节点配置适合本机开发和演示，消息和消费者进度随数据卷保存。

两个Dockerfile都有同名的`.dockerignore`文件，排除已填写的`settings.py`、本地`.env`、运行目录、虚拟环境及数据库。普通`.dockerignore`同时保留作规则参考；构建以Dockerfile旁的专用规则为准。[Docker构建上下文说明](https://docs.docker.com/build/concepts/context/)

不用Docker的独立启动步骤见[NATIVE.md](NATIVE.md)。隔离测试附带的旧Redis 5二进制仅供验证，不是生产部署推荐。

本部署面向本机演示与开发，不提供企业登录、互联网公开访问、生产高可用或Redis跨机复制。真实模型效果、生产接入和收益需要独立验证。

## 离线检查

在已有Python环境并安装PyYAML后，可不依赖Docker执行：

```powershell
python -m unittest discover -s . -p test_platform.py -v
```

检查覆盖：仅回环发布端口、卷与AOF配置、内部令牌变量一致、依赖健康顺序、构建上下文私密文件排除、入口不保存密钥、健康检查拒绝假成功。它不证明镜像已成功构建，也不代替实际Redis联调。
