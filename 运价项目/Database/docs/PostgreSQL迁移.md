# PostgreSQL 迁移

## 当前状态

2026-09-28 本机 PostgreSQL 已建表并迁移配置；业务条目在有校验归档后清空，HTTP 服务使用 `postgresdb.py` 适配器并通过隔离 schema 的预检、待审、放行、查询和登录回归。正式端口的运行后端以公开 `/api/health/live` 的 `data.backend` 现场核对；业务 `/api/stats` 已要求身份。

旧 SQLite 切换归档 `backup/cutover-archive-20260928-095441.db` 不参与自动轮转，保留旧待审记录作人工回溯；不要把它重放到新生产库。迁移不带 `auth_sessions`，原有用户需重新登录一次。心跳采集在切换时暂停，现场验证后恢复。

2026-09-28 切换验收：8795 的 `/api/stats` 返回 `backend=postgres`、正式业务四表均为 0；备份与守护计划任务已改用稳定的 Python 路径，备份任务手动触发结果码 0；四个 A/B 心跳已恢复。30 并发、300 次只读请求成功 300 次，p95 约 1.7 秒，仍需后续连接复用优化。

安全遗留：`postgresql.conf` 已写 `listen_addresses='127.0.0.1'`，但运行中的 PostgreSQL 进程仍报告 `*` 并监听所有网卡。`pg_hba.conf` 当前仅列本机 `127.0.0.1/32` 和 `::1/128`，远端不应获得认证。当前普通用户无法操作 `postgres-freightpilot` 服务；请在管理员终端重启该服务，然后核对 `SHOW listen_addresses` 与 5432 的监听地址均为 `127.0.0.1`。这项未完成前不要把数据库端口放行给局域网。

迁移工具默认只迁配置：操作员、群配置、港口、船司、航线、发送人、港群、作用域别名与字段默认值。会话永不迁移；已发布报价、待审、影子记录和规则候选必须显式传 `--include-business-data` 才会迁移。

## 服务器准备

在公司固定内网 IP、长期在线且可备份的设备或 VM 上安装 PostgreSQL 16+。创建专用数据库和最小权限业务账号；数据库必须只允许应用服务器访问。不要把数据库密码、DingTalk 密钥或连接串发到对话中。

在应用服务器的受控环境变量中设置 `FREIGHT_POSTGRES_DSN`。示例仅说明格式：

```text
postgresql://freight_app:<password>@127.0.0.1:5432/freight_ledger
```

安装驱动：

```powershell
C:\Users\YQN\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install -r Database\requirements-postgres.txt
```

## 执行顺序

先查看计划，不连接 PostgreSQL：

```powershell
cd Database
python ops\postgres_migrate.py --dry-run
```

确认新建的 PostgreSQL 数据库为空后执行配置迁移：

```powershell
python ops\postgres_migrate.py
```

工具会创建表、索引和兼容视图，再对每张已迁移表比对行数和 SHA-256 内容摘要。任何不一致都会使事务回滚。

## 预切换自动镜像

本机试点环境将连接串放在 `C:\ProgramData\FreightRate\freight-postgres.env`，不写入仓库、环境日志或对话。执行以下任务可将 SQLite 的主数据自动同步到 PostgreSQL 并做同样的摘要校验：

```powershell
cd Database
python ops\postgres_sync.py
```

该任务永远不迁会话或业务记录。目标 PostgreSQL 只要已有报价、待审、影子记录或规则候选，就会拒绝运行，防止以“同步配置”为名清空正式数据。

只有历史数据明确需要保留时才执行：

```powershell
python ops\postgres_migrate.py --include-business-data
```

目标已有本项目数据时默认拒绝覆盖。`--replace-target` 会清空目标表，只能在隔离环境、已确认备份后使用。

## 切换门槛

数据库迁移成功不等于应用已切换。必须同时检查计划任务和 `/api/health/live` 实际后端，另用已登录的 `/api/system/health` 验证数据库连通；自动采集仍只允许 `/api/validate` 后接 `/api/pending`。`ops/postgres_backup.py` 创建 `pg_dump` custom 归档并用 `pg_restore --list` 验证目录，但这不等于完整恢复演练。需要进一步配置异机备份与独立库恢复演练；旧 SQLite 归档至少保留 48 小时，不能在切换后继续写入。
