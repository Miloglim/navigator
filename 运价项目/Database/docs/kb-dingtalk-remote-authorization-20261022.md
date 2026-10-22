# 知识库钉钉远程授权与自动刷新

## 目标

正式 Navigator 部署在 Linux ECS，不能依赖服务器本地 Edge/Chrome，也不能跨域读取用户浏览器中知识库的 `localStorage`。连接流程改为知识库已有的钉钉设备授权。

## 流程

1. Navigator 管理页点击连接后，服务器调用知识库 `/api/auth/dingtalk/start`，获得一次性 `auth_url`、`state` 和有效期。
2. 管理页在**当前操作者自己的浏览器**打开钉钉授权链接；服务器不启动浏览器。
3. 管理页每 3 秒读取 Navigator 状态；Navigator 服务端按 `state` 调用知识库 `/api/auth/dingtalk/status`。
4. 登录成功后，知识库返回 access token 和 refresh token。Navigator 只取 refresh token 加密保存，并用 access token 获取测试令牌。
5. 后续由 Navigator 按现有 24 小时令牌和提前 3 小时策略自动刷新；不需要再次扫码或手工填令牌。

## 安全边界

- 一次性 state 只绑定发起连接的管理员，服务端内存保存，超时即失效。
- 刷新令牌不返回管理页、不进入日志或 URL，只保存在 Linux/Windows 加密凭据文件中。
- 连接状态接口只返回是否连接、登录链接和错误信息，不返回 access token 或 refresh token。
- 知识库登录仍由用户本人完成；服务器不保存账号密码。

## 验证边界

单元测试覆盖发起授权、等待状态、成功获取 refresh token、写入测试令牌、暂停后不改旧令牌，以及状态接口不泄露秘密。正式部署后需要管理员实际扫码一次，确认钉钉登录完成、令牌刷新成功和公司查价可用。
