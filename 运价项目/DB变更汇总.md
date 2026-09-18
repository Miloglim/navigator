# 运价台账 · 自主数据库改动汇总

> 约定：凡我在**未逐条请示**下对「台账库 / 主数据 / 采集配置」做的改动，都记在这里备查、可否决。日期倒序，最新在上。每条给「缘由」和「回退方法」。

---

## 2026-09-17（前端：功能区默认折叠——撤销后按新双视图结构重做，已上线）

**经过**：你先要求"航管端每个卡片（＝功能区组件，带标题的区块）可折叠且默认折叠"。第一版我误做成"报价工单卡折叠"→ 回滚重做为功能区折叠 → 你改主意要求撤销（纯删 60 行、净变化归零）→ 等另一会话的视图拆分落地（`OPS_TABS` 5 ＋ 新 `CFG_TABS` batch/admin/alias/shadow ＋ `view-config`）后，**按新结构重做并重新上线**。

**最终实现**（只改 `Database/web/index.html`，未动台账库/主数据）：

1. `zoneFoldInit / zoneFoldAll / zfSetAll / zfSetOpen / zfToggle`：每个 `.zone-title` 及其后直到下一个标题的兄弟收进 `.zf` 容器，**默认只留标题条**，点标题展开；标题条上的按钮（跑一次扫描 / 全库体检 / 刷新等）不触发折叠。
2. 扫描按 **`[id^="panel-"]`** 而非 `.ops-panel` 类名 ⇒ **航管端 5 个面板 + 配置端 4 个面板全覆盖**，将来再拆视图也不用改；`zfSetAll` 只作用于当前可见面板，两排页签各加「全部展开 / 全部折叠」。
3. 开合状态记本地 `localStorage['freightboard.zones']`（键＝面板 id + 标题前 24 字），刷新与切视图后保持。

**实测**：`js_check`（3473 行）/ `web_check` 通过；与重做前快照逐行 diff＝**纯新增 73 行、删除 0 行**（没吃掉另一会话任何一行）；`foldKey` 定义数仍为 1；线上页面已含新代码，刷新即生效。

**事故留痕（本轮沉淀，务必记住）**：第一版曾在同一个 `<script>` 里定义 `function foldKey(box, el, i)`，**覆盖了另一会话 13:47 写入的 `function foldKey(s)`**（别名索引 `buildAliasIndex`、联想 `acQuery`、`normPortTok` 都在用）——`web/index.html` 只有一个 `<script>`，同名顶层函数**后者静默覆盖前者**，不报错地改坏别人的功能。现已全部改用 `zf*` 前缀。规矩：往这个文件加函数前先 `grep 'function 名字(' web/index.html`；改完与改前快照逐行 diff，确认只动自己的行。教训已写入 `Skill/.../references/learnings.md`。

**快照**：`index.html.bak-foldfix-142909`（首次折叠前）、`index.html.bak-unfold-*`（撤销前）、`index.html.bak-zone-*`（重做前）。回退＝删 CSS 的 `.zf*` 块、JS 的"功能区折叠"块、两排页签的按钮 span、BOOT 处 `zoneFoldAll();` 一行。

---

## 2026-09-12（第八轮：数据模型四项落地——ports.route / alias_scope / 港群候选 / 显示名）

**缘由**：你回"开始推进改动"，按定稿的四项落地。

**动作**：

1. **`ports.route` 列（航线是港口的子属性）**：DDL + `_migrate` 幂等补列 + `MASTER_COLS` 登记（主数据表单可编辑）。
   - **种子只用港群成员**（高置信）：写入 **41 个港**（WCSA→南美西、ECSA→南美东、WCCA→中美洲、CAR→加勒比、MEX→墨西哥）。
   - **明确不用 `ports.source` 派生**：实测它的区域 token 粒度粗且有错——`Ambarli` 标"地中海"（业务是地东）、`Gdansk` 标"北欧"（业务是波罗的海）、`Buenaventura` 标"加勒比"（实为南美西）。若拿来覆盖，会把 **617 条**本来填对的航线改错。冲突与无种子的 299 行进清单 `Database/主数据-航线种子待确认-299行.md` 等人工勾。
   - **NEB 港群 8 港挂起**：30 条子线里没有"北巴"这一档，不猜，`route` 留空。
2. **航线派生规则（闸门）**：`Master.derive_route(pod)`——单港查 `ports.route`，给港群名则看成员航线是否**共同一致**（不一致返回空，不猜）。接入两处：`resolve_core`（预检/卡片显示一致）与 `expand_cn`（按拆分后的每个目的港补）。**只补空、绝不覆盖已明示值**，理由同上 617 条。
3. **`alias_scope` 表（识别名带"列 + 群"作用域）**：主键 `(alias, kind, scope_group)`，`kind ∈ pol/pod/carrier/route`，`scope_group='*'` 为不限群的全局覆盖。`Master.scoped()` 在 `lookup_port` / `master_side` / `split_carriers` 里**先于**全局索引命中；`/api/master` 新增 `aliasScope` 输出、`ports` 紧凑输出加 `r`（route）。**真表当前 0 行**——还没有需要按群限定的别名，机制先就位。
4. **`group_suggestion` 港群候选通道**：`validate_group_suggestion()`（成员逐个命中 `ports.name`，虚构名整条打回）→ `stage_pending` 专用分支（不展开、按群名撤旧留新）→ `approve_pending` 专用分支（放行才 `master_save` 写 `port_groups`，**只补缺成员、不覆盖人工维护的标签/特例港**）→ `list_pending` 与 `_kind` 放行该 kind → 前端「港群建议」第三页签 + 专用卡片（成员 chips + 出处逐字原文 + 放行/驳回，复用事件委托）。
5. **`display(name)` 剥国家后缀（前端）**：新增 `dispName()`，销售端结果表两格、待审分组行、归一 chips、港群成员 chips 都改用它；原值进 `title` 悬停；详情面板与「主数据→港口」维护界面仍显示规范名原值。
6. **文档与 skill**：`agent-input-contract.md` 增补第三类 kind 的字段与放行语义；`extraction-rules.md` 加规则 **25**（港群/航线归属只提候选、不猜值）；源→运行副本已同步（30 文件对齐）。

**实测**：`js_check` / `web_check` / `validity_rule_check` / `manual_check` 四套 **exit 0**；`manual_check` 用例 89 → **100**（新增：航线派生 3 条、作用域别名 2 条、港群候选 3 条，另含清理断言）。派生抽查实测：`Santos`→南美东、`MEX`→墨西哥、`Santos`+明示"墨西哥"→保持墨西哥、`NEB`→留空不猜。线上 `/api/master` 已带 `aliasScope`（0 行）与 41 个 `route`。服务现 **pid 13064**。

**过程中我自己踩的两个坑（都已修，记着别再犯）**：①`sqlite3.Row` 没有 `.get()`，`master_payload` 与放行分支各崩一次（同类坑一天内第二次）；②编辑 `extraction-rules.md` 时把规则 24 的标题吞了、编号错乱，已复原成 24/25 两条。

**未接线（诚实说明）**：`alias_scope` 的**写入通道**（前端管理界面 / 专用接口）没做——表是空的，且 `master_save` 走单主键、这张表是复合主键，硬塞会破坏它既有的通用逻辑。等第一条真实"按群限定"的别名出现时再一并设计（人工可读来源已在 `references/群聊规范/*/规范.md` 的缩写表里）。同理，前端联想候选的"按当前来源群置顶专属别名"也未接——空表下写这段代码无法验证，属于猜。

**回退**：改前整库快照 `Database/export-pre-主数据优化-172910.json`（含 route 之前的状态；`--import` 恢复）。代码回退＝删 `ports.route` 相关（DDL/迁移/MASTER_COLS/`port_route`/`derive_route`/两处派生调用）、删 `alias_scope` 表与 `scoped()` 及其三处调用、删 `GSUG_KIND` 与两个专用分支及前端第三页签与 `gsCardHtml`、删 `dispName` 并还原三处显示。`ports.route` 的 41 行值与待确认清单文件删掉即可，不影响业务表（仍 0 行）。

---

## 2026-09-12（第七轮：数据模型定稿 + 逐群业务规范目录）

**缘由**：你把方向定成"精简结构 + 理清 规范名/识别名/港群/航线 的关系"，给出四层模型与航线两级分类；四条待定点你逐条给了口径（显示不带国家括号、识别名加"群"作用域、航线是港口子属性、港群只提候选）。

**动作**（**本轮未改台账库、未改主数据、未改闸门代码**，只加文档与采集侧知识）：

1. 新增 `Database/docs/data-model-spec.md`：四层名词表、两条 ASCII 图示（名字→规范名→显示 的映射链、一条记录从群到看板的路径）、四条定稿口径、表职责与"本轮定稿要动的点"、尚未定稿项。
2. 新增 `Skill/freight-rate-heartbeat/references/群聊规范/`：**11 个群各一个中文目录 + `规范.md`**，含定位 / 报价格式（贴逐字实测样例）/ **缩写表（带"列 + 群"作用域）** / 已知坑 / 解析要求；父级 `README.md` 写检索规则与 目录↔群名↔slug 对照。**无实测依据的一律标「待补」，不编造**（天津、大连两群尚无逐字样例；CMA/EMC/PIL 与欧洲群的"两个价格对应哪两档柜型"顺序待确认）。
3. `SKILL.md` 升 **5.1.0**（功能新增）：目录导览登记新目录、主流程第 3 步加"判定某群消息前先读该群规范.md，标待补的禁止猜"；`references/Instruction.md` 同步登记，并把 extraction-rules 条数 16→24 更正。
4. **源→运行副本已同步**：30 文件对齐，运行副本 SKILL.md 已是 5.1.0，技能描述已更新。

**四条定稿口径（已写进文档，代码待下一轮）**：① 界面显示一律裸英文名，`(CO)` 这类消歧后缀只活在数据层，具体所指看详情原文；② 识别名带作用域，新增 `alias_scope(alias, group, kind, target)` 承接"某群里 X 指 Y"（实例：`NB` 在起运港列 98 次全是宁波，与"北巴"无关）；③ 航线是港口的子属性 → `ports.route` 列（可多值，`Balboa`/`Port Said`/`Aqaba` 这类天然跨线），族从 `routes.region` 派生，记录上的 `route` 改由 `pod` 派生覆盖；④ 港群 agent 只提候选（新增 `kind=group_suggestion` 待审类型），人工放行才改字典。

**待落地四项（下一轮需你点头顺序）**：`ports.route` 列 + 330 行 `source` 种子派生；`alias_scope` 表 + 前端群作用域；`group_suggestion` 待审类型；`display(name)` 剥后缀。另有 `schema-map-and-slimming.md` 里的 A 类删列 / B 类停止双写 / `operators` 启用或摘除。

**回退**：删 `docs/data-model-spec.md`、删 `references/群聊规范/` 整目录、`SKILL.md` 与 `references/Instruction.md` 的新增段落回退并改回 5.0.0、运行副本用 `%TEMP%\skill-runtime-backup-*` 还原。数据层零改动，不需要快照。

---

## 2026-09-12（第六轮：结构梳理文档 + 航线两级结构定稿）

**缘由**：你把方向从"补港口字典"转到"精简结构 + 理清 规范名/识别名/港群 关系"，并给出航线两级分类（6 族 31 子线），四条结构争议你回"以群聊中发生的真实业务的为准"。

**动作**：

1. **新增 `Database/docs/schema-map-and-slimming.md`**：识别名→规范名→港群→业务值的关系图、业务表 29/30 列的四组分类（核心值/时间/溯源/系统）、带实测数字的冗余清单（A 可删 / B 停止双写 / C 派生 / D 未启用 / E 复合串）、精简后的目标结构与落地顺序。
2. **航线重排为两级（主数据改动）**：`routes.region` 复用为"族"，值换成 6 个「××线」，**零 DDL 变更**；子线 25 → **30** 条。
   - 改名 3 条：`非洲东岸/南岸/西岸` → `东非/南非/西非`（实测 0 条在用，旧名进别名）。
   - 新增 5 条：`亚得里亚海`(68 命中)、`北非`(148)、`港澳台`(49)、`远东`、`中亚`（后两条 0 命中，只建名不配映射）。
   - 别名增撤：`欧基港` + "欧洲"；`加勒比` + "巴拿马/科隆/colon"；`中美洲` − "巴拿马"；`南美东/南美西/东南亚/中东/印巴/日韩/内陆河运` 撤出**国家名**别名；`北欧` 撤出 `hamburg/rotterdam`（港名当航线别名是歧义源）；`南美西` 撤出 `SAWC`。
3. **四条争议按语料定**（2850 条：群原始消息+识图+判定+待审）：欧基港 807/49 源且与 地东/黑海 并列出现 → "欧洲"不合并、降为别名；地东 435/58 源 → 独立保留；巴拿马 231 次全是目的港与运河路径 → 不建子线；澳新 **0 命中** → 不拆。详见该文档第 6 节。

**我自己引入又修掉的一处**：给 `中美洲` 加别名 `ca` 时没注意 `加拿大` 也用 `ca`，`master_health` 立刻报 `ca→2港` 歧义死别名——已撤 `中美洲` 的 `ca`，复检归零。教训：加别名前该先跑一遍歧义检查。

**后果（要你知道）**：`SAWC` 撤出 `南美西` 别名后，那 **28 条待审**从"靠别名自动归一"变成 `航线「SAWC」不在主数据里` 交人工——与 `DB变更汇总.md` 里你早先"SAWC 是服务码当航线，可留空或人工改"的判断一致。要恢复自动归一，把 `SAWC` 加回 `南美西` 别名即可（一行）。

**实测**：`js_check`/`web_check`/`validity_rule_check`/`manual_check` 四套 **exit 0**（30 条子线、6 族全挂齐、待审航线值除 SAWC 外全部命中）；`master_health` ok=True，`routes 歧义死别名` 归零，总问题条数 51 → 49。服务 pid 40808。

**回退**：本轮改动全在 `routes` 表内（族值、改名、别名），无 DDL、无业务表写入。回退＝按上文逐条反向 `master_save`（旧名 `非洲东岸/南岸/西岸` 重建、删 `东非/南非/西非`、别名原样填回、`region` 改回原 10 值）；改前整库快照 `Database/export-pre-主数据优化-172910.json` 可整体回退（会连带回退第五轮的主数据清理，注意）。

---

## 2026-09-12（第五轮：主数据优化 P0+P1，按你"全部进行优化"授权）

**缘由**：主数据体检查出别名里 302 条死键、`country` 白进索引、1 行括号不闭合坏数据、港群成员无保存校验、`active` 完全没用起来。你回"按你的全部进行优化"，即 P0 全做 + P1 的体检/保存校验 + P1-7 只出清单；P2 三条涉口径的不动数据、只把约定写进文档。

**动作**：

1. **P0-① 清死别名（数据）**：336 行 `ports.aliases` 删掉 **338 条**区域词/国家码条目（3395→3093），每行在被改行的 `note` 追加"清理死别名：X/Y"备查。理由：这些词折叠后指向 30-48 个港，被歧义规则整条丢弃，写了不生效。
2. **P0-② 修坏行（数据）**：`Kangerdluarssoruseg (Færingehavn` → 规范名 `Kangerdluarssoruseg`，注记 `Færingehavn` 进别名。
3. **P0-③ 索引键源（代码）**：`port_idx` 不再吃 `country` 与 `city_pinyin`（国家码每个港都有必然歧义；拼音与 `aliases` 双写、净贡献仅 3 个键）。
4. **P1-④ 港群保存校验（代码）**：`master_save` 对 `port_groups` 的 `members`/`specials` 强制校验命中 `ports.name`，命中不了直接拒（报"成员港不在 ports 规范名里…"）。
5. **P1-⑤ 主数据体检（代码）**：新增 `manualdb.audit_master()`，`/api/audit` 返回 `master_health`，自动查七类：成员断链、各表歧义死别名、规范名折叠重复、括号不闭合、`locode_ref` 缺失、`active` 未收口、`senders` 主键嵌电话。只报告不阻断。
6. **P1-⑦ 长尾收口清单（只出清单，未改数据）**：`Database/主数据-auto长尾收口候选-792项.md`——`source='auto'` 且从未被港群/待审/业务表/航线别名引用的 792 行，等你勾选后置 `active=0`（不删行、可恢复）。
7. **P2 文档化（未改数据）**：`docs/field-governance-spec.md` 新增第 6 节「主数据维护约定」五条规矩（消歧写法、别名只放唯一指向本行的写法、成员必须命中规范名、停用不删行、agent 补船司必带 auto 溯源）。`senders` 拆列与 `routes.countries` 属 P2 未做。

**撤销的一条（我自己报错了）**：原计划"合并 `Veracruz`/`Vera Cruz`"。查证后**两行是两个不同港**——`Veracruz`(MX, MXVER, 韦拉克鲁斯, 14 条待审引用它) 与 `Vera Cruz`(BR, BRVZC)，且真实 `fold()` 保留空格、二者并不冲突。我先前那句"裸词 veracruz 现在命中不了"是用自制探针的粗糙折叠得出的**错误结论**，实测 `MXZLO→Manzanillo`、`veracruz→Veracruz` 都正常。已把"体检口径必须与索引 fold 一致"写进代码注释，避免下次再犯。

**实测**：`js_check`/`web_check`/`validity_rule_check` 全绿；`manual_check` **89 项全通过**（新增 6 项：`master_health` 结构、error 级为 0、假成员被拒、被拒不写脏、`MXZLO` 归一仍有效、区域词不再被误归一）。`master_health` 现为 ok=True / 6 类 warn / 51 条，其中**待你人工消歧**的：`'Aqaba / Aqaba`、`Vitoria / Vitória`（规范名折叠重复）、`卡尔德拉→3港`、`奥克兰→2港`、`亚喀巴→2港`、`hamburg/rotterdam→2航线`（北欧与欧基港争这两个港名）。`深圳→4港` 看着吓人但实际不受影响——`深圳` 本身是 pol 港群，群名匹配先于港口匹配。服务现 **pid 40808**。

**回退**：改前整库快照 `Database/export-pre-主数据优化-172910.json`（`manual_server.py --import` 恢复）。别名清理与坏行修复均可从该快照逐行还原（每行 `note` 里记了删掉的别名）；代码四项回退＝删 `audit_master` 与其接线、删 `master_save` 的 `port_groups` 校验块、`port_idx` 键源补回 `"city_pinyin", "country"`。

---

## 2026-09-12（第四轮：舱位类型十一档 + record_builder 失真 + 该图全量重采 + skill 运行副本同步）

**缘由**：上一轮遗留三条由我定口径（你回"你自己加／改／要"）。那张宁波群图 39 行里 33 行是「跳港/空班/首航」，八档装不下，只能整段不入库——枚举缺口不补，采集就永远丢数据。

**动作**：

1. **舱位类型 8 档 → 11 档**：新增 `空班`（整班取消）、`跳港`（船照开但不挂这港）、`首航`（新航线首班）。三处定义一次改齐并加了一致性校验：`manualdb.SPACE_TYPE_ENUM`、`ledger_contract.json.space_type_options`、`web/index.html` 的 `space_type` opts；文档同步改 `agent-input-contract.md`、`SKILL.md`、`llm_output_contract.md`、`config/Instruction.md`。**台账闸门新增舱位类型白名单校验**（此前只由 skill `pipeline.py` 自守），实测现存待审只用八档 → 加白名单零误伤。
2. **`record_builder.py` 两处失真修正**：①模板映射表把 `跳挂/omit` 归到「撤载改期」、`BLANK SAILING` 也归到「撤载改期」→ 改成 `跳挂/omit→跳港`、`BLANK/停航→空班`（撤载改期的本意是甩柜/退舱改期，混用会让销售查不到空班）；②COSCO 华南订舱指南模板把 **`(未注明，WSA2线)` 塞进 `目的港`**（就是上轮那批脏目的港的来源之一）→ 改为 `目的港` 留空、服务码写进 `备注`。
3. **主数据订正**：`ports` 新增规范名 `Manzanillo(PA)`（country=PA，note 写明"CAR 成员一直写它但库里只有墨西哥那个 Manzanillo，成员名对不上规范名 → 双向展开失效"；**未编 LOCODE**，留人工核）；`MEX` 港群成员 `Manzanillo(MX)` → 规范名 `Manzanillo`。ports 1189→1190。
4. **数据订正与重采**：13 条「原文写空班却填撤载改期」的待审行翻正为 `空班`；那张图剩余 33 行按定稿口径提交 60 条记录 → 闸门展开 **179 条待审**（跳港 40 记录、空班 12、加班船 7、首航 1，合并船司逐司展开；`9.6和9.13` 拆两条；南美西墨西哥段每行存 WCSA+MEX 两条）；删除旧版 flawed 遗留 **12 条**（目的港全空 + 9 条带占位符船名，内容已被新集覆盖）。
5. **skill 运行副本同步**（`~/.qwenworkcn/skills/freight-rate-heartbeat`）：里面还是 **v4 世代**（`init_ledger.py`/`check_spec.py`/`price_history.py`/`spec.json`/`rules.md` 等 10 个已废弃文件），本轮铁律根本到不了定时任务。已备份到 `%TEMP%\skill-runtime-backup-20260912-121051`，10 个废弃文件送回收站，源→运行 18 个文件全量覆盖（技能列表描述已变成 v5 口径，确认生效）。

**实测**：`js_check`/`web_check`/`validity_rule_check`/`manual_check` 四套 **exit 0**（manual_check 83 项全通过）；枚举三处一致性脚本校验 `True`；待审 2137 → **2300**（通过 1842 / 未过 458），该图待审 195 条中 184 通过、11 未过（6 条占位符船名遗留、5 条原图确实没写船名）。服务现 **pid 30716**。

**回退**：改前整库快照 `Database/export-pre-十一档-120541.json`（`--import` 恢复）；代码基线 `rollback-20260911\`；十一档回退＝删 `SPACE_TYPE_ENUM` 里三项与各文档/前端 opts 同步回八档；主数据回退＝`/api/master/delete` 删 `Manzanillo(PA)`、MEX 成员改回 `Manzanillo(MX),Lázaro Cárdenas`；本轮新增 179 条待审可按 `消息ID=msgh7uDiyJuT7Qu/wIhxwRSBA==` 且备注含"重采自图"批量删除；运行副本回退＝用上面那个 `%TEMP%` 备份目录覆盖回去。

---

## 2026-09-11（第三轮：多值展开闸门 + 船司主数据兜底 + 采集铁律 + 宁波群图重采）

**缘由**：宁波舱位群那张「2026年9月10月空班/加班船计划」图暴露三类失真——合并船司被"取第一家"（`EMC/COSCO`→`EMC`，伴生船司丢了还显示通过）、表头没有目的港列时把 ETD 格说明文字塞进 `目的港`（产出 `(未注明，只去Manzanillo`、`ESE)` 碎片）、OCR 只认 14 行而实际 39 行且无提示。经你逐条确认（展开矩阵＝对、WCSA+MEX 双条与港群标签加英文＝对、未知船司自动补主键规则＝好）后落地。

**动作**：

1. **台账闸门 `manualdb.py`**（三条入口共用，`/api/validate` 预检不写主数据）
   - `expand_cn` 升级为多维展开：船司按 `CARRIER_SEP` 拆→逐个归一 `carriers`→**全命中才展开成 N 条**；柜型走新分词器 `ctype_list()`（`40HQ/HC` 自带斜杠，不能裸切），多档必须**逐档配价**，配不上就把价留空交闸门打回（绝不复制同一个价）；起运/目的/船司/柜型做笛卡尔积，去重键加进船司与价。
   - 新增 `ensure_carrier()`：未知但形态像船司缩写（`CARRIER_SHAPE`）的 token **自动补一条 `carriers` 主键**，`note` 写"auto：待人工复核＋来源群/发送人/消息ID/原文片段"，只补不覆盖，写完 `invalidate_master()` + 就地重建索引。仅 agent 暂存路径允许；人工/批量入口 `allow_carrier_add=False`。
   - 脏值硬闸：`目的港`/`起运港` token 未命中主数据且含括号/`via`/`Base Port`/`只去`等说明词 → 单独报"含说明文字"（判脏前先剥 `(CO)` 这类两位国家码消歧后缀，避免误报）；`船名航次` 是占位符（`(未注明船名)`/`未注明`/`待定`/`无`）→ 按未填处理；柜型加三档白名单硬闸，**并取消 `norm_ctype` 把 40 开头悄悄归成 `40HQ/HC` 的行为**（`40GP` 普柜不再被改成高柜，直接打回）。
   - `MAX_EXPAND=60`：单条消息展开超限不再逐条暂存，只落一条"超过上限"failed 工单（`stage_pending` 与 `push_with_review` 都接）。
2. **主数据（真库实际改动）**：港群 `MEX.label` `墨西哥`→`墨西哥/Mexico`、`WCSA.label` `南美西岸`→`南美西岸/WCSA`（让英文 Mexico 能在筛选联想里弹出来；成员串未动）。**`carriers` 真库仍是 50 条、没有 auto 新增**——本轮提交的行船司都已登记，自动兜底只在回归副本里验证过。
3. **skill 文档**（`运价项目/Skill/freight-rate-heartbeat/`，源副本）：`extraction-rules.md` 新增规则 **19-24**（合并船司原样传串禁取第一家／段名即港群且合并段存两条／多档柜型逐档配价／服务码与占位说明不进港口字段／占位符不是值／多船期拆条与被放弃的行必须交代），并**修正规则 16 我上一轮写过头的断言**——带国家括号的写法只有已登记成规范名的 7 个港能用（`Cartagena(CO)`、`Caldera(CR)` 等），`Manzanillo(MX)` 命中不了（库里只有一个 `Manzanillo`=MX，别名 `MXZLO`；巴拿马那个未登记，而 `CAR`/`MEX` 港群成员串里却写着 `Manzanillo(PA)`/`Manzanillo(MX)`＝主数据不一致）。`llm_output_contract.md` 加第 7-12 条，`learnings.md` 加 3 条纠错并把旧的"台账不校验柜型枚举"标注失效范围。
4. **这张图重采**：`ocr_cache/lQLPJw0hV0….json` 由 14 行补成 **39 行**（逐字、带分段→港群映射与 6 条备注）；删除碎片工单 **4603/4604/4605**；按新口径提交 **8 条**（ONE/HMM/TSL/ZIM×2/COSCO×2 走 `MEX`、EMC OGEC 走 `CAR`；`9.6和9.13` 拆两条；`只去Manzanillo/Lazaro/ESE` 用明示港覆盖段默认，`ESE` 不当港口）。结果 **3 通过 / 5 未过**，未过全部是"船名航次不得为空"——原图确实没写船名，按新口径留空交人工而不是填占位符。

**实测**：`manual_check` **83 项全通过**（新增 18 项闸门用例：合并船司拆 4 条、三档三价配对、三档一价逐条打回、`40GP` 不被归成高柜、脏目的港与消歧后缀两种文案、占位符船名、预检不写主数据、未知船司 auto+溯源、超限只落一条、用例自清）；`js_check`/`web_check`/`validity_rule_check` 全绿。影响面：新规则下现存 2132 条待审只有 **22 条**从 passed 翻 failed（全是占位符船名），柜型闸与目的港闸零误伤。真库待审 2132→**2137**（未过 456）。服务重启两次，现 **pid 23108**。放行时 `approve_pending` 会重新过闸门，所以旧行显示的"通过"是过期徽标、脏值放不进去。

**遗留（要你定）**：① `舱位类型` 八档里没有 **空班／跳港／首航**，而这张图 39 行里 33 行是这三类——所以这 33 行本轮**没入库**，另有 **13 条**旧待审行把"空班"就近填成了"撤载改期"（现仍显示通过），要么加档要么给映射规则；② `CAR`/`MEX` 成员串里的 `Manzanillo(PA)`/`Manzanillo(MX)` 与规范名不一致，要不要补 `ports` 行订正；③ skill **运行副本**（`~/.qwenworkcn/skills/freight-rate-heartbeat`，SKILL.md 还是 9-7 的）比源副本旧，本轮铁律只写在源副本，**要同步/重装才对定时任务生效**。

**回退**：`manualdb.py` 回退＝`git` 无，按本条目逐项反向删（新增常量与 `ctype_list`/`ctype_ok`/`ensure_carrier`/`split_carriers`/`rebuild_carrier_idx` 三处方法、`expand_cn` 与 `resolve_core` 的改动、两处 `MAX_EXPAND` 护栏）；港群标签＝`/api/master/save` 把 label 改回 `墨西哥`/`南美西岸`；这张图＝删 4662-4669 八条、恢复 4603/4604/4605 需从快照 `Database/export-pre-重跑-101520.json` 里取；`ocr_cache` 旧版内容在该快照同批的 `.work` 里没有副本，但可由 `rows` 字段前 14 条还原。改前整库快照已存 `Database/export-pre-重跑-101520.json`（3.3MB，恢复走 `manual_server.py --import`）。

---

## 2026-09-11（第二轮：清单②③＋两个回归脚本失守点）

**动作**（全部只改代码，未动台账库/主数据/采集配置）：

*前端 `web/index.html`*

1. **分页 200 → 50，并提成常量 `PD_PAGE`**（原先 `loadPending`、`renderPdPager` 里三处魔数 200）。缘由：一页 200 行重画要编译约 600 个行内事件处理器；接口实测 200 条只 38ms，瓶颈在渲染侧。**代价**：2132 条待审从 11 页变 43 页，嫌翻得累改 `PD_PAGE` 一处即可。
2. **分组表每行三个按钮改事件委托**：`onclick="pdEditRow(…)"` 等改成 `data-ract` + `data-rid`，由 `pdBindGroup()`（原来只管组级 `data-gact`）统一处理。分组行渲染函数里的内联 `onclick` 从 3×N 降到 **0**；展开的编辑器卡里还剩 3 个（运价/舱位互切、收起），同时只有一张卡展开，可忽略。
3. **展开态跨重画复原**：`pdEditRow` 把当前展开的行 id 记进 `PD.open`，`renderPending` 重画后按它复原、失效 id 顺手清掉。缘由：切运价/舱位或翻页会整块重写 `innerHTML`，编辑卡被抹掉，只能重新点开的体验被当成"又卡了"。
4. **`.pd-gtable` 改 `table-layout:fixed` + 九列 `<colgroup>`**（23/8/9/12/11/8/7/7/15%），单元格加 `overflow-wrap:anywhere`。缘由：编辑卡是 `colspan=9` 的一行，auto 布局下每次展开收起都要为整表重算最小内容宽度。

*回归脚本*

5. **`manual_check.py`：拷出库副本后先 `DELETE FROM pending_records`**（新增 `import sqlite3`）。缘由：回归用的是**生产库副本**，现在副本里带着 2132 条真实待审，`/api/pending` 按 `failed 优先 + id DESC` 排序，453 条 failed 把用例自己暂存的 passed 条目挤出 `limit=50~300` 窗口 → **6 项常年假红**。定性过程：逐条读断言 + 改前基线对照，确认是脚本起点不干净，不是实现坏了。只动副本，不碰真库。
6. **`web_check.py`：两条漂移断言跟上新实现**——`function normalizePortField` 改为现名 `hardNormalizePort`；`gPod.concat(pod.sort())` 那句随旧筛选实现一起下线，换成现架构下等价的两条（`fieldOptions` 合并 rates+space 真值；销售端筛选框走 `data-ac` 联想组件且 `data-nogroup` 排除港群）。

**实测**：`js_check` 通过（2969 行）；`web_check` exit 0；`validity_rule_check` exit 0；`manual_check` **全部通过**（上一轮是 58 通过 / 6 失败）。线上页面已含 `data-ract` / `PD_PAGE = 50` / `table-layout:fixed` / `<colgroup>` 四处标记；HTML 每次请求现读，**不用重启服务**（服务仍是 pid 26892）。

**并发备案**：`web/index.html` 15:11 被另一处改动动过一行（某页签加 `justify-content: flex-end`，并留下 `index.html.bak-tabright-20260911-151432`），**不是本次所作**；上面三套检查是在两拨改动合并后的文件上重跑的，均通过。但同一个单文件前端两个 agent 并行改迟早互相覆盖——同一时间只让一个 agent 动 `web/index.html`。

**待你眼睛验收**（我点不了页面）：切运价/舱位是否还掉帧、展开编辑卡后翻页/切 kind 是否保住展开、九列宽度是否合适（尤其「操作」列三个按钮、「起运→目的」长港名换行）。

**回退**：`index.html` 退回本轮前＝用 `正式版运价库/index.html.bak-20260911-141511` 覆盖 `Database/web/index.html`（那份同时含清单①④之前状态，退它会连 blur/阴影一起退回）；只退本轮 4 项＝删 `.pd-gtable` 的 `table-layout` 与 `overflow-wrap`、删 `gcols` 常量与拼接、`PD_PAGE` 改回 200、行按钮改回内联 `onclick`、`pdEditRow`/`renderPending` 去掉 `PD.open` 相关三行。`manual_check.py` / `web_check.py` 的改动是测试侧，回退只影响自检红绿灯，不影响业务。

**仍未做**：其余页签合计约 96 个内联 `onclick`（静态少量，不在卡顿路径上）；驳回日志列表仍用内联按钮（每页 ≤50 条）。

---

## 2026-09-11（待审核页两项修复 · 经批准的口径＝清单④＋①）

**动作**（只改代码，未动台账库/主数据）：

1. **`manual_server.py` · `images_for()` 补中文键兜底** — `declared` 增加 `or row.get("图片名")`，原文兜底增加 `or row.get("消息原文")`；仍是一个解析函数，业务表链路（英文列名行）与待审核链路（中文 payload）共用。
2. **`web/index.html` · `.topnav` 去掉 `backdrop-filter: blur(8px)`**，底色由 `rgba(15,43,70,.94)` 改不透明 `var(--navy)`，并留注释说明别再往这加回来；**`.pd-card` 阴影**由双层大半径改复用 `--shadow-sm` 令牌。
3. **`imagesBlock()` 的 `<img>` 补 `loading="lazy" decoding="async"`** — 缘由：第 1 条修好后待审核分组卡会一次性同步拉原图（实测单张最大 1.6MB、本页去重 8 张），不懒加载等于用卡顿换图片。

**缘由**：你报「待审核页切运价/舱位、点编辑展开就突然卡」。实测后端不慢（200 条 38ms、主数据只在启动时拉一次），掉帧来自 sticky 顶栏模糊层重算＋整块 `innerHTML` 重画；同时发现待审核卡片**从来没显示过原图**（放行只能信 OCR），与你「原图是唯一核对源」的口径冲突。

**实测**：`/api/pending` 200 行 → 带图 **37 行 / 去重 8 张 / missing 0**，`/images/` 回 200 且是 PNG 字节；`js_check` 通过；`manual_check` **58 通过 / 6 失败**，并用改动前的 `manual_server.py` 在库副本上跑基线，**失败集合与逐条 detail 完全一致**（＝这 6 项非本次引入）。

**顺手记两个既有失守点（我没改，等你定）**：`web_check.py` 有 2 条断言已与代码漂移、常年红（`function normalizePortField` 实际已改名 `hardNormalizePort`；`gPod.concat(pod.sort())` 表达式已变）；`manual_check.py` 那 6 项红，疑是 09-11 上午身份键改版／撤旧留新改语义后测试没跟上。

**回退**：用 `正式版运价库/manual_server.py.bak-20260911-141511`、`index.html.bak-20260911-141511` 分别覆盖 `Database/manual_server.py`、`Database/web/index.html`。HTML 改动刷新页面即生效；`manual_server.py` 改动需重启服务（本次已重启，pid 26892）。

**未做（清单剩余两条，等你点头单独一轮）**：切 kind 不再整块重画＋行级按钮改事件委托＋分页 200→50；`.pd-gtable` 加 `table-layout:fixed`。

---

## 2026-09-11（正式 48h 回填 · 自主跑，供醒来复核）

**动作**：按你选的「方案A·分批」，我起了 6 个子 agent 并行处理 11 个群的 48h 窗口（since 2026-09-09 00:00）——各自拉消息→下载识图→写 ocr_cache→逐条写 llm_out；再由主流程统一 `pipeline advance` 三步（parse→build→submit）落进**待审核**。**未直接写业务表**（放行仍归你）。

**结果**：拉取 237 条消息、识图 76 张；**待审核新增 2,132 条**。首轮机械校验 通过 1,283 / 待修 849；随后我又做了 3 处安全修复（见下），**现 通过 1,429 / 待修 703（67% 通过）**。水位已推进到各群 09-10 最新一条。

**回填后追加的修复（同轮自主）**：
- **修 `manualdb.py` route_idx 碰撞 bug**：原来把航线的 `region` 也当查询键，导致 `地东` 的 region「地中海」与航线名「地中海」判歧义被整体丢弃（`中东` 同理）。改为 route_idx 只用 `name+aliases`（region 是分组元数据、不该作查询键）。**已翻正 ~238 条**（地中海/中东 航线）。重启服务 pid=41656。回退：把该行改回含 "region"。
- **给 Ningbo 加别名 `NB`/`NGB`**（子 agent 爱写缩写）。
- **把 58 条 pending 的起运港「全口岸/全国」改写为 `全国基本港`**（对齐港群）。
- 逐条 re-check 后共翻正 130 条。

**仍待修 703（都是该交人工/补主数据的，脏值没进业务表，符合设计）**：
- `船司不得为空` 106：帖里没写船司，按"没写别猜"留空 → 人工指派。
- 目的港不在主数据：IZMIT 25 / Puerto Caldera 14 / CONSTANTA 13 / MISURATA 10 等（地中海·北非·黑海·中美洲一批港）→ 需补 `ports`。
- 无效航线值：`SAWC` 28、`南美西/墨西哥` 14（服务码/合并串当航线）→ 可留空或人工改。
- `海运费USD不得为空` 16（纯加价公式）、`船名航次不得为空` 12（现舱缺船名）→ 人工补。
- 起运港「深圳」8（无此港名，实为蛇口/盐田，二选一交人工）。

**回退**：这些都在 `pending_records`（review_status=awaiting），没放行就不进业务表；要清空回 `Database/freight_manual.db.bak-20260911-024808` 或按 pending 批量删除。

**补映射（第2轮，你睡着时自主跑的）**：建 `深圳` 起运港群（蛇口+盐田）；`SAWC`→「南美西」别名、`NB/NGB`→Ningbo；新增港 Izmit/Derince/Evpey/Tekirdag/Gebze(TR)、Constanta(RO)、Misurata/Benghazi(LY)、Poti(GE)、Varna(BG)、Port Said(EG)、Puerto Caldera(CR)、Puerto Barrios(GT)、Jiujiang(CN)、Malta(MT)、Burgas(BG)；合并别名 Lázaro Cárdenas+=LAZARO、Puerto Quetzal+=PQ、Cristóbal+=Cristobal Colon/Colon Free Zone、Algiers+=ALGER、New York+=NYC/NY、Savannah+=SAV、Pecém+=PECAL；并写航线（"南美西/墨西哥"）批量留空。**结果：待修 703→453、通过 1,429→1,679（79%），起运港已全部命中。**

**剩下 453 我不再无脑补主数据**（再补会塞垃圾），两类：
- **真·该人工 ~146**：船司没写 116、海运费空(纯加价公式) 16、船名航次空 12、柜型非法 2 —— 帖子里就没这项。
- **agent 写法问题 ~307 目的港(145 种)**，根子在采集侧不是缺港：占位符/服务组名当目的港(`(未注明，南美东)`/`BP`/`NE Port`/`ECSA Base Port 1`/`Argentina 1`)；带"via 走法"的串(`Pecem/VITORIA via CARIOCA/SANTANA`，应拆基础港入库、via 进备注)；同名港没消歧(裸 `Cartagena` 该写 COCTG)；零星真缺港(Itaguaí/VIX/Lirquén/Alicante/Tenerife/Las Palmas/Nassau/Turbo/HOU/MIA/TPA)。

**建议根治**：与其一条条补主数据，不如我改一版**子 agent 写法铁律**（占位符留空/判噪声、via 串拆基础港、同名港强制 LOCODE、US 三字码→规范名），下轮增量就不再产这类脏记录。

**你醒来可定**：① 通过的 1,679 条在待审核页批量放行；② 要不要我改子 agent 写法铁律（治那 307 条的根）；③ 零星真缺港要不要批量补。

---

## 2026-09-11（欧洲群接入 + 试运行收尾）

### 1. 主数据 · 港口 ports（加别名 / 新增）
| 动作 | 对象 | 内容 | 缘由 | 回退 |
|---|---|---|---|---|
| 加别名 | Norfolk | `+ORF` | 美加线群按三字码写美东 | 删别名 ORF |
| 加别名 | Philadelphia | `+PEL` | 同上 | 删 PEL |
| 加别名 | Baltimore | `+BAL` | 同上 | 删 BAL |
| 加别名 | Ambarli | `+Istanbul / ISTANBUL / 伊斯坦布尔` | 土耳其群写"ISTANBUL"实指伊斯坦布尔集装箱门户 Ambarli | 删这 3 个别名 |
| **新增港** | **Aliağa** | en=Aliaga, country=TR, locode=TRAAG, 别名 `阿里亚加,地中海,Aliaga,Aliağa,TRAAG` | 土耳其群报价里的 ALIAGA 主数据没有 | 删该港 |

### 2. 主数据 · 港群 port_groups
| 动作 | 对象 | 内容 | 缘由 | 回退 |
|---|---|---|---|---|
| **新增港群** | **全国基本港**（kind=pol） | 成员 `Nansha,Shekou,Yantian,Ningbo,Shanghai,Qingdao,Tianjin New Port,Dalian,Xiamen` | 「欧洲价格更新」是"全国口岸出欧洲"、无固定起运港，按你授权建兜底起运港群 | 删该港群 |

### 3. 采集配置 chat_sources.json（skill 包内）
| 动作 | 内容 |
|---|---|
| 改群名 | `ONE+ZIM+HPL+CSP 交流群` → **`ONE/ZIM/WHL/HPL/CSP 交流群`**（对齐钉钉真实群名），carriers `+WHL` |
| 新增群 | **欧洲价格更新**：cid `cidTKlF4IcDiQonoNQY3qkHow==`，slug `europe_price`，default_pol `全国基本港`，carriers `CMA/MSC/MSK`（帖子里写明的），routes `欧基港/北欧/地中海/波罗的海`，发送人 `杜佳仪 Alby` |
| 未采纳 | 运去哪（你说不要，未加） |

### 4. 台账表 chat_groups / senders
- **chat_groups：11 群**（10 原有 + 欧洲；ONE 改名 = 删旧行 + 增新行）。
- **senders：10 个** = 画像里的 7 个（Dale万秋燕 / 古秋铃 / 韩琦 / 孙丽洁 / 虞方磊Ted / 俞文清 / 杜佳仪 Alby）+ 这轮实拉见过、画像没列的 3 个（**宁萌 / Monica-15104094203 / Echo Ma**）。

### 5. 表结构 schema（manualdb.py）
- `freight_rates` 加列 `ai_flag`（幂等 `ALTER TABLE` 迁移）+ `RATES_MAP` 映射 `AI判断→ai_flag` + 排除出内容指纹 + `GROUP_FIELDS` 带上。
- 说明：`space_records` 的 `validity_raw/valid_from/valid_to` 列与 `box_qty INTEGER` 是**你已加的**（非本次）；`VALIDITY_REQUIRED_KINDS` 我一度误改成只 `{rates}`、已改回 `{rates,space}`（净无变化）。

### 6. 数据清理（破坏性，已备份）
- 清空测试数据：`freight_rates` 15→0、`space_records` 3→0、`pending_records` 71→0。
- **备份**：`Database/freight_manual.db.bak-20260911-024808`（要回退直接覆盖回 `freight_manual.db`）。

### 7. 代码改动（非数据库，一并记，便于对照）
- skill：`validity_parser.derive`（有效期三级兜底）、`record_builder`/`pipeline`（运价+舱位都兜底 + 写 `AI判断`）。
- 看板 `web/index.html`：卡片阴影、紧凑化、失败字段标红、改动即时校验+自动保存（删了"保存修改"按钮）。

---

### 待你确认/可否决项
1. `全国基本港` 的成员港是否合适（我放了 9 个基本港，你要增减直说）。
2. senders 里我补的 3 个（宁萌/Monica/Echo Ma）要不要留；`阮玲芳`（只提问不报价）我没加。
3. `ISTANBUL→Ambarli` 这条别名映射是否符合你们口径（也有人把伊斯坦布尔算 Kumport/Haydarpasa）。

## 2026-09-17（心跳 skill 去「钉钉 AI 表格」表述 · 措辞退役）

机制早在 09-10 台账化改版时就删了，但文档里还留着"不再写钉钉 AI 表格"这类退役对比句，
读的人容易误以为还存在第二条写入链路。本轮把这些表述一律换成正向现行口径，不删任何 operative 约束：

- 唯一写入口径统一表述为「运价台账 `POST /api/pending`」（`SKILL.md`、`Instruction.md`、`ledger_contract.json` 注释、`runbook.md`、`extraction-rules.md` 改版说明）。
- 删掉 AI 表格机制残留词：`record update`/覆盖标记字段/`fieldId`/`baseId`/`fileToken`/`singleSelect` 选项比对/"不像旧钉钉表会自建选项"（`extraction-rules.md`、`record_builder.py`、`llm_output_contract.md`）。
- `learnings.md`「已废弃做法」里那条"写钉钉 AI 表格"整条移除，同段 `init_ledger.py`/`check_spec.py`/`price_history.py` 等括号里的"建钉钉 base/比对钉钉字段选项/查钉钉快照"一并去掉，只留"已移除 + 历史轨迹改走 `/api/rates_grouped`"。
- `config/Instruction.md` 舱位类型十一档后那句"（旧钉钉表的第 9 档"报备"已删）"删除，枚举本身未动。
- 版本 5.1.0 → 5.1.1；**项目副本与已安装副本 `~/.qwenworkcn/skills/freight-rate-heartbeat` 同步改**（改前两份逐字节相同）。
- 唯一没动的同源代码词：`manualdb.py:384` 图片字段容错读键里的 `"fileToken"`——那是入站 payload 的键名兜底不是文案，且该文件今天另有改动在飞，要清就单独说。

自查：两份副本 grep「AI 表格|多维表|aitable|钉钉表|fieldId|baseId|fileToken」已只剩上面那条代码兜底；`ledger_contract.json` 仍是合法 JSON；skill 内 `.py` 全部编译通过。

## 2026-09-17（心跳规范从"靠自觉"变成"可校验" · skill v5.2.0）

问题：`群聊规范/` 的强制力此前全是 prose——脚本零引用该目录、`chat_sources.json` 没有规范路径、
队列项不带依据字段，"每轮先读该群规范"只能指望 Agent 自觉；README 还写着 `alias_scope`「尚未建」，
而库里已 36→37 行。本轮补三道机械关卡（值域硬拦仍在台账闸门，这三处只报告不拦）：

1. **路由数据化**：`chat_sources.json` 11 个群各加 `spec_dir`；新增 `scripts/spec_index.py`
   解析规范文件（缩写表按"写法/含义/归一目标/作用域"读成结构化行、「待补」条目、`##` 小节锚点、
   `extraction-rules.md` 的 25 个条款号），`pipeline.py` 生成 `llm_queue` 时把 `spec_path`/`spec_todo`
   注入每一项 → Agent 不再需要人肉对 README 的映射表，定位不到规范的群会当场报出。
2. **依据可校验**：契约沿用已有的 `rule_basis` 字段（不另造 spec_refs），新增 `scripts/check_llm_out.py`
   反查：溯源（msg_id 必须在落盘消息里）、禁填键、decision/_table/slug 合法为**错误**；
   柜型三档/舱位十一档/逐档配价/占位符/锚点真伪/"用了该群缩写却没引用本群规范"为**可疑**。
   BUILD 合并 llm_out 前自动跑一次并打印汇总；`python scripts/check_llm_out.py --date <MMDD>` 可单独跑。
3. **漂移体检**：新增 `scripts/spec_alias_diff.py`，规范缩写表 ↔ `alias_scope` 求差集（含语义分诊：
   规范写"留空/不是港口/交人工"的叫别登记、写法即规范值/港群的叫自映射，都不算缺失），
   并查断链（配置 `spec_dir` 是否指得到、规范头部 slug/群名与配置是否一致、引用的 `.md` 是否存在）。
   已挂进 `manual_check.py`：断链或目标冲突才判红，待登记只进 detail。今天实测：规范 54 行、库里 37 行、
   待登记 1（`欧洲价格更新/pol/+TAO→Qingdao`）、库里未写 2、冲突 0、断链 0。

附带修掉两处过期与一个自测发现的 bug：`extraction-rules.md` 第 1 条引用的 `memory.md` 已不存在 → 改指 `learnings.md`；
`群聊规范/README.md` 的「alias_scope 尚未建」改成现状；`resolve_basis` 判全局条款号时拿 `#7`/`7` 去比完整锚点串，
导致所有有效条款全被判"不存在"——已修（`extraction-rules.md#7` 现在正确通过，`#999` 报"现有 1…25"）。

测试：`test_spec_layer.py`（合成夹具在临时目录，`FRT_RUNTIME_DIR` 指过去，不碰生产库/不连服务）5 组全过——
模块导入与编译、路由 11/11 走 config、正例零误报、六类错误全抓、五类可疑全抓、干净夹具退出码 0、体检对生产库只读通过。
`manual_check.py` 全过（含新增那条规范检查），`web_check.py` 全过，看板 8795 仍 200。
源→运行同步 10 个文件、md5 一致，版本 5.1.1 → 5.2.0（新增能力＝minor）。

## 2026-09-17（心跳定时任务重绑本库 + 水位基线推到 09-18 06:00）

**任务侧**：低频 `0 9-13 * * *` 与高频 `*/10 14-18 * * *` 两条已重绑并**启用**——工作目录换成
`运价项目/Skill/freight-rate-heartbeat`、主控 `heartbeat/run.py` → `scripts/pipeline.py`、契约 →
`scripts/llm_output_contract.md`、图片归位改成台账 images 目录；判定步骤改为"先读队列项 `spec_path` 指向的该群规范
→ 写判定（`rule_basis` 必须真实锚点）→ 先跑 `check_llm_out.py --date` 再 advance"，并删掉废弃的 `status_updates`/钉钉表字样。
「运价云看板同步」任务已删除（云端链路已退役，本地台账是唯一主库）。

**水位基线**（`Skill/heartbeat-data/watermark.json`，11 个群）：原值全部停在 09-10（`cma_emc_pil 17:23:17`、
`tianjin_latam 17:36:00`、`cosco_hmm_msk_oocl 18:17:57`、`huanan_latam 18:27:25`、`one_zim_hpl_csp 18:27:20`、
`qingdao_latam 17:50:24`、`ningbo_space 17:45:49`、`meijia_sales 10:12:36`、`dalian_latam 15:21:03`、
`turkey_price 19:07:11`、`europe_price 17:26:05`），现统一改为 **2026-09-18 06:00:00**。
效果：今晚 18:20–18:50 那几轮 since 落在未来 → 拉到空 → DONE，零成本；明早 9:00 那轮只收 06:00 之后的消息。
**被跳过且不会自动补拉的窗口**＝现在起至 09-18 06:00（含深夜的土耳其/欧洲/美加报价段）。要收回：直接把 JSON 里对应 slug 写回小值
——注意 `watermark.py set` 只许前进（防回退设计），回退必须改文件；`watermark.py get` 另有"不早于今天 00:00"的下限兜底。

**顺带纠正我自己一个判断**：先前说"首轮会一次拉回近 7 天消息"是错的——`pipeline.py` 走 `watermark.py get`，
那里有今天 00:00 的兜底下限，真实首轮窗口是"今天 00:00 → 当轮"，不是 7 天。

**待观察**：两条任务启用后 `nextRunAt` 仍显示 null（已知的停摆信号）。明早请核对：
`Skill/heartbeat-data/.work/run_0918/` 是否生成、台账「待审核」是否有增量、`watermark.json` 是否被 `pipeline.py` 往前推。

## 2026-09-18（按"全改"执行：主数据补录 + 别名清理 + 图片还原，含三处我自己的错）

**做了的**
- 航线补录（依据 `extraction-rules.md` 第 2 条工作台口径，库里原本只有 地东/地中海/欧基港）：新建 `routes` 地西（aliases 西地中海,western med,wemed）、希腊；把「亚德里海」并入现有航线 `亚得里亚海` 的 `aliases`（按契约全局叫法进 `routes.aliases`，不进 `alias_scope`）。走 `/api/master/save` 写，服务侧 `get_master` TTL 300s 自动跟上。
- 效果：待审 failed 从 451 行里的 185 → **101**；航线类 failed 76 → **0**（`resolve_unmapped_ports.py --write` 顺带重判，32 行转 passed）。
- 图片：昨天被 `purge_files_recyclebin.py` 清掉的 76 个 `Database/images` 文件已从回收站复制还原（现在 80 个文件）。
- 别名清理：删掉恒等别名（alias==target）**5 条** —— 本意只删 resolve 脚本因我新建航线而产生的 2 条噪音（`地西→地西`、`希腊→希腊`），但我的条件是"凡 trim 后相等就删"，多删了 3 条 `宁波舱位滚动更新/space_type/跳港·空班·首航`（今天候选批准流程产的行）。这 3 档早已在十一档枚举里、恒等别名对闸门无影响，`manual_check` 全过；要还原我可以按 alias_scope 契约一行补回。

**没做、需要你的**
- `operators` 仍 0 行、266 次放行 `reviewed_by=unknown`：11 个群的 `chat_groups.owner` 全为 None，没有任何可信来源生成账号，编造会直接错配分区可见性 → 需要姓名清单。
- VACUUM（碎片率 53.5%）：要停服务，而此刻有会话正在写库 → 延后，等空闲时 stop→VACUUM→start，约 30 秒。
- 20 个"库里查无此港"的值 + 2 个 difflib **误配**（`Arrecife`↔`Recife`、`Las Palmas`↔`La Paloma`，跨洲跨国）已写进 `ops/reports/主数据待补-2026-09-18.md`，不自动补。

**我自己错的三处（都已修）**
1. `ops/db_health.py` 图片判据错：按行累加 + 没剥 `@` 前缀 → 报"缺图 134"，实际唯一引用 4 张、**真缺 0**。判据已改成唯一引用 + 剥 `@` 词干匹配。
2. `heartbeat_perf.py` 最初读 `state.json`，实测发现每轮 DONE 后被重置，当天产出会丢 → 改用 raw/llm_out/ocr_cache 的 mtime 聚类重建轮次 + 台账 `created_at` 算入库。
3. `pending_review.py` 最初按 `edit_log` 非空算"人工改过"，把 266 条放行留痕当成修改、正确率成了空转 → 改成必须 `orig_payload` 与 `payload` 有字段差异（真改过 1 行）。

## 2026-09-18（群负责人 owner 预填 + VACUUM 提示门槛纠正；附一次我用错端点）

- 走错端点：先拿 `/api/master/save` 写 `chat_groups`，被白名单挡下（只收 ports/carriers/routes/senders/port_groups），
  9 次全 500、**零脏写**；正确入口是 `POST /api/chat-group/save`。
- 按"该群主要发送人且占比≥80%"预填 `chat_groups.owner` 8 个群，note 标"预填待人工确认"；
  写入前后逐字段比对，确认只有 owner/note 变化（该函数会整行覆盖 default_pol/region/active/source，所以必须回传原值）。
  3 个群无运价记录，留空不猜。
- `db_health.py` 的 VACUUM 提示改为「空闲率>20% 且库>200MB」双条件：此前 4.5MB 的库每天报一个不需要处理的 WARN。
- 记一笔归属：本次 `manual_check` 出现 6 项红，不是本会话造成的 —— 跑着的服务进程是 09-17 16:18 启的，
  而 manual_server.py/manualdb.py 10:39、manual_check.py 10:47 刚被另一会话改过（新用例测的是没重启的旧进程）。
