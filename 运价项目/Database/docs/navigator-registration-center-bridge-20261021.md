# Navigator 自助注册中心桥接

中心账号注册契约与完整边界见 Prospector 仓库 `docs/navigator-registration-center-bridge-20261021.md`。

Navigator 在 `center_auth_url` 已配置时，`/api/auth/register` 先调用中心 `/api/auth/desktop/register` 并固定使用 `product=navigator`，成功后保存同名本地销售业务身份。中心失败时回滚本次新本地身份；旧版遗留的本地-only 账号必须使用原密码验证后才能绑定。本地密码摘要在绑定成功后清空，中心成为唯一登录真源。
