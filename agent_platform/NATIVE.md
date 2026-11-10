# 不用Docker时如何运行

本机已有Java和Maven时，可以分别启动Redis、Java存储服务与Python工作台。根目录验收已通过Java + 真正Redis进程的本机链路；测试工具附带的旧Redis 5二进制只用于隔离测试，不能当成生产部署推荐。本项目Docker方案使用Redis 7.4。

以下是独立运行步骤，不会替你下载或安装本机Redis。请先使用自己已安装且受支持的Redis，确保只有本机或可信内部网络可访问。Redis应启用持久化，报告不应配置TTL或自动淘汰。

## 1. 启动Redis

用已有Redis配置启动，开启AOF并确认`PING`返回`PONG`。若只想本机演示，可设置：

```text
bind 127.0.0.1
port 6379
appendonly yes
appendfsync everysec
maxmemory-policy noeviction
```

配置与持久化目录由Redis管理员管理。这里没有把测试Redis复制到项目或提供未经验证的Windows安装器。

## 2. 启动Java存储

在`agent_storage_java`目录的PowerShell中设置本机路径与令牌：

```powershell
$env:STORAGE_SERVICE_TOKEN="替换为自己生成的至少32字符随机令牌"
$env:SERVER_ADDRESS="127.0.0.1"
$env:SERVER_PORT="8780"
$env:SQLITE_PATH="C:/Users/你的名字/agent-data/storage.sqlite3"
$env:REDIS_HOST="127.0.0.1"
$env:REDIS_PORT="6379"
mvn -B -ntp package
java -jar target/agent-storage-2.0.0.jar
```

随机令牌可以用`python -c "import secrets; print(secrets.token_hex(32))"`在自己终端生成，不要发到聊天、截图或代码仓库。Redis有密码时，在此窗口另设`REDIS_PASSWORD`；不需要传给Python工作台。

Java默认在本机8780监听。`http://127.0.0.1:8780/health`只有SQLite和Redis都正常时才返回健康；Redis不可用时不会把内存或文件假装成Redis。

## 3. 启动Python工作台

在`agent_workbench`目录打开另一个PowerShell，设置与Java相同的存储令牌：

```powershell
$env:WORKBENCH_STORAGE_MODE="java"
$env:STORAGE_SERVICE_URL="http://127.0.0.1:8780"
$env:STORAGE_SERVICE_TOKEN="填写与Java窗口相同的随机令牌"
..\release_risk_agent\.venv\Scripts\python.exe run_workbench.py
```

依赖未安装时先按两个Agent的README创建Python环境。网页模型密钥继续使用各自原配置；默认离线演示不发送模型请求。

启动后打开 http://127.0.0.1:8765 ，存储状态应明确显示Java、SQLite、Redis都正常，报告存储为Redis。若提示兼容模式，先核对`WORKBENCH_STORAGE_MODE=java`，不要仅凭报告可显示就认为已经改用Java。

原本机任务数据库由工作台迁移逻辑处理，Java数据目录和Redis持久化目录也要备份。Python内部检查点与记忆仍保留，不能把业务存储迁移解释为所有SQLite代码都已改成Java。

关闭时先停止Python工作台，再停Java和Redis。已有Redis/SQLite服务属于你的本机环境，启动脚本不会擅自终止其他进程。
