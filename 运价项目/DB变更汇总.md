# 运价台账 · 自主数据库改动汇总

> 约定：凡我在**未逐条请示**下对「台账库 / 主数据 / 采集配置」做的改动，都记在这里备查、可否决。日期倒序，最新在上。每条给「缘由」和「回退方法」。

---

## 2026-10-21（新增中心成长同步 outbox；未改运价业务数据）

**缘由**：中心成为共享昵称、头像、经验和成长投影唯一真源。Navigator 业务经验仍需先作为本地不可变事件保存，再可靠补传中心，避免中心短暂离线时丢事件。

**改动**：新增运行时表 `account_growth_sync_outbox(event_id,user_id,status,attempts,last_error,created_at,sent_at)`，只记录可同步事件状态，不复制经验正文。业务事件与 outbox 同事务写入；登录、账号页读取和新经验产生时补传中心，成功标记 sent。表和待同步状态不进入业务快照。

**验证**：账号、成长、中心客户端 15 项测试通过；跨栈隔离实例验证首次有效查询 +18、重复请求不增加、中心最终 1 条事件；正式本地与阿里云账号均核对为 `Zayne / av-04 / 6612 XP / level 28`。运价、待审、报价和客户业务数据未改。

**回退**：停用新客户端代码并恢复发布前备份；确认无待同步事件后再删除 outbox 表。不能只清 outbox 而保留未上报业务经验。

## 2026-09-21（采集配置：新增「地西+地黑价格更新」群 + 杜佳仪系三群规范改为「共享基础+群内差异」；未动台账数据）

**缘由**：你说明 土耳其 / 欧洲 / 地西+地黑 三个群航管同为杜佳仪等 2 人，业务规范可套用；选择"共享基础+群内差异"的组织方式。"地黑"经确认为群名 **地西+地黑价格更新**（此前未配置）。

**改动**：
1. **新增群** `dixi_hei_price`（地西+地黑价格更新，cid `cidU3K4GSO+bHPhFklCHMnZQw==`，shard A，兜底 全国基本港·待航管确认，航线 地西/黑海）进 `chat_sources.json`；建 `references/群聊规范/地西+地黑价格更新/规范.md`（只写差异，大量待补）。群数 11→12。
2. **新建共享基础** `references/群聊规范/_杜佳仪系共享/规范.md`：杜佳仪系三群共用的报价格式、运价/舱位判别(#27/#28)、通用坑（币种/多POL/附加费）。
3. turkey/europe 规范.md 顶部加"继承共享"抬头、删掉与共享重复的判别/通用坑节，只留群内差异（专属缩写表、兜底港、别名登记日志）。
4. `chat_sources.json` 三群加 `spec_base` 字段指向共享目录。
5. `spec_index.py` 支持 `spec_base` 合并：群档案 = 共享基础 + 本群差异（headers/abbrev/todo/anchors 并入），`brief()` 增 `spec_base_path`；`pipeline.py` LLM 提示改为"有 spec_base_path 先读共享再读本群"。

**验证**：spec_index/pipeline/record_builder py_compile 通过；spec_index 冒烟 12 群全 route=config 无异常，共享 4 小节并入各群（europe 小节 5→9、dixi_hei 3→7）；resolve_basis 跨基础锚点验证：europe 命中共享"运价/舱位判别"、dixi_hei 命中本群缩写表、不存在小节正确报错。源↔运行字节比对复验 0 差异（新群/新基础文件手工补拷，因 sync 只覆盖两边都有的）。

**遗留**：`dixi_hei_price` 归 shard A ⇒ A 片从 6 群变 7 群，定时任务重启用前留意 A/B 均衡（当前 cron 全禁用，无影响）；第 2 位航管姓名、地西/地黑 兜底港与专属缩写表待实测补。台账数据本次未改动。

**回退**：`git checkout` chat_sources.json / spec_index.py / pipeline.py / 三群规范.md → 手工删 `_杜佳仪系共享/`、`地西+地黑价格更新/` 两目录（源+运行）→ `skill_diff.py sync`。

---

## 2026-09-21（全局规范新增「带价现舱 / 舱价同源」判别 + 对齐代码；未动台账数据）

**缘由**：你给出运价/舱位的业务分类口径——① 带价现舱属于现舱（价格是舱位信息里的字段）；② 舱价同源是运价与舱位分属不同区块、不混排。要求写进全局业务规范，并给 turkey 群加判别节。

**改动**：
1. `references/extraction-rules.md` 新增 **规则 27**（带价现舱→1 条 space、价格进 `价格USD`、不拆运价）与 **规则 28**（舱价同源→运价块进 `freight_rates`、舱位块进 `space_records`，按区块分表），并写明与规则 7 的衔接、27/28 的分界。
2. `scripts/llm_output_contract.md` 把旧的"带价现舱拆两条：金额进 rate、箱量进 space"注释改为对齐 #27/#28（旧写法与新口径冲突）。
3. `record_builder.py` `t_space_table_cache` 改为**每行只产 1 条 space**（价格进 `价格USD`），去掉原来额外产的那条 rate。直连验证：现舱表 2 行→2 条 space（原为 2rate+2space）。
4. `references/群聊规范/土耳其价格更新/规范.md` 新增「运价/舱位判别（本群）」节：纯运价矩阵/价卡→rates；带价现舱（价嵌在舱位行/表内）→space 一条；舱价同源→分块各归各表。

**验证**：record_builder py_compile 通过；现舱表模板输出确认 space-only；源↔运行字节比对复验 0 差异（同步 4 文件）。

**遗留影响（需你定夺）**：今天 turkey 那轮我已提交的现舱表图（msgHsVxQ）是按**旧口径**产了 2 rate+2 space，按新 #27 应是 2 space。那 2 条 rate 现在挂在待审——要不要我按 `消息ID` 删掉那 2 条多余 rate？（不删也行，人工在待审页驳回即可。）台账数据本次未改动。

**回退**：`git checkout` extraction-rules.md / llm_output_contract.md / record_builder.py / turkey 规范.md → `skill_diff.py sync`。

---

## 2026-09-20（心跳提效：新增三类规整图零 LLM 生成器 + 提交前防呆闸门；未动台账数据）

**缘由**：turkey_price 手动单群拉取暴露两个问题——① 15 条消息全靠 LLM 逐条判（模板零命中），131 条记录转写耗时；② 我这次踩了"判定文件写错目录→空提交推水位""柜型写 40HQ 非规范 40HQ/HC"两个坑。你选了"识图模板化 + 提交前防呆自检"两项优化。

**改动（全在 Skill 源目录，未碰 freight_manual.db / 主数据）**：

1. `record_builder.py` 新增三个规整图生成器 + `CACHE_TPL` 按 `cache["type"]` 分发注册表：
   - `t_space_table_cache`（type `space_table`，现舱表 ETD/POL/POD/VESSEL/VOL/RATE/FT/退关费）→ 每行拆 rate+space；
   - `t_rate_matrix_cache`（type `rate_matrix`，多 POL×多 POD 矩阵如 CUL）→ 逐格展开、空值格跳过、tag 进备注；
   - `t_price_card_cache`（type `price_card`，POD×柜型价卡如 MSK 地中海）→ 每行一条，无 POL 则起运港留空交人工。
   配套 `_num/_ctype/_vol_split` 解析（`40HC`→`40HQ/HC`、`1X40HC`→柜型+箱量）。图片分发从写死 title 判断改为 type 注册表，旧 HPL/add on 兜底保留。
2. `pipeline.py` `step_build` 加防呆闸门：本轮有新消息却零候选、且 llm_out 目录为空 → **ABORT（exit 2）**并打印实际 glob 路径 + sfx，不再空提交推水位（判 noise 也算文件，故"目录空"＝真没判）。
3. `check_llm_out.py` 加防呆：llm_out 目录空/不存在时 `n_obj==0` → 报 **error**（不再"全部干净"假通过），提示核对 `--shard/--only` 与生成时一致。
4. `llm_output_contract.md` 补「结构化表格图 → 零 LLM 生成器」章节，写明三类规范 cache schema + 新增类型只需挂 `CACHE_TPL` 一行。

**验证**：六脚本 py_compile 通过；直连测试三生成器——现舱表 2 行→4 条(2rate+2space)、矩阵含空格→正确跳过、价卡→POL 留空；空目录自查闸门 `--only qingdao_latam` 返回 error 退出码 1（原会假通过）；源↔运行字节比对复验 0 差异。

**回退**：`git checkout` record_builder.py / pipeline.py / check_llm_out.py / llm_output_contract.md → `skill_diff.py sync`。台账数据本次零改动。

---

## 2026-09-20（采集数据重置 + Skill 新增 `--only` 单群手动模式）

**缘由**：你要求「重置水位到今天 09:00、关掉所有定时任务、清空已采运价条目，接下来以单群为单位手动逐个拉群」，为手动重采做干净起点。分片 A/B + 四定时任务架构不动，只是**暂停**。

**台账库改动（三张采集表清空，主数据零改动）**：`DELETE FROM pending_records / freight_rates / space_records` 并清其 `sqlite_sequence`（新条目 ID 从 1 重计）→ 计数 2025 / 714 / 39 全部归 **0**。港口 1212 / 船司 53 / 别名 94 等主数据、以及 `rule_candidates`/`field_defaults`/`edit_log` 学习数据一律**未动**。台账服务在跑但 `foreign_keys=0`、三表无交叉引用、`pending_shadow` 空、WAL 已落盘，直连 SQLite 删表安全；经 `/api/pending?status=awaiting` 复核读到 0，非缓存假象。

**水位线**：全 11 群回退到 `2026-09-20 09:00:00`。注意 `watermark.py set` 是单调的（只前进），回退**必须直接重写 `heartbeat-data/watermark.json`** 为 `{slug:"YYYY-MM-DD HH:MM:SS"}`，不能走 set。

**定时任务**：全部 7 个 disable（4 心跳 + 待审辅助 + 效率审查 + 维护体检）。稳定后把 4 心跳按原 `--shard A/B` 重新 enable 即回自动模式。

**Skill 代码（纯增量，不改分片/cron 路径）**：`--only <slug>` 复用已同步的 `sfx()` 机制——设 `FRT_ONLY` 后中间目录变 `run/msgs/draft/llm_out_<D>_<slug>`，群与群完全隔离；`step_pull` 把 pull_target 锁到该群、非法 slug 直接 ABORT；`runpy()` 与自查提示透传 `--only`；`record_builder` 经子进程继承 env 自动隔离，`check_llm_out` 加读 `FRT_ONLY`。手动跑法：`pipeline.py next --only turkey_price` → 存 raw → `advance --only turkey_price`（照抄输出）到 DONE，再换下一群。

**验证**：6 脚本 py_compile 通过；`--only turkey_price` next 只吐 1 条 dws（时间戳正是 09:00）、不带则 11 条、非法 slug ABORT 列出可用群；源↔运行字节比对复验 0 差异；运行副本 `next --only qingdao_latam` 冒烟通过。

**踩坑修正（销售端仍显示旧条目）**：直连 SQLite 清库后，运行中的 `manual_server` 销售端 `/api/rates_grouped` 仍返回旧 338 条——它走进程内 `_GROUPED_CACHE`，失效键 `PRAGMA data_version` 因"每请求新开连接"而不随外部写变化，缓存永不失效（实查口 `/api/rates`、`stats` 已是 0，故判定为缓存陈旧非没清干净）。用自带脚本 `stop-manual.bat` → `manual_server.py --daemon` 重启（同端口同库）后归零。**教训：凡绕开服务直接改库，务必连带重启 8795。**

**回退**：库用 `Database/backup/freight_manual.pre-reset-20260920-162318.db`（7.8MB）整库 `--import` 回灌；水位用 `watermark.json.pre-reset-20260920-162318.bak` 覆盖回；定时任务逐个 enable 回来；`--only` 是新增可选参数，不用即无影响（回退只需 `git checkout` paths/pipeline/check_llm_out）。

---

## 2026-09-20（采集配置 + Skill 代码：心跳按群分片 A/B，2 个定时任务拆成 4 个；不动台账库数据）

**缘由**：你要求「按群聊单日消息量与频率把高低频两个心跳任务各拆成两个大组，缩短单次识别时间」。落地＝把 11 个群按体量均衡切成 A/B 两片，各片中间目录与水位互不覆盖，ocr_cache 仍全局共享。

**改了什么**（只动 Skill 源目录 + 定时任务，未碰 freight_manual.db / 主数据业务表）：

1. `scripts/paths.py`：新增 `SHARD`（读环境变量 `FRT_SHARD`）与 `sfx()`（返回 `_A`/`_B`/空）；`chat_groups()` 设了分片时只返回该片群。
2. `scripts/pipeline.py`：`--shard` 在 `import paths` 前预扫描写入 env（子进程继承）；`run_/msgs_/draft_/llm_out_<D>` 目录追加分片后缀；`ocr_cache` 保持全局；`runpy()` 与自查提示自动带 `--shard`。
3. `scripts/record_builder.py`、`scripts/check_llm_out.py`：中间目录同步加 `sfx()`；check_llm_out 加 `--shard`。
4. `scripts/rework.py`：`list` 加 `--shard`，按「来源群→所属分片」过滤返修池，两片各修各的、不抢同一条。
5. `scripts/config/chat_sources.json`：每个群新增 `shard` 字段。**A 片（6 群）**＝turkey_price / qingdao_latam / tianjin_latam / europe_price / cma_emc_pil / meijia_sales；**B 片（5 群）**＝cosco_hmm_msk_oocl / dalian_latam / huanan_latam / one_zim_hpl_csp / ningbo_space（华南↔ONE 转发对同留 B 片，且 ocr_cache 全局，转发图不受拆分影响）。
6. **定时任务 2→4**（Asia/Shanghai）：低频 A `0 9-13`、低频 B `5 9-13`、高频 A `*/10 14-18`、高频 B `5-59/10 14-18`。B 比 A 晚 5 分钟错峰，避免两片同时抢宿主 dws。
7. 源目录 `sync` 到运行副本 `~/.qwenworkcn/skills/freight-rate-heartbeat/`，字节比对复验 0 差异。

**新配置键**：环境变量 `FRT_SHARD`（A/B；不设＝全量、沿用旧无后缀目录，向后兼容）；`chat_sources.json` 群对象新增 `shard`。

**验证**：py_compile 五脚本通过；离线探针——不设分片返回 11 群、A 返回 6、B 返回 5、并集 11 交集空（无群掉出两片）；`pipeline.py status --shard A/B` 建出 `run_0920A/`、`run_0920B/`；`check_llm_out.py --shard` 空目录返回"全部干净"退出码 0；`rework._NAME2SHARD` 在 `FRT_SHARD=B` 下仍读全 11 群映射。

**并发安全**：两片目录完全隔离，`watermark.json`/`ocr_cache` 按 (群,槽) 与 (mediaId) 写、无交叉；最坏情况是某轮 >5 分钟导致另一片一轮被调度器 skip，无数据损坏、无 content_key 重复，下轮补拉。09-19 实测 11 群单轮 p50 2.7 / p90 4.8 / 最长 7.7 min，半量两片应显著低于错峰间隔。

**回退**：`git checkout` 上述 6 个 Skill 文件 + `chat_sources.json`（未分片前版本）→ 再 `skill_diff.py sync`；定时任务把低频 A/高频 A 的 payload 去掉 `--shard`、删掉两个 B 任务即可恢复 2 任务全量。台账库数据本次零改动，无需数据回退。

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

## 2026-09-18（待审核队列·批量同步：改一条 → 同问题的其他条目一起改）

动机：队列里同一批条目常常是同一个毛病（一条消息拆 8 个港、同群连续 30 条把船司识错），只能逐条展开改再放行，非常费时间。

- 新接口 `POST /api/pending/sync`（写令牌），两种模式共用一个口子：
  - `preview=true` → `scan_sync_targets()` 只读扫队列，回报「同字段且现值 == old」的其他待审条目（含每条摘要与 ids），不改一行数据；
  - 缺省 → `sync_pending_field()` 按前端**确认过的 ids** 执行。预览即所得：执行时逐条复核仍是 awaiting 且现值仍是 old，
    被别人放行/改过值的显式 `skipped` 并回报原因，绝不覆盖。
- 「同一个问题」的判定：同类目 + 同字段 + 旧值完全相同（含都没填=空串）。跨类目一律不互刷（锚点是运价就只扫运价，否则会把「舱位类型」写进运价行）。
- 可同步字段白名单 `manualdb.SYNC_SAFE_FIELDS`：船司/起运港/目的港/航线/柜型/舱位类型/船名航次/来源群/发送人/**有效期**。
  海运费、价格、亏舱费、箱量、T/T、目免、ETD、备注、**消息原文**一律不参与——每票独立事实，值相同不代表同一个错误；原文是逐字溯源文本。
  有效期是例外：一条消息整版报价常共用同一区间，识图错一位就是整批同错（反馈后加）。它是"一个控件、两个 payload 键"，
  故新增 `SYNC_KEYS` 复合映射：匹配值编码成 `"起|止"`（只比到日，容忍带时分秒写法），写回两键一起写，杜绝只刷起点刷出半截区间；
  服务端 `_sync_value_error` 另加顺序硬拒（闸门只管单字段枚举，管不到起晚于止）。另外拒绝把一批同步成空值，单批上限 `MAX_SYNC=500`。
- **只改值不放行**：同步后条目仍留在待审队列，`update_pending` 逐条重跑闸门 + 重判新增/更新，
  改完仍不合规的（如某群别名没登记）翻成 failed 并计入 `failed_check_ids` 回报，放行仍是人工动作。
- 留痕：`update_pending` 的 edit_log 条目多带一个 `sync_from=锚点id`；P1/P2 反向沉淀扫描只读 action/field/old/new，
  多出的键自然忽略，所以同步过的条目放行时照旧进观察流候选。
- 事务：`update_pending` 加 `commit=False`（沿用 `push_with_review` 既有约定），整批一个 `conn.commit()`，异常整体回滚。
- 前端：待审编辑卡底部新增「同步到队列」按钮 → 面板列出本卡改过的可同步字段（旧值→新值，有效期显示成区间）
  + 队列同问题条数 + 可展开候选明细（含每条当前区间），点「同步 N 条」再二次确认；
  快照基线在展开编辑卡时取（`PD.snap`），条目放行/驳回或翻页掉出列表即失效。
- **顺手修掉一个老显示缺陷**（不是本次引入）：`pdGroupRowHtml` 只在整表重画时重建，`pdApplyCheckState` 只更新展开的编辑卡内部
  → 改完有效期/船司，队列那一行仍显示旧值，卡头「未过 N」与「全部放行（N 条）」也不跟着翻转，容易被当成没保存上再改一遍。
  修法：拆出 `pdGroupCellsHtml`、显示行挂 `pd-grow-{id}`，保存成功后 `pdRefreshRowDisplay(row)` 就地重刷该行 + 本批两个计数 + 整批放行条数。
- 回归：`manual_check` 6.85 段 **18 条断言**、全套 **117 项全绿**；`web_check` 另加 3 条结构断言（同步入口在位 / 保存后就地刷行 / 有效期成对）。
  两条关键实测证据：① 把一条缺目的港的 failed 行改到 passed → 条目仍在待审队列且 `/api/stats.ratesTotal` **15→15 一条没多**
  （代码层写业务表只有 `approve_pending → push_with_review` 一条路，静态核过 update/sync/stage 都不调它）；
  ② 暂存同区间两条 → 锚点改成 09-20→09-26 → 同步另一条（起止一起变）→ 人工放行 → 读回业务表 `valid_from/valid_to` 就是新值。
- 生效条件：新接口要重启台账服务（当前跑着的进程是旧的，`/api/pending/sync` 会 404）；前端 index.html 是运行时直读，刷新页面即可。
- 本次无表结构变更（不加列、不加表），`pending_records` 只是 payload/edit_log 里多一个键。

### 同日第二轮：强匹配字段防覆盖 + 未过原因直显

- **防覆盖（用户反馈：随手打的值把原先正确的港口冲掉，事后看不出来）**。原来编辑是"输入即存"，`/api/pending/update`
  把手打一半的值连 payload 一起写了，只把 `check_status` 翻成 failed——原值就没了。现在两层拦：
  - 服务端 `manualdb.update_pending` 为权威：`STRICT_KEEP_FIELDS = 起运港/目的港/船司/航线/柜型/舱位类型`，
    闸门在这些字段上报未命中且这次确实改了该字段 → **该字段回退保留原值**，同一次编辑的其他字段照常落库，
    被回退字段不计 edit_log（没写进去的不算人工改动）、在返回值 `rejected[{field,input,kept,msg}]` 里报给前端，
    并记一条 `WARN 强匹配回退`。原本就脏、这次没碰的值不背锅也不会被顺手抹掉。
  - 前端 `pdSplitStrict/cnResolves` 只做纯判断（不改输入框，避免打字打到一半被抹），落不到主数据的值这次不发出去，字段标红；
    服务端回退回来的再还原输入框并 toast 说明保留了什么值。**批量同步同源**：候选只取服务端已存的 payload，
    `sync_pending_field` 遇到 `rejected` 记为 skipped（"…不在主数据，已保留原值未改"），绝不刷进整条队列。
  - 代价：编辑时不能再把主数据外的值"先存着回头处理"，也不会在卡片里清空某个字段（`collectFormFields` 本来就不带空值）；
    要新增港口/船司得走主数据或候选批准。
- **待审核「校验」列直显未过原因**：第一行 `未过 ＋N 项`，第二行 `.pd-why` 单行显示第一条原因、超宽 CSS 省略号截断、
  悬停 `title` 看全文（多条按"原因1；原因2"给全）；无原因文案的旧数据退回只显示"未过"。9 列宽度重排，校验列 7%→15%。
  实测原因长度：57 条未过里中位 18 字、最长 57 字，14 条有多项原因。
- 回归：`manual_check` 6.85 段升到 **21 条断言**（新增"打不进主数据 → 保留原值且其他字段照常保存"、"回退不留 edit_log"、
  "同步也不刷脏值"），`web_check` 结构断言升到 8 条。
- 服务重启：台账进程 11:32:47 与 12:14 前各被外部终止一次（日志无 Traceback、`manual.pid` 未删 → 不是走 `stop-manual.bat`，
  机器自 09-16 未重启），已按 PID 停旧起新；为加载本节全部改动共重启两次，现跑 **pid 34632**。
- 本轮 `manual_check` 出现 **5 项红，非本会话造成**：另一会话正在做港口改名迁移（11:27:43 删 `Tianjin New Port`，
  现规范名为 `Xingang`），于是"起运港候选含 Tianjin New Port""天津→Tianjin New Port 归一""多值交集"等用例按旧名断言而红，
  另有 carriers 52≠50 与船司 sort 两项也是对方在途改动。测试期望该跟到新名还是回滚改名，由做迁移的那一侧定，我不替对方改断言。
  需要留意的存量影响：`freight_rates` 仍有 **38 行 `pol='Tianjin New Port'`** 指向已不存在的规范名，
  要么补 `Tianjin New Port → Xingang` 别名，要么把存量迁到新名，否则销售端筛"天津"看不到这批历史价、体检也会报孤儿引用。

### 同日第三轮：舱位「船名航次」改选填（现舱消息常不给船名）

业务实况：群里一句"ONE 现舱 CALLAO 5高 / MANZANILLO 2高"只报港口+舱位类型，根本没有船名，旧口径把 `船名航次` 列必填，
只能逼着判定层编一个或写占位符。用户拍板改选填。口径改动要**四处一起动**，少一处就是文档与闸门打架：

1. 闸门 `manualdb.REQUIRED_BY_KIND["space"]`：`起运港/船司/船名航次/舱位类型` → **`起运港/船司/舱位类型`**。
   `PLACEHOLDER` 占位符规则不动：写了 `(未注明船名)` 仍按未填打回（选填 ≠ 可以瞎填）。
   身份键 `IDENTITY_COLS` / `_IDENTITY_CN` 里保留 `vessel_voyage`：给了船名的仍然按船名区分产品，不被合并。
2. 前端 `SPACE_FIELDS.vessel_voyage` `req:true → false`（现舱录入表单与待审编辑卡共用这一份定义，星号与必填空格一起消失）。
3. 契约文档：`Database/docs/agent-input-contract.md` 必填列 ✅→✕、`docs/standardization-review-spec.md` 的常量镜像同步。
4. 心跳技能：`scripts/llm_output_contract.md`、`references/data-review.md`（审查清单第 2 条）、`references/extraction-rules.md#23`
   三处改口径，并按"源→运行副本"同步到 `~/.qwenworkcn/skills/freight-rate-heartbeat`。
   顺手做一次全量比对，发现两份群聊规范已在漂移：台账批准候选时会把「别名登记日志」写进**项目内源目录**（今天 11:01），
   而心跳实际读的运行副本还停在 09-14 → 已同步，现两棵树 0 差异。以后改完 skill 必须做这个全量比对，不能只复制自己改的那个文件。

配套修的一个死路：待审卡片原来 `collectFormFields` 只收非空值，**"把某个字段的值抹掉"这件事根本传不到服务端**，
所以占位船名这类行只能驳回。新增 `collectCardFields(id)`：表单里被清空、而 payload 里原本有值的字段显式发 `""`
（`消息原文` 例外，逐字溯源文本永不清空），`pdInstantSave` / `collectPendingFields` / `pdSwitchKind` 三处改用它。
与上一轮的强匹配防覆盖不打架：清空 `目的港` 这类必填仍会被 `STRICT_KEEP_FIELDS` 回退保留原值，清空 `船名航次` 则真的落库。

回归：`manual_check` 新增 2 条（现舱不给船名 → 过机械校验；占位船名在卡片里清空 → 校验翻 passed 且 `rejected` 为空），
原「占位符船名 → failed」保留不动；全套 **117 项绿 + 5 项红**，5 项红仍是上一节说的那批改名在途用例，与本节无关。

### 同日第四轮：看板变慢的定位与修法（不是 tooltip 的锅）

用户反馈"改完这些之后网页明显变慢"。分层取证（同一进程内交替采样、只读真库副本，避开另一会话的 CPU 干扰）：

| 一层 | 耗时 |
|---|---|
| `connect()`（DDL + `_migrate` + 建索引，每请求一次） | 2.6 ms |
| `list_pending` 50 行（SQL + json 解析） | 2.5 ms |
| **`images_for × 50 行`（旧实现）** | **127 ms** |
| `images_for × 50 行`（新实现） | 0.2 ms |
| 整包 json 序列化 | 0.9 ms |
| `Master` 索引 | 冷建 26.6 ms / 缓存命中 0.001 ms（TTL 300s，主数据一提交即作废） |

- 根因：`_resolve_image` 精确名没命中就 `imgdir.iterdir()` 扫整个 `images/` 并逐个 `is_file()`。一条消息拆出的多行重复解析同一批 mediaId
  （本页 68 次引用其实只有 **6 个不同名**），而 `images/` 会随心跳下载持续增长 → 接口耗时随"行数 × 目录大小"线性涨，这就是"最近才变慢"的原因。
- 修法：`images/` 的 mtime 做目录指纹（一次 `stat`）+ 一次性建"mediaId 词干前 16 字符 → 文件名"索引 + 单个名字的解析结果 memo（4000 条上限防御）。
  **等价性不靠嘴说**：拿全队列 payload、两张业务表的 `image_file/image_url`、以及 `images/` 92 个文件的两种写法（含 `@词干` 形式）
  共 **185 个引用名**，逐一比对新旧实现结果 —— **0 差异**。目录增删文件下一请求即生效（mtime 变了就重建）。
- 列表瘦身：`/api/pending` 不再回传 `orig_payload`（前端 0 处使用，50 行占 53KB）；`edit_log` 保留（留痕核对与回归断言要读）。
  报表脚本 `ops/pending_review.py` 走自己的 sqlite 连接，不受影响。
- 实测（同一台机、另一个会话还在跑的时段）：`/api/pending` 50 行 339–474 ms → **11 ms**；approved 50 行 → **8 ms**；
  `/api/rates` 50 行 414–611 ms → **5 ms**；`/api/pending` 200 行 1.25–2.0 s → **16 ms**。
- 两个坑记下来：① **指纹计算里不能放被缓存的昂贵操作**——我第一版为算"目录有没有变"每次解析都 `iterdir` 全目录，127 ms 反而涨到 217 ms；
  ② 按命令行特征批量杀 `manual_server` 会连带杀掉 `manual_check` 自己起的 8791 测试实例（本轮回归就被打断过一次，报"连接被拒"先想到这条）。
- 回归：`manual_check` 仍 **117 绿 + 那 5 项改名在途红**，本次改动零新增红；服务已重启加载（pid 35648）。

### 同日第五轮：身份码芯片（用码把"这条有什么问题"粘给 agent）

- 形状：待审条目 `#<pending id>`；已放行行 `#<id>·<content_key 前12位>`；销售端台账详情 `key:<content_key 前12位>`。
  点击复制（clipboard API，局域网 http 非安全上下文自动退回 execCommand）。挂载位置：分组表格每行、展开编辑卡头、
  驳回日志卡、港群候选卡、台账详情「来源」块。注意 `target_key` 是"预判更新目标"不是本条身份，不拼进码。
- agent 侧协议写进 `Database/docs/agent-integration-spec.md` 第 6 节：三种码的定位 SQL + 诊断顺序
  （先 `errors` 机械闸门原话 → 再 `edit_log` 人工改动史（`sync_from`=批量同步来的）→ `orig_payload ↔ payload` 对照归一改写 → 台账行 `status/supersedes`）。
- 码只用于定位不用于改写：修改仍走既有接口，强匹配防覆盖与同步护栏照常。
- `web_check` 加结构断言钉住芯片在位；`js_check` 通过。纯前端改动，刷新页面即生效（无需重启）。
- 用户裁定后调整：**展开编辑卡头的芯片撤掉**（行级码在表格行里已有）；**组卡头部新增组级码 `组:<消息ID>`**
  （一条消息 = 主条目整体；看板显示截断前 12 字符、复制给全量；无消息ID 的单行兜底组不显示）。
  复制实现改为 `data-code` + 文档级事件委托——消息ID带斜杠/加号，拼 onclick 有转义隐患，一并消除。
  `agent-integration-spec.md` 第 6 节补 `组:` 码的定位 SQL（待审按 `payload LIKE`、台账按 `mid` 列）。

### 同日第六轮：主数据新增 BP 港群（巴西基础港）

- 用户口径：`BP` = 巴西基础港，是**港群**，成员 = 国家为巴西的全部港口。已建：
  `port_groups(name=BP, label=巴西基础港, kind=pod, members=44 港, note=口径来源与建组时间)`。
  成员按 `ports.country='BR' AND kind LIKE '%pod%' AND active=1` 取（国家列是 ISO 码，44 个全 active）。
- 建组前先做折叠键冲突检查（`manualdb.fold('BP')='bp'` 与全部 ports 名/别名/port_groups 名比对，**无冲突**，避免 ECSA 那次的误伤重演）。
- 端到端验证（正式 `/api/pending` 路径，验证后 force 删）：`目的港=BP + 柜型 20GP/40HQ/40NOR + 价 6820/7130/6030`
  → 自动拆 3 条、**全部 passed**、目的港归一为港群名 BP（群价语义：查任何成员港也会带出 BP 群价，与 NEB 同机制）。
  注意 `/api/validate` 是单条原样预检、不做多档展开，拿它验证多档记录会误报柜型/价格错——多档要走上库路径看。
- 航线派生不给 BP 猜子线（未登记映射，同 NEB 留空先例）：报价里航线要明示（如南美东），不填不猜。
- 并发注意：验证时另一会话正在写库，`POST /api/pending` 报过一次 `database is locked`（15:52:17），重试即过——
  多会话并行写库期间批量操作要带重试。存量待审里暂无目的港=BP 的行，下次心跳拉到含 BP 的消息会直接过闸。

## 2026-09-18（土耳其群 Istanbul 被改写：审核、主数据修正、20 行数据恢复）

用户裁决「群里出现的目的港原样可用，Istanbul 就是 Istanbul」。审核确认判定层无误（agent 写 ISTanbul 且 rule_basis 正确引用本群规范），
改写来自台账归一的**两层**：`alias_scope` 的 ISTANBUL/伊斯坦布尔→Ambarli，加上 `ports.Ambarli.aliases` 本身含 Istanbul/伊斯坦布尔
——只删别名不够，这解释了为什么之前删了 Izmit 还会犯同类错。
处理：新建 `ports` Istanbul（TR，TRIST）；从 Ambarli 别名删掉两个 Istanbul 写法；删两条 alias_scope；
登记作用域别名 EVPTY→Evyap、MEISIN→Mersin（错拼变体，不新增假港）；预检验证四类写法归一结果各自正确。
数据恢复 20 行：7 行被另一会话「同步候选扫描」写成 `Ist`/`istan`（主数据不存在），13 行被归一改写成 `Ambarli` ——
全部按各行消息原文恢复，原文两者都没写的 4 行（id 156/157/869/870，纯图片消息）留人工；EVPTY/MEISIN 存量 16 行重判，11 行转 passed。
两处遗留写进 `Database/ops/reports/Istanbul改写审核-2026-09-18.md`：① `payload` 存的是归一值（根因 `stage_pending` 先 expand_cn 再落库，
`manualdb.py` 另一会话 11:34 正在改，我没动）；② 同步候选扫描缺"目标值必须命中主数据才改待审行"的护栏。
主数据缺口（天津 90、YT 38、中远海特 19、BRVCO 6）需要命名口径，未擅自新建。

## 2026-09-18（天津按行业口径建 Xingang、中远海特/BREVCO/YT 补齐，待审 failed 193→57）

另一会话只删了错实体未建对的，且 `全国基本港.members` 仍悬空引用 `Tianjin New Port`。补：新建 `ports` Xingang
（CN／city 天津／locode CNTXG，aliases 含 天津·Tianjin·天津新港·**Tianjin New Port**，让 38 条历史行不改数据即命中）；
港群成员换成 Xingang；新建 carriers `COSCO SPECIALTY／中远海特`（原文以【中远海特】独立发价，SCAC 待补）；
作用域别名 `BRVCO→Vila do Conde`（同句 Manaus/Villa Do Conde 为证）、`YT→Yantian`
（OCR 列头「适用 YT/SK/NS/SH/NB/TJ/QD/DL（不含福建/厦门）」即八基本港）。存量重判后待审 failed 193→57、passed 430。
剩 57 行是三类口径待定：必填为空 32（正常交人工）、`+` 是否为并列分隔符（`+TAO` 与之冲突，且 manualdb 有会话在改）、
`ECSA via X` 是否豁免主数据命中 —— 均未擅自改判据，写在 `Database/ops/reports/Istanbul改写审核-2026-09-18.md`。
更正：Xingang 的 LOCODE 由我误写 CNTSN，按用户口径改为 **CNTXG**（aliases 同步替换，防止将来另建"天津"实体时两码撞在同一港点）；预检复验 天津/Tianjin/Tianjin New Port/Xingang/CNTXG 五种写法均归一到 Xingang。

---

## 2026-09-18（组内原子性：同一条消息拆出的子条目同进同退，不再一半已通过一半未过）

**缘由**：你要求"一个条目下但凡有一个子条目出问题，整个条目一起进未过"，否则审核员判断不了识别完整性——实例是 `4000/6000/6000/5200 NOR`，待审里只把 20GP 和 40HQ/HC 摆在"已通过"，NOR 躺在未过，人分不清是识别漏了还是在另一栏。

**动作**（只改判定状态与说明文字，**不改任何字段值、不代替人工放行**）：

1. `manualdb.GROUP_MARK = "组完整性"` ＋ `enforce_group_atomicity(conn, dry_run=False)`：按 `(消息ID, kind)` 归组，先剥掉旧标记得到每条**真实**错误，组内只要有一条真错未清 → 整组 `check_status=failed`，并在每条 errors **末尾**追加一条连坐说明（真实错误仍排在前面，审核员先看到自己的问题）；全组修好后标记自动摘除、整组回 passed。没带 `消息ID` 的行按 `row-<id>` 自成一组，不牵连别人。
2. **三个挂钩**：`stage_pending` 末尾（新写入即判组）、`approve_all_pending` 末尾（批量放行后剩下的组重判）、`manual_server.GROUP_TOUCHING` 端点之后统一重算（`/api/pending`、`update`、`approve`、`reject`、`restore`、`delete` 全覆盖；失败只 WARN 不影响主流程）。因为重算扫的是全表 awaiting，**任何一次待审写入都会顺手把历史拆开的组收口**。
3. **新端点 `POST /api/pending/regroup`**（`act_pending_regroup`，**默认 `dry_run=True`**）：给你"先看会翻多少条、再决定回灌"的口子，符合批量改写铁律。
4. **前端零改动**：连坐说明走的就是 `errors` 数组，列表行首行直显 ＋ 悬停全文（`pdGroupCellsHtml` 的 `why`）已现成。
5. **契约与 skill**：`agent-input-contract.md` 增补"子条目同进同退 ⇒ 同一条消息的所有记录必须填同一个 `消息ID`"；`extraction-rules.md` 新增规则 **26**（同一条消息拆出的记录必须带同一个消息ID，漏填＝自成一组、享受不到连坐），源→运行已同步（`ops/skill_diff.py` 复验 35/35 一致）。

**数据实况（本轮 09-18 复测，只读）**：待审 547 条 / 41 组，`消息ID` 覆盖率 **100%**；带连坐说明 211 条；**被拆到两边的组 0 个**；生产服务（8795）已加载新代码，`regroup dry_run` 回 `groups_fixed=0, flipped=0`。早前预览的"12 组 / 136 行待翻"已由后续心跳写入触发的全表重算自动收口，**不需要我再手工回灌**。你举的例子核对：含 `40NOR` 的 8 组现在整组同状态（`msgb4c87…` 22 条全未过、`msgjURvQXz…` 48 条全通过里 NOR 15 条与 20GP/40HQ 并列）。

**验证**：`manual_check` 新增 4 条断言（2 好 1 坏 → 整组未过；连坐说明落满 3 条而真实错误只落在坏的那条；改好坏的后整组回 passed 且标记清零；`regroup` dry_run 不落库），全套连跑两次**全绿**；`js_check`（3830 行）/ `web_check` / `validity_rule_check` 通过。

**事故留痕（两条，都归我）**：① 往 `manual_check.py` 插用例时，把带 ASCII 双引号的中文标签写进了双引号字符串（`ck("好的那两条带"组完整性"说明…")`），整文件 SyntaxError → 回归"0 项运行"看着像跑完其实没跑。已改 `「」`，规矩：**中文断言标签里要引号一律用「」**。② 一度报"天津两条红"，我没有先归因给另一会话的改名中间态就准备下结论，改用只读探针取证：`天津/Tianjin/Tianjin New Port/Xingang/CNTXG` 五种写法同组、历史 38 行不改数据即命中，红是改名在途，**断言一行未动**，临时诊断码已摘除干净（含顺手加的 `import os`）。

**新增小工具**：`Database/ops/skill_diff.py`——skill 源↔运行目录全量字节比对（默认只报差异，`sync` 才把"两边都有且内容不同"的文件源→运行覆盖，只增不删）。以前这一步靠记忆，最容易漏。

**回退**：代码侧删 `manualdb.GROUP_MARK` 与 `enforce_group_atomicity` 及其两处调用、`manual_server` 的 `regroup` 路由与 `GROUP_TOUCHING` 包装；数据侧调 `enforce_group_atomicity(conn)` 前先把各条 errors 里 `field="组完整性"` 的元素剔除、再按剩余 errors 是否为空重算 `check_status`（一条 python 即可，不动 payload/价格/原文）。

---

## 2026-09-18（更正上一条：整组"同进同退"只改归属，不改判定——连坐刷未过是错的）

**你的否决**：「已过就是已过，只是单纯出现在未过界面确保条目完整性而已」。上一条我把同组通过条目 `check_status` 直接刷成 failed 还塞进「组完整性」假错误，等于篡改机械校验结论，生产里已污染 **243 条判定 / 319 条带标记**（670 条待审中）。这条把机制摆回正确位置。

**新实现＝读取时派生，写路径一律不碰判定**：

1. `manualdb.GRP_KEY_SQL / GRP_BAD_SQL`（相关子查询按 `COALESCE(消息ID,'row-'||id)`＋`kind` 找同组是否有未过）＋ `_pending_where(..., group_aware=True)`：**未过页签**＝自己有错 **或** 同组有错；**已通过页签**＝自己通过 **且** 整组通过。两页签之和恒等于待审总数（实测 337＋333=670，不重不漏）。
2. `list_pending` 默认 `group_aware=True`，并给每行附派生 `group={total,bad}`；外层 SQL 起别名 `p` 供子查询引用。批量同步的候选扫描（`scan_sync_targets`）**不开** group_aware，口径不变。
3. 写入侧全部撤销：删 `stage_pending` 与 `approve_all_pending` 末尾的 `enforce_group_atomicity` 调用、删 `manual_server.GROUP_TOUCHING` 常量与"每次 POST 后重算"的包装（顺带省掉每次请求多开一个连接）。
4. 「一键放行全部已通过」同口径：只选**整组都通过**的行，回报新增 `held_by_group`（被同组未过条目押住的条数）；前端按钮计数走同一个 `check=passed` 查询，天然一致。
5. 前端只加两句派生说明，**行的"通过/未过"徽标一个字节没改**：组头加琥珀色「通过的仍算通过 · 整组一起看（同组 N 条未过）」；`PD_PAGE=50` 导致整组跨页时再加「本组另有 X 条不在本页」——不假装整组都看得见。
6. 遗留污染复原工具 `manualdb.group_mark_cleanup(dry_run=True)`（挂在原 `POST /api/pending/regroup` 上，端点名不变以兼容已发出去的对接说明）：剥掉旧「组完整性」标记，再按剩下的真实 errors 还原每行自己的 `check_status`。**默认只预览**。

**顺序依赖（重要，别搞反）**：生产服务（8795）现在跑的还是**旧代码**，它会继续把整组刷成 failed。所以必须**先重启加载新代码，再跑 cleanup**；先 cleanup 会被下一次待审写入重新污染。**当前生产数据我一行未动**，副本预览＝扫 670 / 剥标记 319 / 还原判定 243。

**验证**：`manual_check` 那组断言按新语义重写为 6 条（好的两条仍 passed；整组出现在未过页签、已通过页签不出现半截组；不给任何行写假错误；派生 `group.total/bad`；修好后整组回已通过；造一条被污染的旧行经 `regroup dry_run=false` 复原回 passed），全套连跑**全绿**；`js_check`（3853 行）/ `web_check` / `validity_rule_check` 通过。skill 规则 26 与 `agent-input-contract.md` 的机制描述同步改成归属口径，源→运行复验 35/35 一致。

**教训（已写进记忆）**：状态能由查询派生就别存、更别去改别人的结论字段——这条你早定过（"能算出来就别存"），我没套用；把"整组一起看"错实现成"整组一起判错"，本质是拿显示需求去改数据。

**回退**：读侧删 `_pending_where` 的 group_aware 分支与两个 SQL 常量、`list_pending` 的 group 附加；放行侧把三条 SQL 的 `AND NOT/AND " + GRP_BAD_SQL` 去掉；前端删组头两个 badge。数据侧无需回退（本轮没有新写入）。

---

## 2026-09-18（两对折叠同名港口合并：Vitoria/Vitória、'Aqaba/Aqaba ＋ master_save 加撞名拦截）

**现象**（你在待审里报的）：组 `msgihlNanTtNwGXBWhL9f1+Lg==` 里目的港 `Vitoria` 报"不在港口/港群主数据里，请从联想里选"，而**联想里两个 Vitoria 点哪个都是这句**，死循环。

**根因**（不是闸门判错，是前端索引把键整条丢了）：`ports` 里有两条在用港口 `Vitoria`(BR/BRVIX，种子 `nominated:南美东:海港名精确`) 与 `Vitória`(BR/BRVIT，`source=auto`)，前端 `foldKey()` 去重音后两者同名 `vitoria` → `buildAliasIndex` 按"一别名指向两实体就置 null 并丢弃"的规则**整个删掉该键** → 无论点哪条都查不到 → 报"不在主数据"。更糟的是服务端 `name_idx` 是"后来者覆盖"（`ORDER BY name` 让带重音那条赢），所以**绕过界面用 API 提交会被归到 `Vitória`**：前后端两套口径不一致。全库同型死键只有 2 对（另一对 `'Aqaba / Aqaba`，别名一模一样、亚喀巴同样点不中）。

**动作**（备份 `归档\freight_manual.pre-vitoria-merge-163914.db`，4.8MB，可 `--import` 回灌）：

1. **合并保留 `Vitoria`**（你选的）：`Vitória`、`BRVIT` 并入其 aliases，`Vitória` 置 `active=0`（停用不删行）。
2. **合并保留 `Aqaba`**：`'Aqaba`、`JOAQB` 并入其 aliases，`'Aqaba` 置 `active=0`——规范名取不带撇号的（业务好打），撇号写法保留为别名不丢。
3. **港群去重**：`BP.members` 里 `Vitoria` 与 `Vitória` 并排存在（同一实体当两个成员，双向展开会算两遍），去掉带重音的，44→43。
4. **写入口拦截（防再犯）**：`master_save` 对 ports 新增校验——在用港口的规范名/别名与**另一条在用港口**折叠撞名即拒绝并点名冲突对象；`active=0` 停用路径放行（否则永远合并不了）。顺序有讲究：**先停用重复行、再吸收别名**，反了会被这层新防护挡下。
5. **前端说实话**：`buildAliasIndex` 可选收集撞名对存进 `S.portAmbig`；`hardNormalizePort` 遇"因撞名被丢键"不再谎报"不在主数据里"，改说「有两条规范名折叠后同名（X / Y），先在库管理→港口合并」。
6. **存量重判**：20 条含 Vitor/Vitória 写法的待审行发 `fields={}` → **13 条转通过**，7 条仍不过但与本次无关（`ECSA via IPANAMA`、`QINGDAO+TAICANG`、`TAICANG+南沙`，属另一会话记的三项待定口径）。

**验证**：`Vitoria/Vitória/维多利亚` 三种写法都归一到 `Vitoria`；前端等价索引 `vitoria → Vitoria`（不再 null）；**已入库 7 条 `pod='Vitória'` 的报价按 `Vitoria` 筛选全命中**（`port_filter` 变体展开含带重音写法，不断链）；`Aqaba/'Aqaba/亚喀巴` 同理归一到 `Aqaba`；`audit_master` 的"规范名折叠后重复"1 项→**0 项**、error 级 0；港群断链复查无。`manual_check` 加 2 条断言（重新启用撞名港被拒、合并动作不被误伤），四套回归全绿（`js_check` 3919 行 / `web_check` / `validity_rule_check`）。

**待生效**：`master_save` 这层防护在 `manualdb.py`，**要重启 8795 才拦得住**（当前进程仍跑旧版，提速改动也等这次重启）；前端文案是静态文件，刷新即生效。

**回退**：两条 `ports` 的 `active` 改回 1、aliases 摘回原串（`Vitoria`=维多利亚,Vitoria,BRVIX；`Aqaba`=亚喀巴,Aqaba,'Aqaba,JOAQB），`BP.members` 补回 `Vitória`；或整库用上面那份备份 `--import` 回灌。

**补记（同日，你回"好"授权）**：待审 `#1147` 的目的港 `ITAGUAI` **不是死键，是真缺实体**——全库 `ita*` 只有 Itajai/Itacoatiara/Itaqui/Itapoá/Ita Enramada，`Itaguaí` 根本没有。按别名纪律新建 `ports` **`Itaguai`**（BR，pod；规范名取不带重音的，免得将来补 `Itaguai` 又造一对折叠死键；带重音写法 `Itaguaí` 登记为别名；`locode_ref` **留空待人工核**，怀疑是 `BRIGI` 但不替业务定；note 里写了来历）。同图三档 `#1145/#1146/#1147`（20GP 7475 / 40HQ-HC 8225 / 40NOR 7125，达飞青岛-Itaguaí 9.21-10.14）发 `fields={}` 重判 → 三条全 `passed`；**整条消息的 rates 组 54 条真错归零**，按整组归属从"未过"页签整体移入"已通过"（实测：已通过页签含该组 54 条、未过页签含 0 条），"一键放行"候选也从 0 变 54 条。待你判的业务口径：该报价备注写着**"停接（暂停接载）"**，要不要作为当前价入库（或只留痕/驳回）。
---

## 2026-09-22（消息级条款字段补齐：Freetime 只挂一档导致 F/T 列一半空）

**现象**（你报的）：组 `msgIvNR9dX3oEHl+W2zeCf0Uw==` 的 F/T 列"都匹配不到"。原文只有一行条款 `Freetime：10days 【HDL USD 25/柜】`，拆成两档柜型后 **20GP 带了 目免=10、40HQ/HC 是空的**（已入库行 399 有值、398 为空）。**不是主数据问题、也不是 AI 没看懂**（两行的 `备注` 里都逐字抄着这句），是拆分时消息级条款没对齐。

**动作**（纯机械兜底，零 AI/token 成本；**只补空、绝不改已有值**）：

1. `manualdb.MSG_LEVEL_FIELDS = ("目免", "T/T航程", "亏舱费")` ＋ `inherit_message_fields(records)`：按 `消息ID` 归组，组内该字段**只有唯一非空值**时补进空行。两种情况绝不动——组内出现**多个不同值**（业务上免箱期确实按柜型分档，20GP 7 天 / 40HQ 14 天是真事）、或**整组都没值**（不猜，交采集/人工）。
2. 挂在 `manual_server.act_pending`（暂存之前跑），`POST /api/pending` 响应新增 `inherited: [{消息ID, 字段, 值, 柜型, 来源群}]`，并记一条 INFO 日志——采集端能看见兜了几处，而不是被静默改写。
3. skill `extraction-rules.md` 新增**规则 29**（消息级条款必须逐档带全，闸门兜底不是借口；`HDL USD 25/柜` 是超期堆存费、留在 `备注`、不许塞进 `目免`），源→运行复验 37/37 一致；`agent-input-contract.md` 同步一行 `inherited` 说明。

**验证**：`manual_check` 新增 4 条断言全绿——只一档带值 → 另一档补齐 `10`；`inherited` 明细正确；两档不同值（7/14）→ 一律不动；整组无值 → 保持空。隔离副本端到端复现确认（临时端口 8792，未碰生产库）。

**未做/待办**：已入库那条 `40HQ/HC`（id=398）的目免仍是空——业务表只追加，补它要走"追加一条修正记录＋留痕"，等你发话，我不代改也不代放行。另外本轮跑回归时撞上两件事（都属另一会话在途，未代改）：① `token_manual.txt` 只剩 4 字节，本机令牌短到回归断言都过不了，局域网鉴权形同虚设，建议删文件让服务重生成；② 新加的「港口覆盖率」校验把原文里的**起运港**也算成"漏识目的港"（`MSC 宁波-桑托斯…` → 报 `未识别的目的港：Ningbo`），以 error 身份挡放行，会被整组归属放大成整条消息卡死，且 `Spanish base ports` 港群成员 `Gijon/Malaga` 在 `ports` 里不存在（2 条 error 级断链）。
