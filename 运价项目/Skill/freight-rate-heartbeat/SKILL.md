---
name: freight-rate-heartbeat
description: 钉钉群聊海运运价与舱位自动收集，写入「运价台账」待审核库。按水位线拉取指定群/置顶群消息，识别运价图片、文字报价、现舱/加班船/售罄动态，判定前先读该群的业务规范（references/群聊规范，含带"列+群"作用域的缩写表）以降低误判，产出符合台账 agent-input-contract 的中文字段记录，经机械预检后只调 POST /api/pending 进待审核（人工在看板放行才落业务表）；每条记录保留逐字原文与图片溯源。当用户提到"拉群运价""运价入库""运价台账""整理运价""置顶群运价""heartbeat 运价""舱位表""现舱""某个群的报价""今天有没有新运价""历史价格对比"时使用。
version: 5.22.0
---

# 运价心跳收集（钉钉群 → 运价台账待审核库）

## 当前生产入口（2026-10-12核对）

本机 A/B/C/D 四片定时采集由 8797 常驻服务入队并由独立 Worker 执行。实际逐消息链路为 prepare → classify → vision（有图）→ extract → inventory → coverage → audit → normalize → validate → submit → readback；全群对账后推进水位。下文模板/pipeline流程保留为旧轮次恢复说明，不得与编排器同时采集同群。现行运行与维护入口以 `ops/freight-orchestrator/README.md` 为准。

拉群消息 → 落盘 → **record_builder.py 模板直出候选记录** → LLM 只复核异常件 → JSON无写入预检 → Agent结合主数据/群知识/官方资料修正一轮后直接送审 → **POST /api/pending 进待审核**。

柜型未明示但价格明确时，必须先查同船司、相似航线或相近港口、相近日期和费用口径的已确认报价；有唯一稳定口径就填写柜型、记录参照并标 `AI判断=是`。禁止把整条压成“保留原文待核对”、直接留空或丢弃；只有参照冲突时才保留未知，其他明确字段仍完整提取。所有群规范均显式引用此规则。
成本原则：**确定性规则全部脚本化，LLM 只做模板未命中判定与首次识图**；识图结果按 mediaId 缓存跨群复用。

全局航管识别口径见 `references/publisher-identity-rules.md`。群发、消息量和完整工作日稳定性由确定性规则计算；身份识别与报价内容判定分开。采集识别与「账号管理 → 报价发布人」页面共用 `scripts/config/publisher_roster.json`；确认、排除、恢复按群＋账号同步。未知、候选和明确排除账号不生成正式报价，但原始消息仍必须以 evidence-only 待审核行保留，不能因身份不确定而丢失。每个解析阶段读取名单修订号，提交前变化则重新解析，不能使用过期名单继续提交。

明确只有附加费调整、没有基础报价的通知不生成运价或 evidence-only 占位；原图与消息 ID 保留在采集证据中，适用规则沉淀到对应群业务知识。若同一消息同时包含完整基础报价，照常生成报价，附加费只进备注。

## 目录导览（每个子目录都有 Instruction.md，用人话解释里面每个文件干什么、调用哪个后端接口/对象）

- `Instruction.md`（本文件所在根）：整体流程、运行态落在哪、如何安装运行。
- `scripts/`：可执行流水线（`pipeline.py` 编排、`record_builder.py` 抽字段、`ledger_client.py` 调台账、`watermark.py` 水位、`validity_parser.py` 有效期）。详见 `scripts/Instruction.md`。
- `scripts/config/`：两份配置（`ledger_contract.json` 枚举+台账连接+运行态目录；`chat_sources.json` 要爬的群）。详见 `scripts/config/Instruction.md`。
- `references/`：按需读的口径文档（`extraction-rules.md` 抽取口径 / `runbook.md` 坑位手册 / `learnings.md` 纠错记忆 / `data-review.md` 只读体检 / `pending-quality-review.md` 主动业务复核）。详见 `references/Instruction.md`。
- `references/群聊规范/<群名>/规范.md`：**逐群业务规范，处理该群消息前必读**。26 个群各一份，含定位、报价格式、**缩写表（带"列 + 群"作用域，如 `NB` 在起运港列=宁波而非北巴）**、该群踩过的坑、解析要求；标「待补」的条目禁止猜测填充。检索规则与目录↔slug 对照见 `references/群聊规范/README.md`。

## 铁律（本版最重要的一条变化）

- **唯一写入口**是运价台账 `manual_server` 的 `POST /api/pending`。绝不碰 `/api/push`、`/api/record/*`、`/api/master/save`（那些是人工口子）。用户已授权的维护例外是 `/api/agent/master/ensure`（有依据新增港口/群内别名、不覆盖）和 `/api/agent/pending/repair`（有依据修复未审核Agent条目、保留编号和原始快照）。修复后补同源漏行必须只传缺失产品并调用 `ledger_client.submit_missing()`；禁止普通 `submit()` 重提整组导致已修复行被撤换。
- **写进去只是"暂存待审核"，不代表入库。** `pipeline.py` 提交成功、水位推进后就收工；条目是否进业务表，由人在台账「待审核」页点"放行"决定，与 agent 无关。
- **归一只有台账 `Master` 一个真源。** skill 不再自拼港口/船司/航线映射、不再拆多港。识别到什么就发什么（可发中文/别名/港群名），解析与命中判定交台账 `/api/validate`。港群覆盖多港时**用港群名存一条，不拆成员**。

## 台账契约（唯一事实源 = `Database/docs/agent-input-contract.md`）

- 请求体 `{"kind"|"table":"rates|space", "origin":"agent", "records":[中文字段记录,...]}`，一次 ≤1000 条（`pipeline.py` 自动分块）。
- 记录键 = 台账中文列名。核心硬规则：
  - `起运港`/`目的港` 命中主数据港口或港群名；`船司` 命中 `carriers`；`航线` 填了必须命中（可空）。台账对这四字段做强校验，命中不了 → 该条 `check_status=failed`（进待审核带 errors，不污染业务表）。
  - `柜型` 最终只存 `20GP`/`40HQ/HC`/`40NOR`/`45HQ/HC`；入口 `40GP`/`40DC`/`40DV` 按当前业务口径归入 `40HQ/HC` 价格档；`舱位类型` 只能 11 档（见 `ledger_contract.json.space_type_options`；2026-09-11 加了 空班/跳港/首航）。45HQ/45HC 独立保留，不映射成40HQ/HC。**台账闸门现在也校验柜型与舱位类型枚举**，非法值进待审核带 errors，不再只靠 skill 自守。
  - `海运费USD`/`价格USD`/`目免`/`亏舱费`/`T/T航程`/`箱量` = 非负整数（`pipeline.py` 会 int 化）；`币种` = 三位英文代码，缺省 USD，EUR/CNY 等原币金额直接保留，不换汇。
  - 运价必填：`起运港,目的港,船司,柜型,海运费USD` + 有效期；`币种`缺省按 USD。舱位必填：`起运港,船司` + 有效期，并且 `目的港/航线/航线服务` 至少填写一项，保证销售可以检索。`地中海`这类区域填航线；`WSA/MD1`这类服务码填航线服务；不得塞进目的港。`船名航次`与`舱位类型`可为空（非空舱位类型仍必须属于 11 档；`有效期起`/`有效期止` 至少一端，ISO `YYYY-MM-DD`，别发 `9.10-9.16` 区间串）。**两库都强制有效期非空**；报价没写时由 `validity_parser.derive` 兜底：有 ETD→`[消息日, 开船日]`（不标）；既无区间又无 ETD→`[消息日, 消息日+2天]` 并置 `AI判断=是`（看板在"有效期"处打"AI判断"标签）。详见 `references/extraction-rules.md` 规则 17。
  - `消息原文` 逐字带（是新增/更新判定键）；`消息时间` **必带真实群消息时间**（agent 口径服务端保留它，折叠/历史按此排序）。`消息ID`=openMessageId 利于溯源去重。

## 固定资源（当前部署私有配置，换环境重新获取）

台账服务 `manual_server.py`：默认 `http://127.0.0.1:8795`，写令牌 `Database/token_manual.txt`。连接参数在 `scripts/config/ledger_contract.json` 的 `db_conn`，可用环境变量 `FRT_DB_URL` / `FRT_DB_TOKEN` 覆盖。
本机 Python：解释器随 `pipeline.py` 走（`sys.executable`）。所有路径由 `scripts/paths.py` 统一解析；运行态（`.work` 暂存、`watermark.json`、`ocr_cache`）落在**包外**——位置取 `config/ledger_contract.json` 的 `runtime_dir`（默认 skill 同级的 `heartbeat-data/`），可用环境变量 `FRT_RUNTIME_DIR` 覆盖。

## 心跳主流程（水位线拉取式，低成本四段）

首次启动先运行 `python scripts/pipeline.py start --date <MMDD> --shard <A/B/C/D>`，读取返回的 `data.state_file`。新批次无产物时安全创建状态；已有状态原样保留；状态缺失但存在本轮产物或状态损坏时 ABORT，不重建 `new_ids`。`advance` 必须已有状态。状态写入使用原子替换，避免中断留下半份 JSON。

1. **取水位线**：`python scripts/watermark.py get` → 每群 `{since, cid, name, default_pol}`。
2. **逐群拉消息**（每群一次 Bash，dws 不能拼接/重定向；`--jq` 必须带 `q` 引用字段）。`pipeline.py next` 会打印全部命令与存盘路径：
   ```bash
   dws chat message list --group "<cid>" --time "<since>" --direction newer --format json \
     --jq ".messages[] | {t:.createTime, s:.sender, id:.openMessageId, img:[.resourceRefs[]?|.resourceId], c:.content, q:{t:.quotedMessage.createTime, s:.quotedMessage.sender, id:.quotedMessage.openMessageId, c:.quotedMessage.content}}"
   ```
   全部为空 → 静默结束。有消息 → **逐字**落盘 `.work/msgs_<MMDD>/<slug>.json`（顶层数组，字段 t/s/id/img/c/q 照抄）。
3. **record_builder 直出 + LLM 补**：`pipeline.py advance` 到 BUILD 阶段自动跑 `record_builder.py --date <MMDD>` →
   `draft_rate.json`/`draft_space.json`（record 已是**中文字段候选**）+ `needs_llm.json`。稳定模板先过结构指纹（港口名/数量、条目数、产品唯一性）；指纹失败整条进入 `needs_llm`，禁止模板部分结果与 Agent 补差额混合。LLM 只处理三类：
   ① `needs_llm` 未命中消息；② `_meta.conf=med` 复核；③ 新 mediaId 首次识图（识完**必须写 `.work/ocr_cache/<mediaId>.json`**，同图跨群不再识）。
   **判定某个群的消息前，先读 `references/群聊规范/<群名>/规范.md`**——缩写作用域（`NB` 在起运港列=宁波）、报价格式、该群已知坑都在里面；规范里标「待补」的条目一律不许猜，按全局口径留空交人工。
   带 `q` 引用回复的语义（售罄/调价）由 LLM 产出 `llm_out_<D>/*.json`（契约见 `scripts/llm_output_contract.md`）。
4. **JSON首次预检→Agent修正一轮→pending最终校验**：BUILD的JSON每块调用一次`POST /api/validate`。失败进入`AGENT_REPAIR`，`precheck_review.json`提供错误、主数据快照和本群/共享规范路径；Agent查库匹配并结合群知识、原文/原图，必要时搜索官方资料，有明确依据的缺港/群内别名走受限`ensure_master`。最多修正一轮，不能编造数据凑通过率。修正源OCR/llm_out后`advance`重建，下一次`advance`直接提交`POST /api/pending`，不再复检修正后的JSON。没有依据的字段保留未知，也送pending，由内部最终校验标明错误，留在待审核交人工；后检失败不再进入AGENT_REPAIR，不自动调用repair_pending。无法完成结构化解析、判为噪声或身份不确定的消息，自动生成 evidence-only 待审核行，保留原文、消息ID、来源和原因，不猜业务字段。提交失败只因传输故障才重试；提交成功后所有本轮消息都必须有待审核覆盖，水位才推进。任务不得停在`LLM_PENDING`或`COVERAGE_PENDING`等待用户，必须继续处理并以`DONE`收尾；DONE仅表示本轮消息已送审，不代表全部校验通过或已放行。

**带价现舱双入**：任何带可入账金额的条目都先识别为运价(kind=rates)；若同一行/同一表还包含舱位事实（箱量、舱位类型、库存、截关等），再额外生成一条同源舱位(kind=space)。金额只进运价的 `海运费USD`，`价格USD`、舱位类型、箱量和箱型箱量只留在舱位；运价只接收港、船司、柜型、海运费、ETD、船名航次、有效期和备注等兼容字段。普通带价运价只入运价，无价现舱动态只入舱位。
**旧价不改标**：改价/售罄由 LLM 产**新记录**（insert），旧行不动，当前价由台账折叠视图 `fold` 现算；同 `起运港+目的港+船司+原文` 全同的提交，台账自动判"更新（留痕）"。

## 查价 / 历史（只读台账接口，不再读钉钉）

`GET /api/rates` `/api/space`（扁平）、`GET /api/rates_grouped` `/api/space_grouped`（折叠+历史轨迹）。参数 `pol/pod/carrier/route/ctype/group/status` 等，港群自动双向展开。免写令牌。
主数据合法值：`GET /api/master?full=1`（港口/船司/航线/港群），归一以它为准。

## 图片（新看板要显示图）

台账库不存原图字节，只存引用；看板从 `Database/images/<文件名>` 取真实文件显示（缺图渲染"图片未同步"）。
带图消息：LLM 首次识图时 `dws` 下载原图 → 复制进 `ledger_contract.json.db_conn.images_dir`（即 `Database/images/`），**文件名以 mediaId 开头**（`_resolve_image` 按 mediaId 前缀匹配）；`pipeline.py` 把 mediaId 列表写进记录 `图片名`，看板按原文里的 `mediaId=@…` 占位也会派生。

引用消息中的图片不属于本条解析证据：只下载可展示副本、计算 SHA-256 并写入 `图片名` 供看板展示，不进入 vision、extract 或独立盘点，也不能据引用图片复制产品。引用正文仍可作为补充/修订上下文；只有本条消息自己的图片才允许识图解析。引用图片下载失败时保留失败原因并继续本条，不因展示型图片阻塞业务。

## 工具包（scripts/）

`chat_sources.json`（群画像 + `spec_dir` 规范路由）· `ledger_contract.json`（柜型/舱位枚举 + 台账连接配置）· `ledger_client.py`（台账对接层：validate/submit/submit_missing/master，唯一写入口）· `record_builder.py`（模板直出中文记录）· `pipeline.py`（水位拉取 + LLM 队列 + BUILD + 同步提交）· `watermark.py`（水位线）· `validity_parser.py`（有效期唯一口径）· `llm_output_contract.md`（LLM 输出契约）。
2026-09-17 新增规范执行层：`spec_index.py`（群规范机器入口：slug→规范文件、缩写表、「待补」条目、可引用锚点，队列项靠它注入）· `check_llm_out.py`（判定文件只读自查：溯源/禁填键/rule_basis 锚点真伪/是否疑似没读该群规范）· `spec_alias_diff.py`（本目录缩写表 ↔ 台账 `alias_scope` 差集与断链，已挂 `manual_check.py`）。**值域仍由台账闸门判**，这三个只负责"规范有没有被执行"的可验证性，只报告不拦。
`init_ledger.py`/`check_spec.py`/`price_history.py`/`validate.py`/`check_v3.py`/`save_pull.py` 及本机看板镜像已删除（钉钉写入链路废弃）。归一/校验以台账为准，别再自造。

## 记忆文档（解析前读，被纠错当场追加）

`references/learnings.md` 存**用户改进要求 / 纠错事项 / 已废弃做法**。交互采集开工先读；定时采集按公共提示词在本轮首次接受模板结果、OCR 或业务判定前读取现行规则一次，无新增消息不加载解析知识。用户提出新要求或指出做错时，立即追加一条带日期的记录，避免下次重犯。旧的已废弃规则不能覆盖当前输出契约。

## 换环境接手

0. **新机前置**：装 Python 3（只用标准库，无需 pip）；装 QwenWork 并把 `Skill/freight-rate-heartbeat.zip` 安装到它的 `~/.qwenworkcn/skills/`；装 `dws` CLI 并登录**在那 10 个群里的钉钉账号**；放行台账端口（跑 `Database/allow-lan.ps1`）。
1. 包是自包含的（`SKILL.md` + `scripts/`(含 `config/`) + `references/`），放进 skill 目录即可被识别；运行态不在包内，走 `runtime_dir`。
2. 台账服务起来：`Database/start-manual.bat`（监听 `0.0.0.0:8795`，局域网其它机器用本机 IP 访问）。
3. **换机器只改一处锚点**：`scripts/config/ledger_contract.json` 的 `project_root` 指到新机项目根（含 `Database/`+`Skill/` 的那层），或设环境变量 `FRT_PROJECT_ROOT`；`token_file`/`images_dir`/`runtime_dir` 都是相对它的片段，不用逐个改（也可写绝对路径）。群清单 `chat_sources.json` 的 cid 用 `dws chat search --query <群名>` 重查。
4. 首轮用新的空 `watermark.json`（在 `runtime_dir` 下），别沿用打包机水位。
5. 冒烟：`python scripts/paths.py` 看 `PROJECT_ROOT`/`token_file`/`images_dir`/`RUNTIME_DIR` 解析对不对；`python scripts/ledger_client.py` 打一发 master + validate 确认连得上。

## 定时任务

低频段 `0 9-13 * * *`（每小时）+ 高频段 `*/10 14-18 * * *`（每 10 分钟），Asia/Shanghai，tz 均带。改群清单只改 `config/chat_sources.json`；改判定口径改 `references/extraction-rules.md`；新增解析模板改 `scripts/record_builder.py`——不要把规则写回任务提示词。

生产由 8797 的 A/B/C/D 四分片调度控制。开工只读本技能入口；目录说明按路径/命令疑问查阅，抽取与纠错规则在首次解析前读取，对应群规范、继承规范及业务知识按群加载，图片/修正契约按阶段查阅。不得因按需加载而跳过适用规则或用关键词片段替代群规范；知识文件闸门继续检查完整文件。上下文压缩后从本轮状态、回执和判定恢复，并重核当前群知识和原始证据，不重复提交，不宣称切群自动清空上下文。结果仍须表格化，区分预计与实测耗时。

## 已授权的独立主动业务复核（5.12.0）

2026-09-29用户已授权将暂停的自审任务升级为主动业务复核，每10分钟自动领取、独立停启，不再等待人工贴消息ID。
主动业务复核详见 `references/pending-quality-review.md`：独立后台任务自动领取待审消息组，核对原文/原图及本群知识；校验通过也审查。有依据修正一次，已有编辑或最终未过保护并转人工；保持pending，人工放行。原 `references/data-review.md` 继续用于明确只读的专项体检，不替代主动复核。




## 实时工作监控（5.10.0）

旧版独立启动器若通过 `ops/agent-monitor/run_agent.py` 运行，会唤醒本机 8796 面板并注入 `FRT_MONITOR_RUN_ID`；8797 常驻编排器以 Trace Studio 为观测入口。执行器自动报告实际工具开始/结束，pipeline自动报告检查点、送审和未过计数。逐群拉取、读取知识、逐图识别、逐消息判断和预检修正前，调用 `ops/agent-monitor/monitor.py report` 上报动作及已知群/消息/文件；无证据的字段不填。上报命令和pipeline推进遵守用户安全步骤暂停，等待继续后才执行下一动作；在途命令及请求完成后暂停。监控异常不影响业务采集，上报失败需说明。事件说明工作进度，不证明业务正确；保持首次预检→一轮修正→直接pending，不添加复检或自动放行。模块与接口说明见 `ops/agent-monitor/README.md`。


应用内采集补充（5.10.1）：Codex内定时任务以CODEX_THREAD_ID/CODEX_SESSION_ID自动对齐本机监控，不依赖Windows包装器。pipeline推进前自动wake并上报control_ready。监控服务也可只读观察本项目采集会话，补接正在运行的一轮；观察和业务校验分开，保留原始事件时间，排除内部审批任务。上午B实际自动化开工先执行monitor.py wake。

航次矩阵新增原图维度清单核对，见 scripts/llm_output_contract.md「航次矩阵与原图维度清单」。多POL、柜型、船名航次与每图价格不能缩成备注或空占位；只校验结构对应，不声称自动证明原图识别完整。
