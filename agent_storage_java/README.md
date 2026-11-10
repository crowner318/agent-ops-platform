# Java业务存储服务

这个服务接管工作台的任务元数据、案例和报告存取。Python继续执行两个Agent及其推理流程；LangGraph内部检查点、Agent内部记忆仍是原项目组件，不应将本服务描述成把所有Python存储代码重写成Java。

技术：Java 21、Spring Boot 3.5.7、Spring JDBC、SQLite JDBC、Spring Data Redis/Lettuce。已在本机JDK25编译，字节码目标为Java21。

## 职责

- **SQLite**：任务、业务状态、案例和最小删除墓碑，使用JDBC预编译参数，表结构由Java创建。
- **Redis**：JSON和Markdown报告一起序列化到一个key，通过Lua原子替换；不设自动过期，不在Redis不可用时退回内存或磁盘副本。
- **删除**：案例正文从SQLite真正删除，留下仅ID的墓碑，防止历史同步复活。任务报告必须先到回收站，再以完整ID确认永久删除；先删除Redis内容，成功后清除SQLite里的日志、参数和文件定位信息，保留最小来源摘要墓碑。

Redis与SQLite不是同一事务。Redis删除失败时SQLite保持原记录，方便重试；Redis删除成功而SQLite提交失败时，操作也可幂等重试。服务内串行协调报告写入和永久删除，防止已删除报告被晚到的写入重新创建。当前部署为单Java实例，未实现多实例协调或高可用。

## 本机运行

需要Java21+、Maven和可连接的Redis。Docker Compose配置位于同级的工作台项目，使用容器Redis并启用AOF持久化；本服务不安装Redis，也不会因机器没有Docker而假装已经运行容器。

```powershell
# 访问令牌是服务内部令牌，不是模型API Key。请自行生成并与Python工作台保持一致。
$env:STORAGE_SERVICE_TOKEN = '替换为至少24字符的独立随机ASCII令牌'
$env:REDIS_HOST = '127.0.0.1'
$env:REDIS_PORT = '6379'
$env:SQLITE_PATH = 'runtime/storage.sqlite3'
mvn package
java -jar target/agent-storage-2.0.0.jar
```

默认仅监听127.0.0.1:8780。Docker环境可通过SERVER_ADDRESS=0.0.0.0绑定容器网络，配合私有网络和内部令牌使用；不建议直接公开存储端口。

本次机器的全局Maven仓库配置在D盘。隔离验证使用下面的可写仓库参数，不修改系统Maven配置：

```powershell
mvn '-Dmaven.repo.local=../../work/maven-repository' package
```

## HTTP接口

除精确的GET `/health`之外，所有接口要求`X-Storage-Token`。健康检查返回SQLite和Redis的实际连通状态，任一失败返回503；不返回密钥、绝对路径，也不在未检查Redis配置时宣称AOF已启用。

| 接口 | 功能 |
|---|---|
| GET `/v1/jobs?include_deleted=true` | 任务及删除墓碑；普通列表排除回收站与永久删除项 |
| GET `/v1/jobs/{id}` | 获取单个任务 |
| POST `/v1/jobs` | 创建`{"job":{...}}`，编号或来源冲突409 |
| PATCH `/v1/jobs/{id}` | 受限状态字段`{"values":{...}}`，不允许绕过永久删除 |
| POST `/v1/jobs/recover` | 将queued/running任务标为中断，不自动重放模型请求 |
| POST `/v1/jobs/import-tombstone` | 迁移最小历史删除墓碑，不覆盖活动任务 |
| POST `/v1/jobs/{id}/purge` | `{"confirmation":"完整任务ID"}`；清除Redis报告和材料元数据 |
| POST/GET/DELETE `/v1/reports/{id}` | Redis报告存取，POST结构为`{"json":{...},"markdown":"..."}` |
| GET/POST `/v1/cases` | 案例列表/保存，POST使用`{"case":{...}}` |
| DELETE `/v1/cases/{id}` | `{"confirmation":"完整案例ID"}`，正文永久删除 |
| POST `/v1/cases/import-tombstone` | 迁移`{"id":"已删除案例ID"}`；幂等写入墓碑并清除尚未清理的旧正文 |
| GET `/v1/case-tombstones` | 已删除案例ID，防止同步重建 |

## Kafka 分发记录

Java 在一个 SQLite 事务中写入排队任务和待投递事件。提交人工复核时，同一个事务把任务从 `waiting_human` 改为 `queued`。Kafka 生产者从待投递列表取得事件和任务编号。执行参数保存在 SQLite，由成功领取的 worker 通过存储 API 读取。等待投递的事件在进程重启后仍可继续。

| 接口 | 功能 |
|---|---|
| POST `/v1/dispatch` | 提交 `{id, task_id, action, updated_at}`。新任务另附 `job`。排队和执行中的任务合计最多 20 个 |
| GET `/v1/dispatch/pending` | 返回至多 100 个待投递事件的编号和任务编号 |
| POST `/v1/dispatch/{id}/published` | Kafka 确认写入后用空 JSON 对象标记。重复调用和领取后补标均有效 |
| POST `/v1/dispatch/{id}/claim` | 提交 `{worker_id, updated_at}`。只有第一个领取者收到任务编号与执行参数 |
| POST `/v1/dispatch/{id}/finish` | 原领取者提交 `{worker_id, updated_at}`。清除执行参数并保留已有终态或待复核状态 |
| POST `/v1/dispatch/recover` | 提交 `{before, updated_at}`。超过期限的领取被关闭，仍处于执行中的任务标记为中断 |
| POST `/v1/dispatch/recover-untracked` | 提交 `{error, updated_at}`。迁移时中断没有未结束分发事件的旧任务 |

遇到重复消息时，已领取或结束的事件返回 `claimed=false`。worker 异常退出后，已领取的模型任务会显示为中断。调度器保留这个决定，避免再次发送可能已经执行过的模型请求。

输入限制：任务/案例/服务/环境ID为1至80个ASCII字母、数字、下划线或连字符；案例最大256KiB；每种报告正文最大8MiB；HTTP拒绝超过20MiB或分块写请求。JSON拒绝重复字段和深度超过40层。报告接口不执行正文中的代码、路径或URL。

## 验证方式

`mvn test`使用真实SQLite临时文件和`com.github.codemonstur:embedded-redis:1.4.4`附带的真实Redis可执行文件，随机本机端口，测试结束关闭。Windows测试binary为Redis5.0.14.1，仅用于兼容性验证，不是生产Redis推荐版本；Compose使用Redis7，与Windows测试服务的数据目录分开。

测试覆盖鉴权（含编码URI）、JDBC任务CRUD、Redis原子报告/无TTL、真实网络故障503、回收站与确认守卫、永久删除与重试、案例真删除及同步墓碑、重启中断和迁移墓碑。完整日志在工作区`work/java-storage-test.log`。

Redis test依赖只在test作用域，生产可执行JAR不携带旧版测试Redis。测试库及binary来源：[embedded-redis官方项目](https://github.com/codemonstur/embedded-redis)。
