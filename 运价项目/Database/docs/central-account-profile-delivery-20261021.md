# 中心账号资料与成长统一交付

日期：2026-10-21  
版本：6.50.0

## 已实现

- Navigator 账号页从中心读取显示名、头像和成长摘要，本产品偏好继续保存在 Navigator。
- 账号资料保存先写中心，再写本地产品偏好；中心失败时保留原产品和本地状态，不假装保存成功。
- 中心新增本人资料写入和成长事件幂等接口；Navigator 通过中心会话调用，不接受客户端指定其他用户。
- 新增本地经验 outbox。事件与业务结果同时落库，中心暂时不可用时保持 pending，登录、账号页读取和后续业务动作可重试。
- 中心按 `source_product + event_type + subject_key` 去重，并执行规则版本、每日上限、产品准入和用户会话校验。
- 中心模式本地会话不再依赖本地密码摘要；本地密码不参与中心登录。

## 正式迁移

- Navigator 备份：`Database/backup/freight-postgres-20261021-172128.dump`，6226958 字节，SHA256 `b730163f75e40912e23a1780dd80b55440d8e8bc4461b97f19ce66b5f69047e2`，`pg_restore --list` 通过。
- 中心备份：`C:/ProgramData/MiloginCenter/backups/center-2026-10-21T09-21-49-925Z.dump`，195277 字节，SHA256 `3f6de41924c0c63cb1ddcf14483be317010b657cece382e5e91bf2a1136bcf27`，`pg_restore --list` 通过。
- 正式迁移提交 6 个账号、5098 条经验；`admin@yqn.com` 为中心 `Zayne / av-04 / 6612 XP / cycle 7 / level 28 / today 18 XP`，5088 条经验、显式旧账号映射和 1 条迁移审计均已核对。
- 中心生产服务已重新编译并受控重启，`live/ready` 正常；Navigator 8795 已由 watchdog 强制重载，HTTP 200、PostgreSQL 正常，outbox 初始 pending 为 0。

## 验证

- Prospector：typecheck、245 文件架构检查、Electron 构建、15 项定向测试、16 项隔离 PostgreSQL/HTTP、两次迁移 apply、篡改冲突拒绝和真实 Electron 账号页通过。
- Navigator：15 项账号/成长/中心客户端测试通过；编译通过。
- 跨栈：Navigator 隔离实例连接隔离中心 55844，实测中心显示名/头像读取、保存后中心变化、每日经验首次 +18、重复请求不再增加，中心最终为 1 条事件。

## 边界

- 本机正式中心仍使用 `http://127.0.0.1:55847` 管理例外，未开放公网 HTTP；云服务器仍需 HTTPS 和独立部署验收。
- 徽章尚无中心数据模型，继续显示未知；本次不伪造徽章。
- 全量普通回归仍有一项既有设置页“工作区外观”断言与当前未提交页面改动不一致，与本链路无关。
