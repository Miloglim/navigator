# HTTP 契约

## 2026-10-21 Prospector 客户端下载

新增公开只读下载入口：`GET /download`、`GET /api/downloads/prospector/latest`、`GET /downloads/prospector/<fileName>`。清单使用统一 `{ok,data|error}`，包含版本、文件名、大小、SHA256、发布时间、系统要求、签名状态和更新说明；文件接口只发送清单当前登记的唯一安装包，并设置附件下载与长期缓存头。下载能力不读取运价业务库、不要求 Navigator 产品权限，也不改变 Prospector OAuth 查价、自动更新中心或采集写入口。运行目录由 `NAVIGATOR_DOWNLOADS_DIR` 配置，默认 `Database/.runtime/downloads/prospector`。

## 2026-10-21 外部 Agent 标准 JSON 入口 v1

新增 `POST /api/agent/intake/v1`，仅接受 `Authorization: Bearer <NAVIGATOR_AGENT_API_TOKEN>` 或既有采集 `x-push-token`。正式外部调用必须使用前者；Token 由 Navigator 服务环境单独配置，至少 32 字符，不进入仓库、日志或响应。非本机回环访问继续要求 HTTPS。

请求信封固定为 `{schema_version:"1.0", kind:"rates|space|group_suggestion", agent:{name,version?}, records:[...], preserve_other_sources?, preserve_existing_source_rows?}`。每条记录必须带非空 `来源群`、`消息ID`、逐字 `消息原文`，字段值沿用既有 agent-input-contract。服务端强制 `origin=agent`，复用 `act_pending` 的机械校验、幂等和人工审核闸门，不直接写 `freight_rates` 或 `space_records`。

成功统一返回 `{ok:true,data:{schema_version,kind,submitted_by,staged,ids,entry_ids,check_passed,check_failed,validation_failures,coverage,...}}`；`staged` 只表示进入人工审核。请求错误返回 400，Token 缺失或错误返回 401，统一错误体为 `{ok:false,error:{code,message}}`。外部接入 Skill 位于 `Skill/navigator-agent-intake/`。

## 2026-10-19 中心 Navigator 只读摘要

新增 `GET /api/center/navigator/dashboard`，仅接受中心会话的 Bearer Token；服务端向配置的中心身份服务重新校验 Navigator 准入，仅管理员和航管可读。返回 `ok/data` 摘要，包含服务后端、运价/舱位/群组/待审核/待核问题数量及最新消息时间，不返回价格明细、原文、图片、密码或令牌。该接口供 Milogin 管理中心代理调用，不能替代 Navigator 8795 的业务页面和人工审核接口。

## 2026-10-18 异常及权限修正

- GET/POST 未预期异常返回500和固定错误提示，不向客户端发送内部异常文本；预期参数错误400、权限错误403保留业务提示。
- `GET /api/company-rates/tasks` 必须携带 `id`；缺少返回400，不属于当前账号或不存在返回404。
- 管理员原有航管能力保持不变，并显式允许业务知识查询、待核问题报告及既有结案路由；销售、航管及 Agent 权限不扩展。

## 知识库浏览器连接与令牌剩余时间（6.45.0）

新增 `POST /api/admin/company-token-refresh/connect`，空对象请求，仅服务器本机已登录且绑定公司凭据的管理员可用。调用后服务打开独立知识库登录窗口，立即返回原状态及 `connection_state=connecting`；`GET /api/admin/company-token-refresh` 继续用于轮询，新增 `connection_state`（`idle|connecting|connected|error`）、`connection_error` 和 `expiry_estimated`。浏览器登录结果只在本机读取并加密保存，不随响应或日志回传。登录完成后立即尝试刷新测试令牌；失败保留原令牌并显示脱敏原因。`POST /disable` 可取消尚未完成的连接并暂停自动续期；已有登录凭据保留，`POST /enable` 可直接恢复。

页面每秒显示剩余时间：知识库若返回 `expires_at` 使用其到期时间；否则按刷新时间加24小时估算。旧手动配置只有保存时间，按保存时间加24小时估算并明确提示实际可能更早。倒计时不是服务端有效性证明，自动续期以刷新接口实际结果为准。

## 公司查价测试令牌自动刷新（6.44.0）

仅本机已登录管理员可用；现有公司查价凭据的绑定管理员不变。`GET /api/admin/company-token-refresh` 返回 `auto_refresh`、`can_resume`、`last_success_at`、`expires_at`、`last_attempt_at`、`last_error` 以及原凭据状态，不返回登录续期凭据或测试令牌。`POST /api/admin/company-token-refresh` 接收 `{refresh_token}`，以 Windows 当前用户 DPAPI 加密保存。`POST /api/admin/company-token-refresh/now` 接收空对象并立即尝试刷新，失败时在 `data.last_error` 给出脱敏原因且保留旧测试令牌。`POST /api/admin/company-token-refresh/disable` 接收空对象，仅暂停自动续期并保留加密登录凭据及现有测试令牌；`POST /api/admin/company-token-refresh/enable` 接收空对象，恢复自动续期，若临近到期则立即尝试刷新。

后台每小时检查一次，在测试令牌预计失效前3小时内续期：先调用知识库 `/api/auth/refresh` 获取短期登录态，再调用 `/api/me/test-token`，确认返回 `kbtt_` 令牌后通过既有公司凭据存储更新。首次连接后页面立即发起一次刷新。知识库续期凭据过期或撤销时状态显示需要重新连接；不承诺无条件永久登录。

## 报价编辑边界（6.38.0）

本次不增删HTTP接口。查询勾选只生成浏览器报价草稿，编辑、列操作、恢复格式及复制均不调用业务写接口；草稿格式与存储范围见 [架构基底](架构基底.md) 中“报价草稿与邮件复制”。舱位草稿只导入 CARRIER、POL、POD、ETD、ETA、VESSEL No.、TYPE、O/F、ROUTE、TT，其中O/F来自 `totalOceanPriceUsd`；`totalPrice` 不导入草稿单元格或邮件正文。

## 销售柜型单选与实时复用（6.37.1）

销售查询的 `ctype` 前端限定为可取消的单选：选中时只提交一个台账规范柜型 `20GP|40HQ/HC|40NOR|45HQ/HC`，再次取消后省略该参数，表示不限制柜型。公司运价接口仍返回上游完整产品，前端结果表及加入报价表的数据按当前单选柜型裁剪；未选择时展示全部台账规范柜型。后端保留既有逗号多值兼容能力供旧调用方使用，不代表当前页面允许多选。

船司线上查价不再维护第二份柜型选择，`container_id` 必须由销售主查询的当前柜型映射得到：`20GP→1`、`40HQ/HC→12`、`40NOR→20`。未选柜型时不创建实时任务并提示先选择；台账支持但上游实时接口未声明的 `45HQ/HC` 同样不创建任务。公司接口的 `40GP→11` 能力仍保留在服务端契约，当前销售台账没有40GP独立规范档，因此页面不提供该入口，不能把40GP归并成40HQ/HC。

## 船司实时查价柜型（6.37.0）

`POST /api/company-rates/realtime` 的 `container_id` 每次只允许一个正整数：`1=20GP`、`11=40GP`、`12=40HQ/HC`、`20=40NOR`。同时须提交正数 `weight` 和至少一个船司；未知柜型编号返回400，不创建外部任务。查询任务的 `query.container_id` 是结果展示及舱位报价所用柜型的唯一依据。6.37.1起页面改为复用销售主查询柜型，不再默认选择或维护第二份实时柜型控件；实时价仍只在用户显式点击后调用。

## 实时交叉查询覆盖（6.35.1）

每个起运港×目的港组合分别创建实时任务，完整性按请求carrierIds核对，不能仅检查已返回行的status。不同轮次返回不同船司时保留此前已完成行；全部请求船司均返回状态0或1才结束。120秒预算耗尽且已有结果时保留结果，在首行附 `_realtime_missing_carriers`（缺失船司ID字符串数组），任务为partial，前端按resolved_mappings显示缺失的港口组合与船司。完全未收到结果仍失败。status=0且result为空表示该响应未提供报价，不代表所有船司完成或永久无价。

## 销售承运人多选（6.35.0）

本地运价、舱位及共用筛选的归组查询支持 `carrier=CMA,MSC`，表示命中任一已选船司，使用参数化IN查询，单值保持精确匹配。前端复用港口标签输入交互，按船司别名索引归一去重；公司运价及船司实时请求继续传 `carriers:["CMA","MSC"]`，不改变各自日期口径。

## 船司实时ETD起始日（6.34.4）

实时查询ETD单日输入表示所选日当天及以后，前端仅传valid_from，北京时间零点毫秒时间戳；createIds仅提交validFrom，省略validTo。服务端仍兼容调用者显式传valid_to的有界请求。替代6.34.2/6.34.3中的实时ETD当天起止说明。结果仍按船司保留原始result数组，前端必须将数组中每条报价单独展示，不能把同船司或同价报价合并；展示对应ETD、船名航次和该条价格。

## 公司有效期筛选修正（6.34.3）

销售公司运价单日选择仅提交 `valid_from`（所选日期北京时间00:00的毫秒时间戳），省略 `valid_to`，表示当天仍有效及之后生效的报价。此单边筛选继续按原响应中的班期编号补取独立报价，补查返回已在该日期之前失效的报价不加入结果。仅显式提交 `valid_to` 的有界查询保留原范围路径；取代6.33.2中所有显式日期一律跳过补查的描述。实时价格独立ETD仍传当日起止两个时间戳。

## 公司接口映射管理（6.34.0）

6.34.1：自动港口匹配独立核验本地规范中英文名与公司响应港名；本地代码和别名仅作搜索输入，不能覆盖港名冲突。有明确港名时，找不到一致候选则报错，不接受仅代码命中；未改变管理员明确确认映射的优先级。查询签名带港名核验策略标记，旧规则缓存不复用。

全部仅管理员会话可访问，采集令牌和普通账号无权限；身份由服务端注入，响应统一 `{ok,data?,error?}`。

- `GET /api/admin/company-mappings`：当前公司业务身份下的 `rows`，以及启用的本地主数据 `choices.port/carrier`。
- `POST /api/admin/company-mappings/probe`：提交 `kind=port|carrier, local_name`，按公司接口精确匹配；新结果保存为待确认并返回公司编号/代码/名称。已确认映射直接返回，已停用拒绝自动回退。
- `POST /api/admin/company-mappings/save`：提交 `kind,local_name,company_id,company_code,company_name,status,note`；已有条目必须携带当前 `revision`。公司编号为正整数，规范名须存在于启用主数据，标准港口代码不得跨国；状态为 `pending|confirmed|disabled`。返回保存后的完整行。

页面保存并确认使用confirmed，停用使用disabled；编辑停用行后重新确认可恢复。该表匹配港口/船司身份，不修改价格。自动发现不覆盖管理员决定；变更后所有用户的新公司查价生效，本地查询独立。

## 可选公司运价与船司实时价（6.33.0）

6.34.2：实时查价的valid_from/valid_to由独立ETD日期按上海自然日起止生成，不使用sf-valid报价有效期。实测请求shipOwnerShippingClauseId=1，返回shipOwnerShippingClause.selectedValue=1、chineseName=CY/CY。创建接口返回task=[]是空任务，不能误报结构错误或等同无报价；须核对业务身份/权限等调用条件。xUserId必须使用公司业务用户编号，不使用钉钉ID。此次按公司页面ID修正后真实创建/轮询成功。

6.33.2：无日期筛选的公司列表按 `schedule_list.product_id` 核对产品覆盖，并用响应 `comments_list.validFrom` 向同一列表接口补查各产品自己的价格和有效期。显式日期筛选不扩大范围。单组合最多20个补查日期，每次最多20页。若补查只返回关联这些编号的其他有价记录，而没有对应编号自身的价格记录，编号保存在 `_company_associated_unpriced_products`；若连关联有价记录也未找到，则保存在 `_company_missing_products`。两种情况均使任务为 `partial`，前者不在销售结果页另行显示编号提示，但不得把关联记录的价格复制或归属给班期编号；后者仍显示缺价提示。以上内部字段不作业务价格。结果及来源开关仅在销售运价页显示，其他页及切页后收到的响应保持隐藏。

- `POST /api/company-rates/query`：登录的管理员、航管或销售创建公司运价异步任务。请求为 `{pol:[string],pod:[string],carriers:[string],valid_from?,valid_to?}`；起运港和目的港必填，港口组合最多 20 个。立即返回 `{ok:true,data:{task_id,cached}}`，其中 `cached` 为同账号、同类型、同查询签名且仍有效的最近任务，可能为空。
- `POST /api/company-rates/realtime`：同一权限创建船司实时价任务。除上述字段外须按6.37.0契约提交一个 `container_id` 和正数 `weight`，且至少一个船司。实时价只在用户显式点击后调用。
- `GET /api/company-rates/tasks?id={task_id}`：只允许任务所属账号读取，返回状态 `queued|running|complete|partial|failed`、各港口组合结果、单组合错误和每组结果的 `fresh` 标记。读取不触发外部请求。
- 三个接口均使用统一 `{ok,data?,error?}` 信封。请求错误返回 400，匿名返回 401，无权限或读取他人任务返回 403/404。外部任务失败只改变该任务状态，不改变 `/api/rates`、`/api/space` 的响应和时序。

公司查价的港口和船司输入先经过独立匹配层：港口名称、代码和别名统一大小写、空格、标点及重音后，仅接受 `type=11` 的唯一具体港口；命中 `type=4` 城市时按公司契约用 `types=[11]` 展开具体港口。船司优先精确代码，已确认的简称映射包括 `MSK/马士基→MAERSK`、`CMA CGM/达飞→CMA`；模糊候选、多候选和无候选均不自动选第一条。映射失败只影响对应公司查价组合，不影响本地运价接口。

任务响应的 `query.resolved_mappings` 为本次成功确认的 `{kind,input,id,code?,name?}` 数组，页面提供折叠查看。主数据提供港口代码、国家、规范名和别名，船司提供代码、SCAC及名字/别名；同名港口不能跨国家替换。城市展开后仍须匹配明确的具体港口，不自动扩大到所有子港。船司输入回显 `input_key` 和模糊分数不作为身份依据；任一指定船司无法确认时不放宽为无船司过滤。成功匹配使用进程内缓存，按业务用户和主数据内容区分，24小时过期、最多512项；重启清空，不改本地主数据。

`GET/POST /api/account/profile` 的 `preferences` 保留布尔项 `company_rates_enabled`、`carrier_realtime_enabled`，用于兼容旧客户端；当前销售页面不再读取这两个字段控制查价入口。运价查询自动调用既有 `POST /api/company-rates/query`，但须同时提供起运港和目的港；缺少任一项时页面不创建任务并说明条件。船司线上查价仍由用户手动发起。字段存储与校验规则不变。

- `GET /api/admin/company-credentials`：仅服务器本机回环地址上的管理员可用，只返回 `{configured,user_id,updated_at,can_manage}`，永不返回 Token。凭据已绑定其他管理员时 `can_manage=false`，页面隐藏配置表单。
- `POST /api/admin/company-credentials`：同一访问边界，请求 `{token,user_id}`；Token 必填，业务用户 ID 必须为数字。首次保存把当前管理员账号绑定为唯一凭据管理员，后续其他管理员覆盖返回403。成功后使用 Windows 当前服务账号加密覆盖保存，响应仍不含 Token，后续所有账号的查价任务无需重启即可共享新值。
- 非管理员或局域网其他电脑访问统一返回 403。凭据文件无法解密或格式损坏时拒绝查价，不回退到不确定身份。

## 公告多选范围（6.31.0）

`GET /api/notices`增加`groups`和`routes`查询参数，均为JSON名称数组。数组内及两种维度之间按任一命中筛选，保留全体公告；旧`group`单值参数继续精确筛选且不混入全体。返回增加可选`routes`，每条公告增加`target_groups`和`target_routes`。默认群偏好以实际多群范围匹配，不能把旧兼容字段为空的多范围公告当作全体。

`POST /api/notices/save`接受`target_groups:list[str]`、`target_routes:list[str]`，分别最多100项，去重后必须为启用的群聊/航线规范名。两者空数组表示全体，显式空数组用于清空范围；编辑时未提供的范围保留原值。停用的原有范围允许保留，不允许借此新增其他停用范围。旧`group_name`兼容输入与旧数据单群范围保留；多群及航线公告的旧字段为空，不作为范围判据。范围是公告分类与筛选，不改变查看、编辑、删除及置顶审核的账号权限。

## 港群名称与识别别名（6.22.0）

`port_groups`维护及完整主数据增加`aliases`，紧凑`portGroups`增加`a`（逗号分隔别名）。name/n为规范港群名；label/l仅为中文标签；members/m及specials/s必须为现有启用港口规范名，不能填港口别名或港群名。

`GET /api/master`增加`portGroupIndex:{pol:{折叠名称:规范港群名},pod:{折叠名称:规范港群名}}`，由与`POST /api/master/resolve`相同识别器派生，只包含全局可唯一确定的港群名称，方向不允许或存在歧义不输出。群作用域别名由后端根据source_group识别，不提升为全局。

新提交报价识别NEB、NE ports、北巴后归一为Brazil North Ports；旧报价原值保留。销售pol/pod过滤兼容港群别名，nogroup=1仍匹配该港群的所有登记别名，但不展开成员。维护同方向重复港群识别名返回错误，不能按先后顺序覆盖。

## 名称识别（6.21.0，shadow阶段）

`POST /api/master/resolve`输入`{source_group?:string,items:[{type:"pol"|"pod"|"carrier"|"route",value:string}]}`。单次1至200项，每个名称及来源群最多256字符。返回`{ok:true,data:[{input,entity,canonical,status,candidates?}]}`，顺序数量与输入一致。entity为port/port_group/carrier/route；status为resolved/unknown/ambiguous/invalid_target；仅resolved提供规范名，其他状态canonical/entity为null，歧义可列候选。顶层ok表示请求有效，不代表全部名称成功。格式错误沿用HTTP400结构化错误。

会话或采集凭据鉴权，销售、航管、管理员和Agent可读，匿名拒绝。只查询已启用主数据，不插入港口、港群或别名。群作用域先于全局别名，但不得覆盖另一个已登记规范名；折叠后规范名冲突仍拒绝。港群遵循pol/pod/both适用方向，别名目标须为该方向现有规范名，复合目标不偷偷拆解。

原校验新增`name_resolution:{stage:"shadow",equal,fields:[{field,input,legacy,legacy_ok,matches,equal}]}`。采集、入库、待审核仍用旧结果，不能把对照字段当自动放行或待核状态。价格拆分、柜型及有效期解释不属于名称接口；未知名称在所有入口一致拒绝待第二阶段验证。

## 45尺高柜查询（6.20.0）

现有运价、舱位及折叠报价查询的`ctype`支持`45HQ/HC`，兼容`45HQ`、`45HC`。匹配记录柜型、箱型箱量、备注或原文明示的45HQ/HC及中文45尺高柜，不将普通金额45、145HQ、45GP误匹配。与其他柜型逗号多选时按OR；其余筛选条件、权限及状态范围仍按AND。计数和分页由后端完成，不是当前页前端过滤。返回中的20GP/40HQ/HC/40NOR价格保持原口径，45报价及适用限制从详情读取。

## 采集待核问题（2026-10-08）

- `POST /api/review-issues`：仅采集令牌可写，正文 `{issues:[{source_group,message_id,message_text,media_ids,reasons}]}`，每批 1–200 条；返回 `{ok:true,data:{count}}`。按来源群与消息 ID 幂等更新未处理问题，不重新打开已处理问题。不写 `pending_records`，不推进水位。
- `GET /api/review-issues?status=open&limit=500`：仅管理员、航管可读，返回 `{ok:true,data:{total,rows}}`；每行保留原文、原图引用、阻塞原因和状态。销售不可见，最多返回 500 条。
- `POST /api/review-issues/resolve`：仅管理员、航管可写，正文 `{source_group,message_id}`；返回 `{ok:true,data:{resolved:boolean}}`。只记录“已处理”，不生成报价、不放行、不删除原始证据。
- 采集在 BUILD 阶段先持久化本轮解析阻塞，再推进到预检/提交；接口失败时停在原阶段，水位不动。旧版服务不支持此接口时必须升级服务，不能把本地报告冒充前端可见。

## 价舱管理来源群候选（6.15.0，2026-09-30）

- `GET /api/rates` 与 `GET /api/space` 可传 `include_source_groups=1`，响应增加 `source_groups`。候选仅按记录类型及服务端解析后的 `ownership=mine|all` 计算，不受当前分页或已选来源群影响；航管 `mine` 只返回本人稳定 `publisher_id` 归属的来源群，管理员、销售及 `all` 返回全范围候选。
- 价舱管理切换“我的条目/全部条目”或运价/舱位时会清空旧来源群选择，再使用本次查询返回的 `source_groups` 重建下拉，避免旧的全库候选继续限制新范围。未传该参数时既有响应不增加计算和字段。

## 账号消息中心 HTTP（6.13.3，2026-09-30）

- `GET /api/notifications?limit=20&offset=0&unread=1` 只返回当前登录账号自己的消息；`unread=1` 仅看未读。`GET /api/notifications/unread-count` 返回 `{ok:true,data:{count}}`。管理员、航管、销售均可用，匿名为 401，Agent 凭据为 403。
- `POST /api/notifications/read` 接收 `{id}` 或 `{all:true}`，只能修改当前账号的已读状态。`POST /api/notifications/action` 接收 `{id,action}`，当前 action 仅为 `approve` 或 `reject`，且只对本人收到、尚未处理的公告置顶申请生效；角色权限仍由公告审核接口再次校验。
- 销售公告置顶申请按申请修订号幂等通知所有当前可登录管理员；公告更新会结清旧申请。任一管理员处理后，同一公告的其他管理员申请同步标为已处理，发布者收到结果消息。消息正文不复制公告富文本或图片，只保存标题、摘要、关联公告编号和必要操作版本。
- 已登记账号提交密码重置申请时，所有当前可登录管理员收到不可直接操作的提醒，仍须进入人员账号库核实身份并设置新密码；重复提交只刷新同一条未结提醒，不叠加未读数。本人改密或管理员完成重置后，相关提醒自动结清并退出未读计数。未知账号不写申请或通知，匿名响应保持一致，消息不保存密码。

## 账号头像 HTTP（6.6.0，2026-09-30）

- `GET /api/account/profile` 的 `preferences.avatar_id` 为 `null`、旧版 `av-01` 至 `av-30`，或新版 `av2-{w|m}{01..05}-{1..3}`。`options.avatar_ids` 是新旧完整有序白名单；`options.avatar_groups` 是女/男 -> 五个人物 -> 每人三种造型的新版目录（每种含 `id,label`）。未选择时显示用户名首字；旧版选择保留并在选择器中单独展示。
- `POST /api/account/profile` 沿用现有 `{display_name,preferences}`，偏好可选 `avatar_id`。仅接受白名单编号或 `null`；省略时保留本人原选择，未知编号、类型和额外字段返回 400。修改头像不撤销会话，不改变角色/经验/查询范围，也不能指定他人账号。
- `GET /assets/avatars/{avatar_id}.png` 只按 60 个新旧精确文件名返回本地 PNG；其他路径返回 404。不返回账号身份信息，页面不访问外部头像域名。

## 账号成长 HTTP（6.18.0，2026-09-30）

- `GET /api/account/growth?limit=10&offset=0`：登录用户只读自己的经验概览、线性等级区间、循环区间、规则和分页流水。`summary` 增加 `level_cost/level_progress/cycle_start/cycle_end/cycle_xp`；`limit` 为 1–50，`offset` 非负。身份只来自会话，Agent 凭证不能读取。
- `POST /api/account/growth/search`：请求体只能是 `{}`；前端仅在运价或舱位主动查询成功后调用。Asia/Shanghai 当日首次记 `daily_effective_search +18 XP`，返回增加 `awarded_points`。同日 v1 的 `daily_quote_search` 已存在时不重复补领。
- 航管成功审核时，服务端内部写入 `daily_review_activity +18 XP`；无修改放行按待审编号加 2 XP，修正后放行加 6 XP。管理员批准规则候选写入 `daily_governance_activity +18 XP` 及候选贡献 6 XP；驳回只记当日治理活跃。自动报价身份登记、自动绑定、登录、刷新、打开详情和账号设置均不计经验。
- v2 每循环 50 级；单级成本按 `12 + floor((level-1)*18/49)` XP 线性增加，整循环 1026 XP。单账号每日最多写入 30 XP。事件以 `(user_id,event_type,subject_key)` 幂等；客户端不能指定经验值、角色或目标用户。
- v1 流水保持原样，查询时把旧总经验映射到相同循环、等级和本级进度，再叠加 v2 经验；不批量改写历史，不因规则升级降级。等级只表示活跃与贡献，不授予权限。
- 成功产生经验的业务写响应可选增加 `growth_award`：`{awarded,awarded_points,today_xp,daily_cap,level_up,cycle_up,previous_level,previous_cycle,summary}`。该字段由服务端在业务落库后生成；未获得经验时可省略，客户端不得据业务数量自行补算。`POST /api/account/growth/search` 在既有顶层兼容字段之外也返回同一结构。

## 公告看板（6.1.0）

- 所有公告接口仅接受已登录admin/operator/sales账号；Agent令牌不授予公告权限。返回统一为`{ok:true,data}`或`{ok:false,error}`；不接受客户端伪造发送人、角色。
- `GET /api/notices?limit=20&offset=0&group=&tag=&q=&pending=1`：data含rows、total、limit、offset、groups、tags、expiry_supported=true；row增加expires_on（截止日YYYY-MM-DD或null）。前端仅在收到此标志后启用有效期表单，避免旧服务忽略字段。limit上限50；group/tag精确过滤，q匹配标题、正文可见文字、发送人、群分类、标签、时间、有效期和置顶状态。pending=1仅管理员可用。截止日早于服务端北京时间当天的公告不再出现在列表、待审核或标签选项中，数据保留；旧公告默认长期有效。置顶在前，其余按created_at倒序稳定分页。groups来自启用的主数据群聊库，不调用chat_sources.json。
- `POST /api/notices/save`：title（必填160字以内）、body_html、group_name（启用群或空）、tags（最多8个，每个24字）、pin（布尔）、可选expires_on（截止日YYYY-MM-DD或null表示长期）。发布即生效，截止日含当天至北京时间23:59；过去或无效日期拒绝。新建缺省为长期，编辑缺省保持原截止日；编辑另传id/revision。作者身份来自会话，服务端净化HTML；仅内嵌PNG/JPEG/GIF/WebP图片，每图2MB、每条8图，正文16MB、请求24MB上限。data返回id/revision及变更字段。前端快捷1/3/5/7天均含发布当天。
- `POST /api/notices/delete`：id/revision；本人或管理员软删除。`POST /api/notices/pin`：id/revision/action，action为request/unpin/approve/reject。本人可申请/撤回，管理员可管理全部及批准/驳回pending申请。sales申请为pending；admin/operator置顶为approved。销售修改pending/approved公告重新置为pending，清除旧审核记录。
- 公告状态none/pending/approved/rejected与运价校验结论完全独立；待审核公告正常展示，仅不置顶。修改会增加revision，旧版本写入拒绝，不静默覆盖。

## 账号可见范围移除（5.0.0，2026-09-29）

- GET /api/operators不再返回routes/groups/match_mode/groups_list；routes_list改为航管本人条目派生的航线集合，销售/管理员为空列表（界面分别显示“—”/“全部”）。POST /api/operator/save不接受新责任群/航线设置，旧存储值只供快照兼容。
- 受控查询的旧viewer参数由服务端丢弃。航管可用ownership=mine/all：pending、audit、audit-hints、group-stats、queue-stats默认mine；rates/space及grouped默认all，价舱管理页面显式传选择。mine仅使用当前会话name匹配记录sender；销售和管理员不按ownership缩小范围。普通pol/pod/route/group等用户搜索条件保持。
- POST /api/pending/approve_all及sync接收ownership=mine/all，航管默认mine，非法值拒绝；内部_scope_viewer由服务器生成。它仅限定该次批量操作对象，不构成数据权限。角色校验、销售只读、强删限管理员保持。
- /api/account/profile不再返回旧责任字段；个人查询偏好可在完整主数据选项中选择，不是访问许可。未匹配发送人的航管“我的条目”返回空，不能用改显示名称来改变归属。

## 别名管理分页（2026-09-29）

`GET /api/alias?limit=50&offset=0` 返回 `{ok:true,data:{rows,total,limit,offset}}`。`q/group/kind` 在数据库中先筛选再分页，total为筛选后的总数；按scope_group、kind、alias稳定排序。limit上限200，offset最小0，非法整数返回ok=false。无分页参数的既有调用保留 `{ok,rows,total}` 全量兼容返回。界面只在首次打开别名标签时取首50条，翻页按需取数，搜索延迟300ms并回到首屏，旧请求不得覆盖新结果；再次打开复用当前页，显式刷新及编辑后重新查询。

## 候选噪声过滤（2026-09-29）

`/api/candidates/approve` 与候选生成使用同一业务检查；币种/费用误列、港群替换、服务/码头描述、已有映射及独立港口冲突返回具体原因并拒绝写映射。既有 `skipped_entity_conflict` 计数包含这些业务不适用项。放行不再生成agent_observation候选；人工学习扫描排除approve_diff、Agent修改、sync_from及来源被改写记录，按同一旧值/新值和独立消息统计。

## 别名候选实体冲突保护

候选生成与批准都拒绝把两个不同的启用规范港名建立为别名。`/api/candidates/scan` 的子统计增加 `skipped_entity_conflict`，与人工驳回计数分开；存量候选在批准时返回 `{ok:false,error}`，不写映射。此判断不适用于字段默认值建议。

## 独立报价产品

- 待审提交、编辑、校验和放行接收可选 `航线服务`、`目的码头`，正式查询返回 `service_name`、`destination_terminal`；两项均为原文明示文本，缺失留空。
- 两项参与待审精确/窗口去重、正式报价覆盖和柜型折叠；不同服务或码头不能互相撤换。引用更新也不能跨服务或码头选取目标。
- `/api/rates`、`/api/space` 及折叠查询支持 `service_name`、`destination_terminal` 精确筛选。旧记录两项为空时保留原内容指纹与既有口径。

## 统一业务记录

业务事实统一存入 `freight_items`。`record_type` 仅为 `rates` 或 `space` 的显示与筛选标签，字段使用两类记录的并集。

现有读取接口保持不变：

- `GET /api/rates`：返回 `record_type='rates'` 的统一记录。
- `GET /api/stats`：登录账号或采集凭据可读，统计响应包含 `backend` 字段（`sqlite` 或 `postgres`）；匿名守护检查改用 `/api/health/live`。
- `GET /api/space`：返回 `record_type='space'` 的统一记录。
- `GET /api/rates_grouped` 与 `GET /api/space_grouped`：按相同标签聚合。

## 系统运行契约

新接口统一返回 `{ "ok": true, "data": { ... } }`；失败返回 `{ "ok": false, "error": "...", "code": "..." }`。既有销售查询接口为兼容既有前端，暂不强制改形。

- `GET /api/system/health`：已登录用户可读。返回经实际数据库查询验证的连通性、数据库引擎与 API 版本；不返回数据库路径、令牌或任何业务数据。
- `GET /api/system/readiness`：仅管理员可读。基于已验证的数据库状态和非敏感部署配置，返回试点/生产就绪检查项。配置键为 `FREIGHT_DEPLOYMENT_MODE`、`FREIGHT_RELEASE_MODE`、`FREIGHT_BACKUP_TARGET_CONFIGURED`、`FREIGHT_DINGTALK_SSO_ENABLED`。这些键不承载密钥。
- `POST /api/release/decide`：仅管理员可调用。请求为 `{ "facts": { ... } }`，返回发布策略的解释性预览，不读取或写入任何业务记录。`facts` 不是对象时返回统一失败信封 `{ "ok": false, "error": "facts must be an object", "code": "invalid_request" }`。
- `GET /api/monitoring/summary`、`/api/monitoring/groups`、`/api/monitoring/runs`：仅管理员可读。统一返回监控信封；数据来自心跳的 `pull_receipt_*.json`，包含群级 DWS 返回/新增、水位前后和最近运行摘要，不含原始群消息或任何凭据。`summary` 分别返回最近一次 `latest` 与近 24 小时 `recent_24h`，不得把历史累计数标为本轮结果。群级项保留内部 `group` slug，同时返回 `group_name` 作为前端展示名，映射来自 `chat_sources.json`。

`FREIGHT_RELEASE_MODE` 默认 `review`，只输出“需要审核”的安全结论。即使未来设置为 `guarded_auto`，也仅当机械校验、来源、发送人、有效期、变价幅度和群健康度全部达标时，发布策略才会允许自动发布；本版本尚未把该结论连接到写库动作。

## 会话契约

所有业务读取和 `/images/*` 要求有效身份。无身份返回 HTTP 401 `{ok:false,error,code:"unauthenticated"}`；有效身份但权限不足返回 403 `{ok:false,error,code:"forbidden"}`。管理员可访问全部业务/配置，航管可访问销售与审核、编辑业务记录，销售只读销售查询；职责群/航线匹配是业务筛选，不构成安全权限。

`GET /api/auth/status`、`POST /api/auth/login` 的 `user` 为 `{user_id,name,role,views}`，角色取 admin/operator/sales，views 分别为 sales/ops/config/account/monitor、sales/ops/account、sales/account。`account` 表示个人账号入口；人员账号库、角色权限、报价发布人识别及安全操作记录均只对管理员开放。登录请求仍为 `{name,password}`，无新增密码长度要求。登录名不可改名，user_id 不接受客户端覆盖。

`POST /api/operator/save`、`/api/operator/delete` 仅管理员可用；新账号必须设置非空密码，编辑留空保留原密码，成功保存包含 `user_id` 和 `sessions_revoked`。角色、密码或启用状态变更撤销会话，普通资料编辑不撤销；禁止停用、降权或删除最后一个可登录管理员。`GET /api/operators` 不含密码摘要，非管理员的 bootstrap 不返回账号名册。整库 `/api/export` 因含恢复所需安全资料只允许管理员。

紧凑 `GET /api/master` 所有账号可读，`full=1` 仅管理员或采集器可读。采集器用 `x-push-token` 独立认证，可读销售基础数据和 pending，常规报价写链路为 `/api/validate`、`/api/pending`；任何审核、直接发布、正式报价改价或普通配置接口返回 403。独立受控补档/待审修复接口依据各自证据契约，不取得审核或正式报价发布权。有效账号与令牌同时出现时以账号权限为准。`/api/local-token` 仅在管理员尚未初始化时允许回环地址读取；初始化后的本机浏览器也不能取令牌。

`GET /api/health/live` 无需登录，返回 `{ok:true,data:{status:"ok",backend}}`，仅证明进程应答，不替代需登录且实际探测数据库的 `/api/system/health`。前端退出或会话失效时清除旧账号可见数据，并重新展示登录页。

登录会话以 `freight_session` 的高熵 Cookie 为唯一身份凭据，数据库只保存其 SHA-256 摘要。`client_ip` 保留为登录时的审计字段，不参与后续身份校验：DHCP、VPN、公司反向代理和网络切换不应造成用户无故退出。账号停用、角色或密码变更、显式退出、快照导入和到期仍会删除或拒绝会话。

`GET /api/pending` 返回独立待审条目。`check=passed` 与 `check=failed` 只按该条目的机械校验结果筛选；前端可按消息 ID 做视觉归组，但组内条目不共享字段或审核动作。

## 已放行记录重分类

`POST /api/record/revise`，带 `record_type` 参数。

```json
{
  "table": "rates",
  "content_key": "记录内容指纹",
  "record_type": "space",
  "fields": {
    "舱位类型": "现舱"
  }
}
```

返回统一为 `{ "ok": true, "new_keys": ["..."], "revised": 1 }`，失败时为 `{ "ok": false, "error": "...", "errors": [] }`。

带 `record_type` 时，会重跑目标分类的校验，以同表新版本写入并把旧版本标为已被覆盖；不带该字段时，保持原有的留痕字段修订行为。


## 港口多航线归属

`POST /api/master/save` 的 `table=ports` 接收 `row.routes_list: string[]`，以完整集合替换归属；`[]` 明确清空，字段缺省则保留。名称/别名必须命中启用的航线主数据，归一去重。旧 `row.route` 分隔字符串仍兼容；同时给两个字段但归一集合不同则拒绝。成功返回保留旧字段并增加 `{ok:true,data:{key,routes_list}}`，失败为 `{ok:false,error}`。

`GET /api/master` 的紧凑港口增加 `rs: string[]`，保留 `r` 兼容镜像；`full=1` 的 `raw.ports` 增加 `routes_list`。成员筛选只影响港口主数据界面，报价查询的 `route` 语义不变。

校验内部结果与新待审记录增加非阻断 `warnings`，不确定的港口归属用 `field=航线归属` 标记；不能据此把已通过刷成未通过。单条编辑只刷新该字段的提示，不删除原文覆盖率等独立提示。

## 销售高级查询参数

既有四个销售读取接口支持 `ctype=20GP,40HQ/HC,40NOR,45HQ/HC`（四档任意多选，内部 OR，兼容45HQ/45HC），`route` 与 `group` 为规范名精确匹配。
新增 `valid_since=YYYY-MM-DD`：保留有效期与所选日期及之后相交的记录，即 `valid_to >= 所选日期`；无结束边界视为不设限。旧 `valid` 仍为有效期覆盖单日，保持调用兼容。销售查询与导出使用相同筛选参数。


## 航管 ID 精确查询

既有 `GET /api/pending`（包含 grouped 兼容视图）、`GET /api/rates`、`GET /api/space` 支持 `entry_id` 精确条目编号、`message_id` 精确来源消息编号。与类型、状态和校验条件取交集，分页与 total 使用同一条件；不创建持久化组 ID、不修改校验结论。前端识别复制的 `组:<消息ID>`、`消息:<消息ID>`、`条目:<entry_id>`；API 参数传未带前缀的值。


## 航管统一 ID 查询

`GET /api/pending`（含 grouped 视图）、`GET /api/rates`、`GET /api/space` 增加 `id_query`：同一个输入按“条目 entry_id 或来源消息 ID”匹配，保留现有筛选与分页。服务端去掉显示前缀 `组:` / `消息:` / `条目:`（支持全角冒号）、DWS `msg//` 和末尾 `=` 补位符后精确比对；不作包含匹配，不改写存储 ID 或校验结果。既有 `entry_id` / `message_id` 参数保留，其中 `message_id` 同样支持消息 ID 规范化。

## 待审核同源视图

`GET /api/pending?view=source` 按来源消息筛选和分页，返回完整消息组的 `rows`。`total` 为组数，`record_total` 为匹配组内条目总数，`pagination_unit=groups`。`check=failed` 选择含未过条目的消息组，`check=passed` 选择全组通过的消息组；返回行保留各自真实校验结论。ID查询选择对应消息组并包含同源条目。默认读取仍按条目筛选、计数和分页。
# 批量驳回范围修复（2026-09-28）

## 审查链路契约（4.0.0，2026-09-29）

- 候选批准：sender+carrier+group按原范围执行，不能用override_scope_group改变证据的群。同作用域不同目标返回ok:false和conflict_ids；新批准以reviewer_note契约标记启用上下文规则，layer_action.action=context_alias_saved，不自动迁移历史批准。
- POST /api/pending新增coverage（version、ok、expected_unique、actual_unique、exact_duplicates_merged、missing、entries），entries包含entry_id/message_id/fingerprint。顶层ok说明已暂存，coverage.ok独立说明是否齐全；缺项先核查回执，不能盲目重传整批。
- GET /api/group-stats的clean_rate分母改为已审（approved+rejected），无已审样本为null；新增reviewed_total、awaiting、modified_approved、mechanical_passed、mechanical_pass_rate。这是分母和类型的breaking变更，null不能当0分，机械校验通过不能当业务准确。

`POST /api/pending/delete` 常规 `{id}` 允许审核人员删除 awaiting/rejected 条目；approved 仍拒绝，返回具体状态原因。合并视图可传 `{ids:number[],kind}`，服务端先校验全部 ID 的类型和状态，再在一个事务中整组删除，任一条不满足条件则全部回滚。删除成功后才清理 pending_shadow；权限仍限管理员/航管，采集凭据不获得删除权。

`POST /api/pending/approve_all` 只放行整组通过的运价/舱位来源消息。按归一化消息ID归组（去掉msg//前缀和末尾=）；缺少消息ID的旧条目各自成组。同源 awaiting 的 rates/space 条目共同检查，任何一条未过或不属于当前ownership范围则整组保留待审，approved/rejected 历史条目不阻挡；港群候选不在范围。每组在一个事务中逐条重新校验，任一条失败则整组业务写入回滚，仅真正失败条目更新校验错误；其他通过结论不改写。单条和卡片人工放行行为不变。

`GET /api/pending?status=awaiting&check=passed&whole_passed=1` 在默认条目列表中复用一键放行的整组条件，按钮数量为可放行条目数，不是组数；普通列表不带此参数，仍显示真实逐条结论。`remaining_passed` 为整组满足条件且仍待审的条目数；`held_by_group` 为当前范围内已通过、但因同组未过或范围外成员暂缓的条目数。两字段与实际执行共用筛选口径。

- `POST /api/pending/reject_group` 新版传 `ids: number[]`、`kind`、`note`；旧 `group_key` 调用保持兼容。编号必须是正整数，空数组不处理任何记录。
- `POST /api/pending/reject` 与 `/api/pending/reject_group` 的 `note` 可为空：空值记录为 `reject_noise`，形成 `pending_rejection:noise` 学习信号，后续不进入回炉；非空值记录为 `reject_rework`，保留原因供 Agent 按指定问题修订后重新递回人工队列。
- `ids` 优先，范围为当前来源卡中的待审条目；跨目的港、船期和柜型不再被首条组键缩窄。响应保留 `ok/rejected`，增加 `skipped: [{id,error}]`，只统计实际驳回成功的编号。
- 已处理、不存在及类型不符的记录跳过；保留校验结论、原始载荷，并记录实际审核人和原因。重复请求不得改写历史驳回日志。

## Agent 待审船期修复（2026-09-28）

既有 `POST /api/agent/pending/repair` 的 fields 白名单补充 `船期ETD`，允许用空字符串清除误把有效期起日当船期的值。仍要求 evidence、expected_signature、未审核 Agent 条目和整条校验通过；保留 entry_id、orig_payload、审核状态与字段级 edit_log，不增加审核/发布权限。

`POST /api/pending` 可选 `preserve_other_sources: true`，要求每条都有非空“来源群＋消息ID”，支持混合消息批次。开启后逐来源做原有产品去重，不撤换其他消息。完整标识的 Agent 批次默认开启；缺失来源的旧客户端/人工批次缺省保留兼容行为，显式 true 却缺来源或非布尔值均拒绝。补录后对相关来源完整待审组刷新港口覆盖提示，其他警告和校验结论不因归组被改写。

`POST /api/pending` 新增可选 `preserve_existing_source_rows: true`，只允许与 `preserve_other_sources: true` 同时使用且仅支持 rates/space。用于已修复来源补缺失产品：同源同产品命中时不删除、不覆盖，保留原 `id / entry_id / payload / orig_payload / edit_log / review_status`；只插入未命中产品。默认 false，常规完整来源更新仍保持撤旧留新。响应中 `staged/ids` 仅列本次新增，`preserved/preserved_ids` 列原样保留，`covered_ids` 为覆盖核查并集；非布尔值或缺来源拒绝。Skill 客户端统一走 `ledger_client.submit_missing()`，禁止修复后再用普通 `submit()` 重提整组。

## Agent预检修复（2026-09-28）

业务写入仍是`POST /api/validate`→`POST /api/pending`。预检失败必须先由Agent修源JSON并复检；提交前不再重复预检。pending新增响应字段`check_passed/check_failed/validation_failures`，失败详情包含pending_id、entry_id、kind、record、errors，供最终校验发生主数据变化时修复原条目。

- `POST /api/agent/master/ensure`：`{entity:port|alias, source_group, message_id, row, evidence:[{kind,reference,reason}]}`，响应`{ok,data?,error?,code?}`。港口仅可新增，须有官方HTTPS来源、国家码和用途；别名仅可新增具体群/列映射，target须为在用规范名。重复请求返回existing，冲突拒绝，不能覆盖既存主数据；新增实体的note保留来源及证据。返回knowledge_saved指明群规范日志是否同步成功。
- `POST /api/agent/pending/repair`：`{id,expected_signature,fields,evidence}`。签名为原payload的UTF-8、ensure_ascii=False、sort_keys=True JSON SHA256。仅限origin=agent且awaiting，字段白名单禁止修改来源消息、发送人、原文、图片、审核决定。机械不通过整次回滚，通过后保留entry_id、orig_payload并追加edit_log。
- 采集凭据和管理员可使用两个受限维护接口；普通航管/销售不可使用。通用master-save、审核放行、正式业务编辑仍不对采集凭据开放。



## 销售查询范围鉴权（4.1.1，2026-09-29）

`GET /api/rates`、`/api/space`、`/api/rates_grouped`、`/api/space_grouped`：销售或航管登录会话的 `viewer` 由服务端强制绑定为当前人员姓名，客户端传入的 `viewer` 无效；已登记账号的航线/群范围由数据库查询过滤。管理员可保留 `viewer` 预览；无浏览器会话的受限采集凭据保留既有查询行为。此修复只覆盖四个销售查询接口，不代表待审核、图片和所有汇总接口已完成按账号范围隔离。

## 人员账号库（4.1.0，2026-09-29）

管理员 `GET /api/operators` 在既有 `rows` 中增加 `user_id`、`has_password`、`login_enabled` 和 `publisher_groups`，后者按当前采集名单中的人工确认群派生；无账号 ID 或自动观察者不自动入库。`POST /api/operator/save` 可只填姓名创建本地身份；无密码新建时强制 `active=0`，登录仍需非空密码和启用。`POST /api/publishers/accounts` 确认成功后补录人员；如补录异常，采集决定仍已保存，响应 `data.account_sync_pending=true`，服务重启再次幂等同步。有已确认发布身份的人员不能删除，须先撤销群级确认；没有钉钉登录流程。

## 报价发布人资源 HTTP（2026-09-29）

| 接口 | 权限 | 契约 |
|---|---|---|
| GET `/api/publishers/accounts` | 仅管理员浏览器会话 | `{ok,data:{rule_version,minimum_workdays,generated_at,roster_revision,mode,groups,candidates,audit,warnings,collection_runs}}`；每个candidate增加`account_library={registered,ambiguous,user_id,login_name,role,active,has_password,login_enabled}`，明确是否已进入“人员与账号”及能否登录；按群＋账号派生身份和工作日图，不修改待审状态；航管、销售及采集凭据返回 403 |
| POST `/api/publishers/accounts` | 仅管理员浏览器会话 | `{candidate_id,action:confirm|exclude|reset,expected_revision,note?}` → `{ok,data:{revision,action,unchanged?,identity_keys?,account_created?,account_removed?,account_sync_pending?,account_cleanup_pending?}}` 或 `{ok:false,error,code}`；`confirm`和`exclude`允许省略note并分别以“管理员直接确认”“管理员直接排除”记审计，`reset`仍需依据。排除后只删除系统自动登记且未启用、无密码、无后续管理事件和会话的占位账号；真实账号保留。存在稳定群聊账号ID且无冲突时，管理员可人工确认，不受自动统计判据限制；重复提交当前决定返回`unchanged=true`且不增加名单版本或审计。操作人由服务端会话取，不信任客户端传入 |

候选 ID 是群＋账号键的派生哈希；只凭姓名或账号冲突不能执行变更。必填原因，expected_revision 必须是非负整数；旧修订返回 revision_conflict，刷新后再操作。配置锁和原子保存防止并发丢失。接口返回中的 reset 表示撤销人工决定并重新使用自动规则，非强制判为非航管；身份操作不修改历史审核结论。




## Agent工作监控HTTP v1（独立本机8796）

统一返回 `{ok,data?,error?}`；不属于业务8795写入口。契约类型位于 `ops/agent-monitor/agent_watch/core/contracts.py`。

|方法 / 路径|请求 / 返回|权限|
|---|---|---|
|GET /api/health|service、version|本机|
|GET /api/runs|最近任务状态，带signal_age_seconds/activity_age_seconds及派生health|本机|
|GET /api/events?run_id=...|该任务最近250条定位事件|本机|
|POST /api/report|Progress：run_id、source、stage、workflow_stage、activity、group、message_id、file、计数等|本机自动生成Bearer凭据|
|GET /api/control?run_id=...|action=pause/resume|本机|
|POST /api/control|run_id、action=pause/resume；只写控制请求，不中断在途动作|凭据或同源HttpOnly会话|

POST拒绝跨站控制，报告不接收原始正文、完整命令或工具输出；面板当前动作与最近业务对象分开显示。source区分runner/pipeline/agent，runner退出成功不等于业务送审成功。

监控v1.1补充：Progress可含task_name及control_ready；source新增desktop表示真实Codex应用内执行事件。control_ready=false时界面只观察，待pipeline实际接入后启用控制。会话观察使用源时间戳；历史补接不是当前在线信号。

### 4.2.0 Agent待审修复补充

既有 `/api/agent/pending/repair` 请求新增可选 `kind: rates|space`；fields补充船名航次、舱位类型与AI判断。响应仍 `{ok,data?,error?}`。签名并发检查、证据、仅未审核Agent行和禁止改来源/审核结果的边界保留，分类变动追加kind审计。

- `POST /api/agent/pending/suppress`：航管身份过滤专用清理入口，传 `{ids:number[],kind,identity_audit,note}`。仅接受 `origin=agent` 且 `review_status=awaiting` 的条目，`note` 必须以 `publisher-filter:` 开头；只移入驳回日志，不放行、不删除原始证据。


## 局域网账号边界 v4.2（2026-09-29）

- GET `/api/pending`（flat/source/grouped）、`/api/audit`、`/api/audit-hints`、`/api/group-stats`、`/api/queue-stats`与四个报价查询同用会话姓名覆盖viewer。空权限范围仍表示全量；未知/停用身份不回退全量。管理员保留范围预览，采集凭据仍走既有validate→pending。
- 航管的pending、记录修改与整组请求，任一指定或实际组内条目越界则整次403；不能把自己的条目移出范围。approve_all/sync预览由会话范围筛选，客户端_scope_viewer会被删除。新提交的撤旧、更新身份匹配与引用链追踪也筛范围，不覆盖别的群的报价。
- GET `/images/<name>`：管理员可看；销售仅可读取自己范围内已发布条目引用的图片；航管可看范围内已发布及待审引用。无引用或越界返回404，禁止通过猜文件名浏览他人图片。
- POST `/api/auth/login`：繁忙/限流均HTTP429，`{ok:false,error,code}`，附`Retry-After`秒数。`login_busy`页面有上限自动重试；`login_throttled`提示等待，不自动尝试密码。其他身份校验继续使用既有错误。
- 所有浏览器POST拒绝跨站Sec-Fetch-Site或不匹配当前协议和Host的Origin。认证请求最大8192字节，连接超时30_seconds。CLI无Origin时沿用原凭据权限。
- HTTPS会话cookie增加Secure，保持HttpOnly、SameSite=Strict、Path=/；HTTP不伪造Secure。改密码、停用或角色变化仍按既有会话撤销规则处理。


## 账号自助HTTP v4.3（2026-09-29）

新接口统一返回{ok,data?,error?}，请求类型在core/contracts.py。

|方法 / 路径|身份与输入|结果|
|---|---|---|
|POST /api/auth/register|管理员已初始化后可匿名访问；name,password,confirm_password|固定创建销售，不能传role、groups、routes等权限字段；重名400；成功200返回name、role、message，本接口本身不创建会话；网页成功后立即调用现有 `/api/auth/login` 建立会话，失败时保留用户名供手动登录|
|POST /api/auth/forgot-password|匿名，只有name|已知账号写重置申请；未知账号同样200及相同message，不透露存在性、不改密码|
|POST /api/auth/change-password|必须有效浏览器会话；current_password,new_password,confirm_password|只修改当前账号，不接受name等目标字段；原密码不对403，未登录401；成功200并清Cookie、撤销全部会话|
|GET /api/account/recovery-requests|仅管理员|{ok:true,data:{rows:[{name,requested_at,role,active}]}}，只列待处理申请，无密码/摘要|

管理员仍通过POST /api/operator/save设置密码和航管角色/业务范围，销售和航管无法调用。管理员重置密码会关闭申请且保留未提交修改的角色与范围。注册和修改共用密码计算限流，重置申请使用单独的同规格限流；三个接口均受同源和8192字节认证请求上限保护。并发注册不允许将先登记的账号变成第二个请求的密码。


## 个人账号资料（4.4.0）

### 经验液位偏好（6.4.0，2026-09-30）

`GET /api/account/profile` 的 `preferences.growth_liquid_enabled` 为兼容旧客户端继续返回 `true`，但不再是用户可配置项。`POST /api/account/profile` 仍接受布尔值以保持旧请求兼容，服务端统一归一为 `true`。顶栏账号液位始终按当前等级进度 `(total_xp-level_start)/(next_threshold-level_start)` 显示。

`GET /api/account/growth` 的 `summary` 使用循环等级：每 `10 XP` 升一级，每个循环 `50` 级。`level` 是当前循环等级，`cycle` 是从 1 开始的循环编号；`level_start` 和 `next_threshold` 始终给出当前等级区间，总经验与事件历史不重置，因此不再存在最高等级。

- `GET /api/account/profile`：Cookie 登录必需，返回 `{ok,data}`；data 含本人 `name,user_id,display_name,role,created_at,password_changed_at,routes,groups,match_mode,preferences,options,unavailable,current_ip,login_at,expires_at`，无密码摘要、他人资料或 Cookie。当前 IP 是服务器连接对端；expires_at 为 Unix 秒。
- `POST /api/account/profile`：同源、Cookie 登录，body 仅 `{display_name,preferences:{routes:[],groups:[],ctypes:[],start_tab:"rates"|"space"}}`。显示名称1–64字符，三项数组各最多50项、须来自可选池，ctypes 固定20GP/40HQ/HC/40NOR/45HQ/HC；未知字段（name/role/user_id/授权范围等）拒绝。只更新会话本人，不撤销会话。
- 错误按 `{ok:false,error,code?}`，未登录401、跨源403、无效设置400。销售仍无权读取 `/api/operators` 或报价发布人列表。
- 销售运价/舱位查询增加 `default_routes`、`default_groups` JSON 名称数组；同维度显式 `route`/`group` 优先，默认柜型仍走既有ctype筛选。服务端所有结果继续与会话授权范围相交，默认偏好不授予权限。


## 销售默认展示偏好（2026-09-30）

- 本人偏好仍由 `GET/POST /api/account/profile` 保存；`start_tab` 可为 `rates`、`space`、`dyn`、`group`。航线和来源群仅是默认浏览的显示偏好，不改变查询权限。

- 默认浏览请求对 `GET /api/rates`、`/api/space`、`/api/rates_grouped`、`/api/space_grouped` 可传 `display_mode=preferred`，附带 `display_routes` 与 `display_groups` 的 JSON 名称数组（各最多 50 项）；只有该模式生效。主动查询不带展示参数，只按用户填写的筛选条件查询；清除查询回到默认浏览。服务端继续独立叠加会话授权范围。旧 `default_routes/default_groups` 参数保留给旧客户端兼容，新页面不发送。
- `GET /api/sales/groups` 按同一展示参数和销售筛选对运价、舱位完整结果做来源群汇总，响应 `{ok:true,data:{rows:[{name,rates,space,latest}]}}`；仅销售视图授权可访问，服务器绑定查询人。
- `POST /api/sales/groups/visit`：同源 Cookie 登录、销售视图权限必需；body 为 `{name:string}`，账号编号只从服务端会话读取，采集凭据不能调用。仅允许点击当前账号可见、确有报价或舱位记录的来源群；返回 `{ok:true,data:{click_count:number}}`。运价/舱位入口点击均为该群累加一次，页面自动加载不计数。`GET /api/sales/groups` 对登录账号按累计点击次数降序排列，同次数沿用最近消息时间与群名顺序，并返回 `data.order="personal_clicks"`；旧服务没有该标记时页面不发送点击写入。筛选及授权范围不变，响应不暴露其他账号的点击数据。旧快照缺少计数表时按零次处理。
- `GET /api/notices` 在默认浏览时可传 `display_mode=preferred&display_groups=<JSON数组>`，返回所选来源群及 `group_name=''` 的全体公告；航线偏好不推断公告归属。主动选择群/标签或搜索时省略展示参数，故可查偏好范围外消息。分页总数按本次模式计算。

## 账号航线分区（4.5.0，兼容追加）

- `GET/POST /api/account/profile` 返回 `options.route_sections:[{name,routes:[]}]`，分区由可见航线的主数据region派生，区域为空归入未分区，主数据未修改。既有options.routes仍保留。
- 偏好中的ctypes保留旧客户端兼容形状，但读取时恒为空，旧客户端发送合法柜型也不再启用默认柜型；新前端始终发送空数组。销售查询ctype参数保持原语义。
- 区域全选仅将该分区各航线写入既有preferences.routes，不保存地区派生状态，不能扩大服务器授权。

## 销售列排序（4.6.0）

`GET /api/rates_grouped` 和 `/api/space_grouped` 接收可选 `sort=etd|validity|synced_at|price_20gp|price_40hq|price_40nor|price_45hq`、`direction=asc|desc`（默认asc）。`GET /api/rates` 和 `/api/space` 同时支持 `sort=price`；柜型字段在扁平接口按对应柜型价格排序，其他柜型归入缺失值末尾，便于兼容 CSV 分页请求。字段/方向不在白名单时返回 `{ok:false,error}`，不拼接用户原始 SQL。既有 `{ok,total,limit,offset,rows|groups}` 返回不变，既有权限和筛选不变。

有效期先比较 valid_from，再比较 valid_to；日期按完整 ISO 日期，柜型价格按数值，入库按 synced_at。每个排序维度缺失值在升/降序下均最后，相同值保留稳定顺序。合并接口整组排序后分页，历史保留组内顺序；移除原10000条静默截断，确保筛选结果完整。未传sort的老客户端保留原排序；销售前端默认显式传 synced_at/desc。无法解析的合并船期作为未知日期放最后。


## 管理员终端 HTTP（6.2.0）
全部接口仅admin，统一`{ok,data?,error?}`；无权限403。GET `/api/admin/accounts`支持q/role/status/offset/limit；GET `/api/admin/account?user_id=...`返回公开档案、归属统计、安全状态和签名；GET `/api/admin/account/events`支持user_id及分页；GET `/api/admin/account/records`按user_id、source(published/awaiting/rejected)分页。
POST `/api/admin/accounts`仅接收name/display_name/role，建未开通账号；POST `/api/admin/account/profile`接收user_id、section(profile/experience)、fields、expected_signature，白名单分区修改；POST `/api/admin/account/security`接收user_id、action、expected_signature，角色操作带role，开通/重置带new_password/confirm_password。服务端身份字段由认证层覆盖，不信任客户端身份。
安全action为activate/disable/enable/role/rename_login/reset_password/require_password_change/revoke_sessions；`rename_login` 额外接收 `new_name`。登录账号真正变化后撤销该账号旧会话、删除旧/新登录名下的待处理重置申请并要求用新账号重新登录；稳定 `user_id` 与 `publisher_id` 不变。公告归属按 `user_id`，运价/舱位/待审记录按 `publisher_id`，因此改登录账号不改历史归属。其他成功安全操作沿用各自动作的会话规则，不删除钉钉消息、历史报价或采集记录。role的新值与当前角色相同时、rename_login的新值与当前账号相同时均返回400，不更新安全修订、不写审计事件、不撤销会话。开通/重置要求本人改密，登录返回must_change_password；未改密时业务API返回403与code=password_change_required，只允许认证状态、改密、退出等必要入口。密码和令牌不出现在管理员查询或审计中。

## 稳定报价归属与账号绑定（6.12.0）

- `GET /api/admin/account/publisher?user_id=...`：仅管理员，返回 `{ok,data:{publisher_id,sources:[{source_key,publisher_id,source_group,sender_name,identity_kind,bound,...}]}}`。包含本账号已绑定来源及无人绑定来源；不通过姓名猜测账号。
- `POST /api/admin/account/publisher`：仅管理员，输入 `{user_id,publisher_id,expected_signature}`。publisher_id 为待绑定来源当前的报价编号；服务端会话补 actor。原子关联该来源的现有/后续价舱和待审行，返回 `{ok,data:{user_id,publisher_id,counts}}`，并写 `publisher_bound` 审计。不修改报价内容/审核状态，不授予登录权限，不允许抢占其他账号的报价身份。
- 报价输入新增可选 `来源群ID/发送人ID/发送人OpenID`，采集器从原消息回填；客户端 `publisher_id` 不用于决定归属。报价/待审输出增加 `publisher_id`，列表增加查询派生的 `publisher_bound`。未绑定不阻止收集；界面标记“待绑定航管”。
- `operators.publisher_groups` 按绑定来源派生；`confirmed_roster_groups` 仅表示旧发布人名册结果，两者不混用。账号 `publisher_name` 是显示名，不能用它与原始姓名连接记录。公告保持 `user_id` 归属。
- 同来源姓名改变仍走原 ID；只有姓名的历史关系仅迁移无歧义部分，新 source key 不按名称自动绑定。绑定导致旧报价编号合并时保留重定向，保证在途放行不恢复旧归属。

## 人员级自动归属（6.14.0）

新报价继续传 `来源群ID/发送人ID/发送人OpenID`。服务端按当前采集源内的人员 ID 查找 publisher_id，群不参与账号身份匹配；同群不同人员分别归属，换群无需请求人工绑定。

`GET /api/admin/account/publisher?user_id=...` 仅返回该账号来源，新增 `automatic:true` 和 `identity_keys`。普通前端不再提供逐群绑定按钮，旧POST绑定端点保留兼容但不参与自动采集。报价发布人页面的“已登记账号”对带ID人员按已持久化的ID关联判断，不再仅凭姓名宣称已关联。

发现尚未登记的新钉钉人员时，仅自动建立 active=0、无密码的报价身份并记录 publisher_registered 事件。此行为不是开通登录。已存在的角色、密码、启停和会话不改。历史报价不回填；此节覆盖6.12.0中“新来源必须管理员首次绑定”的旧要求。

## 条目用户修订（6.16.0）

同源补缺保留模式按完整提交指纹判断已覆盖，包含不同价格、截关、船期和附加条件。新旧港口别名及默认起运港仅按当前权威主数据归一比较，回执fingerprint仍使用实际保存内容；不覆盖人工修改，不因为别名变化新增重复报价。正式港名自带逗号保持单港；CY/ICD/by rail的内陆交付标识不能在暂存展开阶段被别名归一消除。

图片回填使用 `POST /api/agent/review-issues/settle`，仅采集令牌可调用。输入 `{source_group,message_id,pending_ids,expected_count}`，返回 `{ok,data:{resolved,pending_count}}`。服务端要求待核问题存在、回执数量完整且唯一、所有待审行原文和消息ID与问题一致，且状态为awaiting或approved；同一原文已有其他有效行时拒绝部分结案。仅关闭来源问题，不放行报价、不推进采集水位。原图完整识读及维度核查由回填器先执行；此接口本身不能证明原图识读正确。

逐图判定非独立报价时，可传`classification:"non_quote",reason,media_ids,pending_ids:[],expected_count:0`。必须提供具体分类理由和完全匹配的图片ID；存在该原文的有效待审报价时拒绝此分类，禁止用非报价掩盖已生成报价。分类依据存入已处理问题，保留源证据。

`POST /api/agent/pending/repair`可选`retain_failed:true`仅允许修正未编辑、尚未审核且校验失败的Agent行；有图片时证据必须引用该行实际原图ID，无图片的纯文本消息可用对应群规范证据修正。允许把识错字段纠正后仍保持failed，重新校验并记录审计，不改变源证据及审核状态，不能放行。默认仍要求修复后passed；人工编辑行不开放此路径。

- 登录的销售、航管、管理员可 `GET /api/item-feedback?entry_id=frt_...`，返回 `{ok:true,data:{item:{entry_id,record_type,...可反馈字段},feedback:[...]}}`。原文、图片、系统身份和时间字段不在可反馈字段内。
- `feedback[]` 及销售查询附带的反馈增加 `author_avatar_id`：只从反馈作者当前账号的头像白名单派生，未选、账号已不存在或编号无效时为 `null`；保留提交时的 `author_name`，不返回账号偏好原文。
- `POST /api/item-feedback` 接收 `{entry_id,changes:{字段:建议值},expected:{字段:所见原值},note}`，身份从会话取得；按字段存独立修订记录，不修改 `freight_items`。理由必填；同一条目同字段只允许一个有效修订，过时原值拒绝提交。返回 `{ok:true,data:{submission_id,entry_id,fields}}`。
- `POST /api/item-feedback/dismiss` 接收 `{submission_id,action:"withdraw"|"reject"}`。提交人可撤下自己提交的反馈；航管及管理员可撤下或驳回。记录只改状态，保留作者、时间及原建议；未经登录、采集令牌单独调用均无权使用。
- 销售扁平查询的每行附 `feedback:[]`，运价折叠查询的每版报价附 `feedback_by_entry:{entry_id:[...]}`，两接口都返回 `feedback_supported:true` 供旧进程兼容门禁。`old_value` 与当前正式字段不符时反馈带 `stale:true`，主行不覆盖新正式值；新建议提交时旧反馈转为 `stale`。这些显示字段不改变查询、排序、审核结论或正式价格。


## 2026-10-10 编排器只读HTTP契约（127.0.0.1:8797）

独立服务，不改变台账8795接口。GET /api/health 返回{ok:true,data:{version,mode,production_cutover:false}}；GET /api/runs 返回{ok:true,data:[轮次详情]}；GET /api/run?id=<UUID>返回{ok:true,data:轮次详情}，不存在返回{ok:false,error}及404。详情包含id/lane/mode/created/status/tasks/events；任务包含stage/status/attempt/generation/lease_until/result/error；事件包含at/stage/message。GET /为看板，所有路径核对本机Host，无POST控制或业务写入端点。
# 待审核字段同步范围（2026-10-10）

`POST /api/pending/sync` 的预览和执行都必须传入 `anchor_id`。服务端按锚点原始消息ID、原始来源群及条目分类限制候选，执行时再次核对每个ID及航管访问范围；缺少锚点直接拒绝，无消息ID不跨条目同步。`ids` 仅表示预览中选中的候选，不能扩大组范围。来源群编辑不会改变原始同步归属。同步只修改字段、重新校验，不自动放行。

## 用户行为统计与审计 HTTP（6.26.0）

- `POST /api/activity/page-open`：所有已登录账号可调用，body 为 `{page,client_event_id}`。`client_event_id` 在一次页面生命周期内稳定并唯一；账号、角色、IP 和服务端时间忽略客户端输入并从会话/连接取得。返回 `{ok:true,data:{recorded,event_id}}`，重复编号返回 `recorded:false`。
- `GET /api/admin/activity/summary?days=30&hide_admin=0`：仅管理员，`days` 为 1 至 90。返回 `{ok:true,data:{days,since,through,dau,wau,mau,search_users,events,page_opens,searches,zero_result_searches,daily,top}}`；`daily` 每日项含 `active_users/events/page_opens/searches/zero_result_searches`。
- `GET /api/admin/activity/events?days=30&event_type=&user_id=&hide_admin=0&limit=50&offset=0`：仅管理员，返回 `{ok:true,data:{rows,total,days,event_type,user_id,hide_admin,limit,offset}}`。每行含事件编号、账号、显示名、角色、类型、白名单属性、结果数、IP 和时间。
- `GET /api/admin/activity/timeline?days=30&category=&user_id=&q=&display_mode=timeline&hide_admin=0&limit=20&offset=0`：仅管理员，只读合并行为事件和账号事件。`category` 为空或为 `access/query/security/management`；`q` 匹配用户、操作人与事件内容；`display_mode` 支持 `timeline`（按时间明细）和 `account`（按账号聚合，返回 `user_id/display_name/role/event_count/latest_at`）。聚合在服务端基于全部匹配记录计算，不受明细分页影响。返回统一的 `{ok:true,data:{rows,total,days,category,user_id,q,display_mode,hide_admin,limit,offset}}`。
- 上述三个只读接口的 `hide_admin` 可选，默认 `0`（显示全部），`1` 时在计数、汇总和分页前隐藏当前或事件时标为管理员的账号，以及当前管理员账号作为操作人的事件；不删除审计记录。管理员账号已被删除、且历史事件未保存其角色时，无法仅凭现有字段追溯其管理员身份。
- 销售端主动查询在既有 `/api/rates`、`/api/space`、`/api/rates_grouped`、`/api/space_grouped` 请求中附 `activity=intentional`。该参数只触发旁路行为记账，不改变筛选、排序、分页或返回结构；统计写入失败不改变主请求结果。

## 管理员运行监控 HTTP（6.40.0）

- `GET /api/admin/monitor/api-events?days=7&limit=50&offset=0&method=&path=&failures=0`：仅管理员。返回 `{ok:true,data:{rows,total,days,limit,offset,...}}`，每行仅含服务端时间、账号/角色、方法、路径、HTTP状态、耗时和来源IP；`days` 为1–90，`limit` 为1–200，`path` 必须是完整 `/api/` 路径。
- `GET /api/admin/monitor/api-summary?days=7&method=&path=&failures=0`：仅管理员，返回范围内调用数、失败数、账号数、平均耗时及请求最多的接口。
- `GET /api/admin/monitor/health`：仅管理员，只读返回应用进程在线状态、运行时长及业务数据库连接探测结果、数据库类型、探测耗时和错误类型；不执行修复、备份或业务写入。
- 接口日志在业务请求响应前尽力写入 `api_request_events`；日志故障只写告警日志、不改变业务响应。仅保存请求路径元数据，不保存查询参数、请求/响应正文、Cookie、Token、密码或密钥。监控查询和健康探测端点自身不记入接口日志，避免自增循环。
- SQLite 由 `manualdb.init_schema` 建表，PostgreSQL 由启动期 `ensure_identity_schema` 幂等建表和索引。新快照包含接口日志表；旧快照缺少该表时按空历史兼容导入。

## 报价表使用行为（6.41.0）

- `POST /api/activity/quote-usage`：所有已登录账号可调用，服务端从会话和连接绑定用户、角色、IP及时间。body 严格限定为 `{action,quote_kind,item_count,field_type}`；报价表类别为 `rates/space/both`，操作为打开、选入/移出条目、手工增删行、清空、恢复格式、复制、编辑字段或调整字段，条目数为0–500，字段类别为价格/港口/日期/航线/航程/船司/船名/柜型/备注/自定义/格式/其他。任何多余字段均拒绝。
- 行为记录进入管理员“运行监控 → 用户行为”的“报价表”分类，并参与活跃账号与事件总数；不计入业务查询数。字段编辑按短暂停顿合并上报，仅记录字段类别与数量，不记录价格、港口/船司值、备注内容、单元格值、行/条目编号或报价正文。上报故障不影响本地报价编辑。
- SQLite 与 PostgreSQL 初始化 `quote_usage_events` 及索引；新快照包含该表，旧快照缺少时按空历史兼容。该记录只覆盖功能上线后的操作，不回填历史报价草稿行为。


Agent监控台8796新增GET /orchestrator（同源只读看板）、GET /api/orchestrator/runs（{ok:true,data:[轮次详情]}，读取失败为{ok:false,error}及500）、GET /api/orchestrator/health（{ok:true,data:{available,production_cutover:false}}）。编排器视图无POST控制；不替代原/api/runs、/api/control。

## Prospector 用户授权查询

新增 HTTP 授权码+PKCE、刷新/me/退出与 Bearer 只读调用，精确请求和返回、高级筛选、分页及 TLS边界见 [prospector-user-query-contract.md](prospector-user-query-contract.md)。固定 client_id=prospector，令牌不允许进入 renderer；既有浏览器/采集器权限不变。

## 2026-10-10 生产编排器命令与监控契约

CLI成功统一{ok:true,data}：enqueue-production返回run_ids，start-worker返回started/healthy/pid（已运行时不重复启动），resume返回resumed_tasks或recheck_inventory。原status/只读HTTP保留。Store详情增加message_id、children:[{id,lane,failed,waiting}]；父等待派生WAITING_CHILDREN，子失败派生BLOCKED，不改业务审核结论。

8796 GET /api/orchestrator/health新增worker_alive；production_cutover从已核实接管标记读取。编排视图默认隐藏开发验收轮次，支持查看子消息与逐阶段回执；仍无网页写入/批准/删除操作。

业务继续使用现有POST /api/validate → POST /api/pending（preserve_other_sources=true,preserve_existing_source_rows=true），GET /api/pending分页读回；失败来源POST /api/review-issues。无新台账写API，禁止绕过人工放行。

## 2026-10-12 编排器只读健康补充

GET /api/orchestrator/health仍返回{ok,data?,error?}。data保留available/production_cutover/worker_alive，新增business_state（running/blocked/waiting/idle）和queue。queue包含batches/messages（各轮次状态计数）、running_tasks、lane_blocked_batches、last_progress_at（真实生产成功阶段epoch秒，可为null）。仅group:*批次和message:*消息计入，历史验收保留在最近列表但不计生产健康。心跳存活不代表业务推进；GET /api/orchestrator/runs列表仍限最近100条。

本地维护CLI新增resume --reread-source，不能与--recheck-inventory同时使用；仅修复代码后人工授权恢复未送审业务失败，保留原证据和业务重读预算，不由定时任务调用。

编排器的既有review-issues结案POST，仅在本地成功报告回执存在且完整源读回证明成立时调用；历史无回执保留原问题，不扩展Agent GET权限。结案不批准报价，失败不推进水位。
