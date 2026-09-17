---
name: freight-rate-heartbeat
description: 钉钉群聊海运运价与舱位自动收集，写入「运价台账」待审核库。按水位线拉取指定群/置顶群消息，识别运价图片、文字报价、现舱/加班船/售罄动态，判定前先读该群的业务规范（references/群聊规范，含带"列+群"作用域的缩写表）以降低误判，产出符合台账 agent-input-contract 的中文字段记录，经机械预检后只调 POST /api/pending 进待审核（人工在看板放行才落业务表）；每条记录保留逐字原文与图片溯源。当用户提到"拉群运价""运价入库""运价台账""整理运价""置顶群运价""heartbeat 运价""舱位表""现舱""某个群的报价""今天有没有新运价""历史价格对比"时使用。
version: 5.1.1
---

# 运价心跳收集（钉钉群 → 运价台账待审核库）

全自动：拉群消息 → 落盘 → **record_builder.py 模板直出候选记录** → LLM 只复核异常件 → 预检 → **POST /api/pending 进待审核**。
成本原则：**确定性规则全部脚本化，LLM 只做模板未命中判定与首次识图**；识图结果按 mediaId 缓存跨群复用。

## 目录导览（每个子目录都有 Instruction.md，用人话解释里面每个文件干什么、调用哪个后端接口/对象）

- `Instruction.md`（本文件所在根）：整体流程、运行态落在哪、如何安装运行。
- `scripts/`：可执行流水线（`pipeline.py` 编排、`record_builder.py` 抽字段、`ledger_client.py` 调台账、`watermark.py` 水位、`validity_parser.py` 有效期）。详见 `scripts/Instruction.md`。
- `scripts/config/`：两份配置（`ledger_contract.json` 枚举+台账连接+运行态目录；`chat_sources.json` 要爬的群）。详见 `scripts/config/Instruction.md`。
- `references/`：按需读的口径文档（`extraction-rules.md` 抽取口径 / `runbook.md` 坑位手册 / `learnings.md` 纠错记忆 / `data-review.md` 台账数据审查 agent 规范）。详见 `references/Instruction.md`。
- `references/群聊规范/<群名>/规范.md`：**逐群业务规范，处理该群消息前必读**。11 个群各一份，含定位、报价格式、**缩写表（带"列 + 群"作用域，如 `NB` 在起运港列=宁波而非北巴）**、该群踩过的坑、解析要求；标「待补」的条目禁止猜测填充。检索规则与目录↔slug 对照见 `references/群聊规范/README.md`。

## 铁律（本版最重要的一条变化）

- **唯一写入口**是运价台账 `manual_server` 的 `POST /api/pending`。绝不碰 `/api/push`、`/api/record/*`、`/api/master/save`（那些是人工口子）。
- **写进去只是"暂存待审核"，不代表入库。** `pipeline.py` 提交成功、水位推进后就收工；条目是否进业务表，由人在台账「待审核」页点"放行"决定，与 agent 无关。
- **归一只有台账 `Master` 一个真源。** skill 不再自拼港口/船司/航线映射、不再拆多港。识别到什么就发什么（可发中文/别名/港群名），解析与命中判定交台账 `/api/validate`。港群覆盖多港时**用港群名存一条，不拆成员**。

## 台账契约（唯一事实源 = `Database/docs/agent-input-contract.md`）

- 请求体 `{"kind"|"table":"rates|space", "origin":"agent", "records":[中文字段记录,...]}`，一次 ≤1000 条（`pipeline.py` 自动分块）。
- 记录键 = 台账中文列名。核心硬规则：
  - `起运港`/`目的港` 命中主数据港口或港群名；`船司` 命中 `carriers`；`航线` 填了必须命中（可空）。台账对这四字段做强校验，命中不了 → 该条 `check_status=failed`（进待审核带 errors，不污染业务表）。
  - `柜型` 只能 `20GP`/`40HQ/HC`/`40NOR`；`舱位类型` 只能 11 档（见 `ledger_contract.json.space_type_options`；2026-09-11 加了 空班/跳港/首航）。**台账闸门现在也校验柜型与舱位类型枚举**，非法值进待审核带 errors，不再只靠 skill 自守。
  - `海运费USD`/`价格USD`/`目免`/`亏舱费`/`T/T航程`/`箱量` = 非负整数（`pipeline.py` 会 int 化）。
  - 运价必填：`起运港,目的港,船司,柜型,海运费USD` + 有效期；舱位必填：`起运港,船司,船名航次,舱位类型` + 有效期（`有效期起`/`有效期止` 至少一端，ISO `YYYY-MM-DD`，别发 `9.10-9.16` 区间串）。**两库都强制有效期非空**；报价没写时由 `validity_parser.derive` 兜底：有 ETD→`[消息日, 开船日]`（不标）；既无区间又无 ETD→`[消息日, 消息日+2天]` 并置 `AI判断=是`（看板在"有效期"处打"AI判断"标签）。详见 `references/extraction-rules.md` 规则 17。
  - `消息原文` 逐字带（是新增/更新判定键）；`消息时间` **必带真实群消息时间**（agent 口径服务端保留它，折叠/历史按此排序）。`消息ID`=openMessageId 利于溯源去重。

## 固定资源（当前部署私有配置，换环境重新获取）

台账服务 `manual_server.py`：默认 `http://127.0.0.1:8795`，写令牌 `Database/token_manual.txt`。连接参数在 `scripts/config/ledger_contract.json` 的 `db_conn`，可用环境变量 `FRT_DB_URL` / `FRT_DB_TOKEN` 覆盖。
本机 Python：解释器随 `pipeline.py` 走（`sys.executable`）。所有路径由 `scripts/paths.py` 统一解析；运行态（`.work` 暂存、`watermark.json`、`ocr_cache`）落在**包外**——位置取 `config/ledger_contract.json` 的 `runtime_dir`（默认 skill 同级的 `heartbeat-data/`），可用环境变量 `FRT_RUNTIME_DIR` 覆盖。

## 心跳主流程（水位线拉取式，低成本四段）

1. **取水位线**：`python scripts/watermark.py get` → 每群 `{since, cid, name, default_pol}`。
2. **逐群拉消息**（每群一次 Bash，dws 不能拼接/重定向；`--jq` 必须带 `q` 引用字段）。`pipeline.py next` 会打印全部命令与存盘路径：
   ```bash
   dws chat message list --group "<cid>" --time "<since>" --direction newer --format json \
     --jq ".messages[] | {t:.createTime, s:.sender, id:.openMessageId, img:[.resourceRefs[]?|.resourceId], c:.content, q:{t:.quotedMessage.createTime, s:.quotedMessage.sender, id:.quotedMessage.openMessageId, c:.quotedMessage.content}}"
   ```
   全部为空 → 静默结束。有消息 → **逐字**落盘 `.work/msgs_<MMDD>/<slug>.json`（顶层数组，字段 t/s/id/img/c/q 照抄）。
3. **record_builder 直出 + LLM 补**：`pipeline.py advance` 到 BUILD 阶段自动跑 `record_builder.py --date <MMDD>` →
   `draft_rate.json`/`draft_space.json`（record 已是**中文字段候选**）+ `needs_llm.json`。LLM 只处理三类：
   ① `needs_llm` 未命中消息；② `_meta.conf=med` 复核；③ 新 mediaId 首次识图（识完**必须写 `.work/ocr_cache/<mediaId>.json`**，同图跨群不再识）。
   **判定某个群的消息前，先读 `references/群聊规范/<群名>/规范.md`**——缩写作用域（`NB` 在起运港列=宁波）、报价格式、该群已知坑都在里面；规范里标「待补」的条目一律不许猜，按全局口径留空交人工。
   带 `q` 引用回复的语义（售罄/调价）由 LLM 产出 `llm_out_<D>/*.json`（契约见 `scripts/llm_output_contract.md`）。
4. **提交台账**：`pipeline.py advance` 到 SUBMIT 阶段，对每块先 `POST /api/validate`（预检、不写库，只为失败告警），
   再 `POST /api/pending` 暂存。**全部批次返回 ok 才推水位**；服务没起/token 失效 → 不推水位，下轮重跑（自愈，可能重投由台账 content_key/待审核判重兜底）。
   连续 `SUBMIT_STALL_LIMIT` 轮零进展才 ABORT 交人工。

**双路分流**：有金额→运价(kind=rates)；现舱/舱位动态→舱位(kind=space)；带价现舱两边都入（金额进运价、箱量进舱位）。
**旧价不改标**：改价/售罄由 LLM 产**新记录**（insert），旧行不动，当前价由台账折叠视图 `fold` 现算；同 `起运港+目的港+船司+原文` 全同的提交，台账自动判"更新（留痕）"。

## 查价 / 历史（只读台账接口，不再读钉钉）

`GET /api/rates` `/api/space`（扁平）、`GET /api/rates_grouped` `/api/space_grouped`（折叠+历史轨迹）。参数 `pol/pod/carrier/route/ctype/group/status` 等，港群自动双向展开。免写令牌。
主数据合法值：`GET /api/master?full=1`（港口/船司/航线/港群），归一以它为准。

## 图片（新看板要显示图）

台账库不存原图字节，只存引用；看板从 `Database/images/<文件名>` 取真实文件显示（缺图渲染"图片未同步"）。
带图消息：LLM 首次识图时 `dws` 下载原图 → 复制进 `ledger_contract.json.db_conn.images_dir`（即 `Database/images/`），**文件名以 mediaId 开头**（`_resolve_image` 按 mediaId 前缀匹配）；`pipeline.py` 把 mediaId 列表写进记录 `图片名`，看板按原文里的 `mediaId=@…` 占位也会派生。

## 工具包（scripts/）

`chat_sources.json`（群画像）· `ledger_contract.json`（柜型/舱位枚举 + 台账连接配置）· `ledger_client.py`（台账对接层：validate/submit/master，唯一写入口）· `record_builder.py`（模板直出中文记录）· `pipeline.py`（水位拉取 + LLM 队列 + BUILD + 同步提交）· `watermark.py`（水位线）· `validity_parser.py`（有效期唯一口径）· `llm_output_contract.md`（LLM 输出契约）。
`init_ledger.py`/`check_spec.py`/`price_history.py`/`validate.py`/`check_v3.py`/`save_pull.py` 及本机看板镜像已删除（钉钉写入链路废弃）。归一/校验以台账为准，别再自造。

## 记忆文档（每轮开工先读，被纠错当场追加）

`references/learnings.md` 存**用户改进要求 / 纠错事项 / 已废弃做法**。开工先读；用户提出新要求或指出做错时，立即追加一条带日期的记录，避免下次重犯。

## 换环境接手

0. **新机前置**：装 Python 3（只用标准库，无需 pip）；装 QwenWork 并把 `Skill/freight-rate-heartbeat.zip` 安装到它的 `~/.qwenworkcn/skills/`；装 `dws` CLI 并登录**在那 10 个群里的钉钉账号**；放行台账端口（跑 `Database/allow-lan.ps1`）。
1. 包是自包含的（`SKILL.md` + `scripts/`(含 `config/`) + `references/`），放进 skill 目录即可被识别；运行态不在包内，走 `runtime_dir`。
2. 台账服务起来：`Database/start-manual.bat`（监听 `0.0.0.0:8795`，局域网其它机器用本机 IP 访问）。
3. **换机器只改一处锚点**：`scripts/config/ledger_contract.json` 的 `project_root` 指到新机项目根（含 `Database/`+`Skill/` 的那层），或设环境变量 `FRT_PROJECT_ROOT`；`token_file`/`images_dir`/`runtime_dir` 都是相对它的片段，不用逐个改（也可写绝对路径）。群清单 `chat_sources.json` 的 cid 用 `dws chat search --query <群名>` 重查。
4. 首轮用新的空 `watermark.json`（在 `runtime_dir` 下），别沿用打包机水位。
5. 冒烟：`python scripts/paths.py` 看 `PROJECT_ROOT`/`token_file`/`images_dir`/`RUNTIME_DIR` 解析对不对；`python scripts/ledger_client.py` 打一发 master + validate 确认连得上。

## 定时任务

低频段 `0 9-13 * * *`（每小时）+ 高频段 `*/10 14-18 * * *`（每 10 分钟），Asia/Shanghai，tz 均带。改群清单只改 `config/chat_sources.json`；改判定口径改 `references/extraction-rules.md`；新增解析模板改 `scripts/record_builder.py`——不要把规则写回任务提示词。

## 建议一并建「数据审查 agent」定时任务（建之前先征询用户许可）

搭好上面的拉取 cron 后，本 skill 还应**主动建议**用户再建一个独立的定时任务：**台账数据审查 agent**（先征求许可，用户同意才建；与拉取任务分开停/启）。
它的职责（详见 `references/data-review.md`）：**先摸清台账库结构**（表/列/CHECK 约束/枚举/主数据），再只读地审查入库数据，**发现问题就向用户给出可执行的具体修改建议**（改哪个字段、补哪条主数据、走哪个端点或人工步骤），但**绝不自动写库**——改由人工在台账「待审核」页/「主数据库」做。
