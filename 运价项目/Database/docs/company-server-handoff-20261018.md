# Navigator 公司服务器交接说明（2026-10-18）

## 已验证

- Python 全量测试：297 项通过，61 项需要外部服务的集成测试按规则跳过。
- 中心鉴权：中心登录、Navigator 权限校验、每次请求复核、退出清理均已接线；Token 只在进程内保存，不写数据库和日志。
- 本机健康检查：`http://127.0.0.1:8795/api/health/live` 返回 `ok=true`，后端为 PostgreSQL。

## 服务器配置

在 `ops/runtime.json` 或环境变量中设置 `NAVIGATOR_CENTER_AUTH_URL`，指向公司内部中心 API。跨机器访问必须由公司反向代理或网关提供 HTTPS；当前本机回环 HTTP 仅用于联调。

需要安装 Python 依赖（见 `requirements-postgres.txt`）、PostgreSQL，并准备正式业务库备份恢复路径。不要把 `freight_manual.db`、日志、Token、密码或证书私钥放进交接包。

## 现场验收

用公司提供的测试账号验证登录、查价、权限拒绝、退出后旧会话失效；再由管理员执行备份恢复演练和服务重启演练。
