# 运价心跳 · 记忆文档

> 用途：存放**用户提出的改进要求**、**做错过要纠正的事项**、**已废弃别再碰的做法**。
> 用法：每轮心跳开工前先读本文件；用户提出新要求或指出做错时，立即在对应分区**追加一条带日期的记录**（倒序，最新在上）。别删旧条，只追加或标注"已过时"。

## 用户改进要求（最新在上）

- **2026-09-11｜识别到船名，但不得从船名反推船司。** 共船 / 舱位互换（VSA）常态下船东 ≠ 出这票报价的承运。`船名航次` 照抄，`船司` 只取报价来源（群画像 `profile.carriers`/正文明示/发送人语境），拿不准就留空交台账判 failed，别从 `MSC OSCAR` 之类船名硬填 `船司=MSC`。已落 `extraction-rules.md#18`、`llm_output_contract` 禁止事项#6、`data-review` #11（审查时也别把"船司≠船名公司"当错纠正）。

- **2026-09-10｜建拉取 cron 时，应建议（经许可）再建一个"台账数据审查 agent"定时任务。** SKILL.md「建议一并建『数据审查 agent』」段 + `references/data-review.md` 已定规范：审查 agent **先摸清库结构**（表/列/CHECK/枚举/主数据），再**只读**审查（`/api/audit`·`/api/pending`·`/api/master`），按测试踩坑+值规范的清单**归因**（数据脏 / 主数据缺 / 传入口径错），**给可执行修改建议**（点名到字段/端点/人工步骤），但**绝不自动写库**，改由人工放行。

- **2026-09-10｜消息原文要附带被引用消息一并传入。** 引用回复不能只存自己那条：`消息原文` 需带上被引用原文。实现：`record_builder.compose_text(msg)` 拼成「本条逐字全文 + 一行 `——引用 <时间> <发送人> mID=<被引用消息ID>——` + 被引用逐字全文」，`record_builder` 与 `pipeline` 两条装配路径都用它（`q.c` 非空才附）；备注不再重复抄被引用全文，只写引用结论。该串确定（同消息同引用→同串），继续作台账新增/更新判定与内容指纹的稳定键。

- **2026-09-10｜预校验一次即可，但每条耗时上限 0.5s。** 实测台账 `/api/validate` 1000 条 ≈0.086s、`/api/pending` 1000 条 ≈0.14s，远优于预算；"校验一次→传入"可行，无需为性能拆分或跳过预检。
- **2026-09-10｜新看板要能显示图。** 台账库不存原图字节，只存引用；看板从 `Database/images/` 取真实文件。skill 需把识图下载的原图放进该目录（文件名以 mediaId 开头），并把 mediaId 写进 `图片名`。配套已给 `manual_server.py` 加 `/images/<名>` 路由（`_send_image`，unquote+NFC+防穿越）。

## 纠错事项（做错过，别再犯）
- **2026-09-17｜单文件前端禁起同名顶层函数。** `web/index.html` 只有一个 `<script>`，两个会话都往里加函数时，**同名顶层函数后者静默覆盖前者**（不是报错）。本次一例：新加的 `foldKey(box, el, i)` 覆盖了主数据别名索引在用的 `foldKey(s)`，联想/归一当场变坏；回滚后改用 `zf*` 前缀。规矩：往这个文件加函数前先 `grep 'function 你的名字(' web/index.html`；加完跑 js_check + 与改前备份 diff，确认没删掉别人的行。

- **2026-09-10｜契约文档与实际入参键名漂移。** `agent-input-contract.md` 写 `kind`，服务端 `_kind()` 实际先读 `table`——按文档传 `kind` 曾 500 `table must be "rates" or "space"`。已让服务端 `kind`/`table` 都收；skill `ledger_client` 发 `table`。**教训：改 skill 前务必对真实服务打一发，别只信文档。**
- **2026-09-10｜"消息时间不用传"是错的（对 agent）。** 服务端只在 `origin=manual` 才盖当前时间；`origin=agent` 保留客户端传值。折叠/历史按 `消息时间` 排序，**agent 必须带真实群消息时间**，否则当前价判定错乱。已在契约文档改正、`pipeline.py`/`record_builder` 强制注入。
- **2026-09-10｜柜型/舱位类型枚举台账不强校验，别指望服务端兜底。** 台账 `validate_record` 只硬校验 起运港/目的港/船司/航线 命中主数据 + 必填 + 整数 + ISO 日期，**不校验柜型/舱位类型枚举**。旧 base 舱位类型有第 9 档"报备"，台账契约只有 8 档。已在 `ledger_contract.json` 砍到 8 档、`pipeline.py._enum_ok` 自守，非法枚举直接丢不进待审核。**（2026-09-11 部分失效：柜型现在台账有三档硬闸，`45GP/40OT/40GP` 一律打回，且 `norm_ctype` 不再把 40 开头悄悄归成 `40HQ/HC`；舱位类型八档仍由 skill 自守，台账不查。）**
- **2026-09-11｜合并船司"取第一家"= 丢数据还不报错。** 宁波群 `EMC/COSCO`、`HPL/ZIM/MSK` 被判定层写成 `EMC`、`HPL`，伴生船司静默消失，而且**校验通过**（单值当然命中）。现台账闸门会按 `/ 、 ,` 拆分逐个归一、全命中展开成 N 条，未知但像船司缩写的 token 自动补 `carriers` 主键（note 带"auto：待人工复核＋来源"）。skill 侧只照抄原串，不挑、不拆、不写主数据。见 `extraction-rules.md#19`。
- **2026-09-11｜表头没有目的港列 ≠ 没有目的港。** 宁波群空班/加班船表按 `南美西墨西哥/加勒比/南美东` 分段，段名就是港群（`WCSA+MEX`/`CAR`/`ECSA`）。当时判定层认定"无目的港"，转而把 ETD 格里的说明文字塞进 `目的港`，产出 `(未注明，只去Manzanillo`、`ESE)` 这类碎片（`ESE` 还是服务码不是港）。现在：段名回填港群名存一条、合并段存两条；含括号/说明词的串台账会单独报"含说明文字"。见 `extraction-rules.md#20`、`#22`。
- **2026-09-11｜OCR 少认了行却不吭声。** 那张图实际 39 行，`ocr_cache` 只留 14 行，把整段 20 行"X号跳港NB"丢了，只在 `footer_notes` 写一句"未逐条入库"——下游完全看不出来。规则：判断不入库的行必须在某条记录 `备注` 里写明"本图另有 N 行…未拆"，或者干脆入库。见 `extraction-rules.md#24`。

## 已废弃做法（一律不再碰）

- 写钉钉 AI 表格（`dws aitable record create` / baseId `6LeBq…`/`mExel2…` / fieldId 映射 / singleSelect 选项比对）——本版起全部删除。
- 本机旧看板镜像（`local_board/freight_rate.db` + `mirror_local` + 依赖 `portnorm.py`/`freightdb.py`）——删除；归一交台账 `Master`，skill 不再自拼第二套映射。
- `init_ledger.py`（建钉钉 base）、`check_spec.py`（比对钉钉字段选项）、`price_history.py`（查钉钉快照，历史改走 `/api/rates_grouped`）、`validate.py`（规则移交台账 `/api/validate`）、`check_v3.py`/`save_pull.py`（钉钉批次产物）——已移除。
- 多港拆分（旧 `expand_all` 把 `/` 分头发多条）——删除。多港按港群名存一条，或原样发 `/`-串交台账 `port_side` 解析成一条 joined，不拆。

## 主数据缺口（2026-09-10 已补）

- 已 `/api/master/save` 补：航线 `地东`、`欧基港`；起运港语义群 `华南基本港`（port_group，kind=pol，成员先给广东核心基本港 Nansha/Shekou/Yantian/Chiwan/Shantou/Hong Kong，可在台账「主数据库→港群」随时调）。
- **`拉美` 不能当航线**：它是 `南美西/南美东/中美洲/加勒比/墨西哥/内陆河运` 共用的 region，台账 `route_idx` 把 `拉美` 判为歧义整条丢弃——填了必被打回。故 `record_builder` 的 COSCO 华南订舱指南模板改为 **`航线` 留空**（space 航线可空），不再发 `拉美`。以后遇 `拉美` 这类"伞形区域词"一律留 `航线` 空让人工补，别硬塞。

## 试运行发现（2026-09-10｜skill↔台账端到端 10 群各 1 条）

- **链路跑通**：dws 拉取 → 3 图首次识图（原图进 `Database/images/`、OCR 进 `ocr_cache/`）→ `record_builder` 0 命中（自由文本/图，正常）→ LLM 补 → `pipeline` 枚举自守 → `/api/validate`+`/api/pending`。30 条进待审核，脏值没进业务表。
- **本轮补的主数据（人工维护，非 agent 自动写）**：`Norfolk/Philadelphia/Baltimore` 追加别名 `ORF/PEL/BAL`；`Ambarli` 追加 `Istanbul/伊斯坦布尔`（销售写"ISTANBUL"实指伊斯坦布尔集装箱门户 Ambarli）；新建港口 `Aliağa`（en=Aliaga, TR, TRAAG）。
- **匹配是"折叠后精确命中"（`fold()` 去音标/大小写/标点，无相似度）**：`CLSAI`→`San Antonio` 是**正确命中**（CLSAI 是圣安东尼奥智利的 LOCODE），不是误伤；`中谷`→`ZGWL` 能命中。同名多国裸发（`Cartagena`/`Manzanillo`）判歧义 failed，要消歧用 **LOCODE**（`COCTG`/`MXZLO`）；带括号规范名 `Manzanillo(MX)` 实测匹配不上。详见 `extraction-rules.md` 规则 16。
- **有效期口径（已定稿）**：`VALIDITY_REQUIRED_KINDS={rates,space}`——**运价、舱位都必填有效期**。报价没写时由 `validity_parser.derive` 兜底：有 ETD→`[消息日, 开船日]`（不标）；既无区间又无 ETD→`[消息日, 消息日+2天]` 且置 `AI判断=是`。台账 `freight_rates`/`space_records` 加 `ai_flag` 列，看板在"有效期"处渲染"AI判断"标签。规则见 `extraction-rules.md` 规则 17。

## 维护约定（2026-09-10｜两份副本，别再改错）

- 本 skill 有**两份副本**：
  - **源副本（唯一真源，改这里）**：`运价项目/Skill/freight-rate-heartbeat/`，旁边 `freight-rate-heartbeat.zip` 是打包产物。
  - **运行副本（QwenWork 实际加载）**：`~/.qwenworkcn/skills/freight-rate-heartbeat/`（与 docx/pdf 等同级）。
- **同步方向是 源→运行**：每次"打包/安装"会用源副本覆盖运行副本。所以**只改源副本，改完重新打包/安装到全局目录**；**不要直接改运行副本**——会被下次安装冲掉（2026-09-10 我的 `derive`/`AI判断` 改动就因此被 `compose_text` 那次安装覆盖过一次）。
- 台账侧（`Database/manualdb.py`、`web/index.html`、`docs/agent-input-contract.md`）不在 skill 包里、不参与这个同步，直接改即可。
