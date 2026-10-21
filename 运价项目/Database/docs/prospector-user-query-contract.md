# Navigator / Prospector 用户授权与在线查价契约

此接口允许固定客户端 `prospector` 以 Navigator 用户身份读取报价，权限固定为 `rates:read space:read images:read`。禁止用采集器凭据或旧本地镜像替代用户授权。

## 授权

- `GET /api/prospector/authorize`：查询参数 `response_type=code,client_id=prospector,redirect_uri,state,code_challenge,code_challenge_method=S256`；返回浏览器授权页面。只接受完整 `http://127.0.0.1:<动态端口>/navigator/callback`，不允许用户名、额外路径、query 或 fragment。
- 页面复用 `/api/auth/login` 登录，展示当前账号，再由用户点击确认授权。`POST /api/prospector/authorize` 接收相同参数，要求有效浏览器 cookie 和明确匹配本服务的 Origin；返回 `{ok:true,data:{redirect_url}}`。授权码绑定用户、账号安全版本、完整 redirect_uri 与 PKCE，120 秒内单次兑换。
- `POST /api/prospector/token`：授权码兑换 `{grant_type:"authorization_code",code,code_verifier,client_id:"prospector",redirect_uri}`；刷新 `{grant_type:"refresh_token",refresh_token,client_id:"prospector"}`。统一返回 `{ok:true,data:{access_token,refresh_token,token_type:"Bearer",expires_in:900,user:{user_id,name,display_name,role}}}`。refresh 存活 2592000 秒，每次轮换并废止旧 access/refresh。并发兑换只允许一个成功；设备行锁串行化刷新与退出。
- `GET /api/prospector/me`：Bearer 返回 `{ok:true,data:{user}}`。
- `POST /api/prospector/logout`：Bearer 撤销该设备全部令牌，返回 `{ok:true,data:{revoked:true}}`，不会退出其他浏览器会话或设备。
- 新接口错误为 `{ok:false,error,code}`。无效/过期 access、账号不可用为 401；无效授权参数、PKCE、单次码或 refresh 为 400；范围/Origin/TLS 拒绝为 403。错误日志不记录密码、令牌或授权码。

## Bearer 读取与查询

Bearer 仅允许 `GET /api/rates /api/space /api/rates_grouped /api/space_grouped /images/<basename> /api/prospector/me` 及专用退出 POST。即使同时传管理员 cookie 或采集器头，也不能读取主数据/导出或执行写入。每次重新读取账号的 active、现行 role、must_change_password、password_hash 和 account_security.revision；停用、强制改密码、安全版本变化与账号删除后旧凭据失效。既有共享查询政策继续生效，不恢复已取消的群/航线限制。

平面查询保持旧响应 `{ok:true,total,limit,offset,rows,...}`；分组查询保持原 `groups` 结构。平面 `limit` 最大 500，分组最大 200；按返回 `total/limit/offset` 分页。服务器在计算 total 和分页之前执行全部筛选。Prospector 平面查询始终排除 status=已被覆盖，查看过期报价也不能把已被替代价格当成可报价；旧浏览器和分组历史语义不变。

除既有 `q,route,ctype,status,valid_since,time_from,time_to` 等参数外，Bearer 支持：

| 参数 | 语义 |
|---|---|
| `terms` | JSON 字符串数组；每个词 AND，词内跨字段 OR。运价匹配 route/pod/pol；舱位额外匹配 container_type/vessel_voyage |
| `lanes` | JSON 字符串数组，route 精确 OR |
| `polExtra`,`podExtra` | JSON 字符串数组，与原 pol/pod 模糊项 OR，扩展项精确等值 |
| `lane`,`carrier` | 不区分大小写的字面子串；百分号/下划线按字面字符处理 |
| `container` | 复用既有柜型解析与 ctype 搜索；40HQ/40HC/40HQ-HC/40HQ/HC 同档，40NOR 独立，40NOR/40HQ 等组合为多档 OR；45HQ/HC 保留原图文字派生检索规则 |
| `pol`,`pod` | 运价为字面子串，与 Extra 合并；舱位 pod/lane 作为额外跨字段词，pol 为起运港子串 |
| `sort=price&direction=asc` | 复用既有全查询价格升序，空价格最后，message_time/id 稳定打破并列，再分页 |

数组最多 64 项，每项最多 512 字符；格式错误返回 400。客户端 `includeExpired=false` 映射为 `valid_since=<北京时间今日>`，舱位 `days` 映射为 `time_from=<起始日>`。未知日期不伪造。`query_id=UUID&activity=intentional` 表示主动查询；同次运价/舱位、分页、重试复用同一 UUID。服务端绑定用户和 `_client_app=prospector`，以 `prospector:<user_id>:<query_id>` 在既有 `user_activity_events.client_event_id` 唯一键去重，并保留 `client_app/query_id` 来源及实际筛选条件。Prospector 专用 terms/lanes/polExtra/podExtra 按同一有界数组解析去空白去重后写入，lane/container 为最多512字符的白名单文本；内部身份和密码字段不进入 properties。客户端传入的下划线内部字段全部丢弃；旧浏览器统计语义不变。

## 持久化与部署

`prospector_codes/prospector_tokens/prospector_grants` 由既有 SQLite 初始化和 PostgreSQL startup-only ensure_identity_schema 建立；请求不做 DDL。原令牌/授权码只出现于响应，库内只保存 SHA-256 摘要；设备行持久保存撤销状态。授权时按表最多清理 256 个过期/已用授权码、过期/已撤销令牌和过期设备，避免一次请求全表删除。业务快照不携带这些私密认证表；恢复业务快照后设备授权清空。

按用户 2026-10-10 的明确决定，HTTP 接受实际客户端 IP 与 Host 都属于 RFC1918 私有局域网或回环地址的请求；Host 必须为字面量 IP，禁止公网 HTTP 和域名伪装。现有 `http://192.168.190.244:8795/` 继续使用，HTTP 不加密登录与授权数据，仅适用于用户信任的局域网。公网仍使用本服务原生 TLS (`--tls-cert-file/--tls-key-file` 或 `FREIGHT_TLS_CERT_FILE/FREIGHT_TLS_KEY_FILE`)。服务端不信任 `X-Forwarded-Proto` 或任意代理头；当前未定义受信任代理配置，不支持由远端代理声明 HTTPS 后继续以明文连接本服务。TLS 终止代理方案须单独设计并验证，不得临时放开此闸门。

`test_navigator_fixture.py` 仅创建临时 SQLite、合成管理员与测试 master，不加载生产账号或数据。作为 CLI 输出合成登录信息和 base_url，在 stdin 收到换行后清理；只供隔离 HTTP 契约验证。

## 独立 PostgreSQL 实测

`test_prospector_postgres.py` 默认跳过；仅显式设置 `NAVIGATOR_PRIVATE_POSTGRES_TEST=1` 时在 TEMP 建立全新 PostgreSQL 18 cluster，以随机 127.0.0.1 端口启动。来源只为合成 SQLite fixture，使用 `ops.postgres_migrate` 移入私有 schema，绝不读取 DEFAULT_ENV_FILE、load_dsn 或正式 SQLite/服务。测试复用同一 HTTP 授权契约，并覆盖 PKCE、代码/刷新并发单赢家、刷新与已接受退出竞态、账号实时状态、柜型别名、覆盖报价排除和留痕。2026-10-10 实测16项通过；完成后用精确私有 data 路径 pg_ctl stop，核实无该私有 cluster 进程及目录残留。Windows pg_ctl 守护进程会继承 PIPE，runner使用命令日志文件以免等待服务器退出才获得EOF。

## 桌面网页匹配（2026-10-10）

GET /api/prospector/options 返回 {ok,data:{ports,portGroups,carriers,routes}}，绑定现有只读设备授权，不包含账号、发送人和来源统计。桌面 rates 查询传 match=web 时复用网页 _multi_filter / port_filter 与 route/carrier/ctype 精确过滤，港口多值 OR，字段间 AND；仍排除覆盖报价并记录真实用户。AI 查询未传此模式，维持原关键词契约。
