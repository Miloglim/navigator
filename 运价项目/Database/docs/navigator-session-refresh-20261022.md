# Navigator 持久登录与中心会话续期

## 契约

Navigator 浏览器登录使用中心桌面会话接口，不再把 1 小时中心访问令牌作为唯一登录寿命来源：

- `POST /api/auth/desktop/login` 返回短时访问令牌和中心刷新令牌。
- Navigator 的本地 `freight_session` Cookie 仍保存本地会话 ID，服务端只保存其哈希。
- 中心刷新令牌保存在独立的 `freight_center_refresh` HttpOnly、SameSite=Strict、HTTPS 下 Secure Cookie，路径限制为 `/api`。
- `nav-api` 重启后内存访问令牌丢失，下一次请求用刷新 Cookie 自动续期；中心访问令牌到期时同样自动续期并轮换刷新 Cookie。
- 中心拒绝续期、用户改密、撤权或刷新 Cookie 缺失时，Navigator 删除本地会话并要求重新登录。
- 退出登录同时撤销中心刷新会话和本地会话，不保留可重放凭据。

## 安全边界

刷新令牌不进入 URL、日志、业务数据库或页面脚本。浏览器只持有 HttpOnly Cookie，Navigator 服务端不持久化明文刷新令牌。本地 `freight_session` 仍保留 30 天绝对期限；中心刷新会话按 15 天空闲期限控制。

## 验证

- 中心客户端测试覆盖桌面登录、刷新令牌轮换和续期请求。
- Navigator 账号生命周期测试覆盖登录、退出、改密与会话撤销。
- 正式环境使用一次性测试账号登录后重启 `nav-api`，验证同一浏览器 Cookie 仍能读取 session；测试数据随后清理。
