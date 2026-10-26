# llm_out 契约（Agent 唯一智能输出的格式）

每条待判定消息一个文件：`.work/llm_out_<MMDD>/<safe(msg_id)>.json`
`safe(msg_id)` = 非 `[A-Za-z0-9_.-]` 字符替换为 `_`、截断 60。

> 2026-09-10 台账化改版：记录键 = **运价台账契约的中文列名**（见 `Database/docs/agent-input-contract.md`）。
> 记录只产出"新行"（insert）——**没有 status_update / 覆盖回写**了；售罄/调价由你产一条新记录，旧行交台账折叠视图现算当前。
> 2026-10-23 引用图片边界：引用消息里的图片只作看板展示和 SHA-256 校验，不是本条报价证据；不得据引用图片生成或修正记录。只有当前消息自己的图片可进入视觉识别、抽取和独立盘点。

```json
{
  "msg_id": "msgXXXX==",                    // 必填，必须是 msgs_<D> 落盘文件里存在的 id（溯源铁律，pipeline.py 校验）
  "group": "qingdao_latam",                  // 必填，群 slug
  "decision": "records",                     // records | noise | template（模板复核用template，noise也留痕）
  "confidence": "high",                      // high | med
  "rule_basis": "rules.md#11",               // 判定依据锚点（可审计、可反推新模板）
  "records": [                               // decision=records 时必填，一条消息可产多条
    {
      "_table": "rate",                      // 带价格一律先产 rate；带价现舱再额外产同源 space。见 extraction-rules.md#27/#28
      "起运港": "青岛",                       // 键 = 台账契约中文字段名（可填别名/中文/港群名，归一交台账 Master）
      "目的港": "Santos",
      "航线": "南美西",                        // 填了必须命中台账 routes；拿不准就省略这个键
      "船司": "MSK",
      "柜型": "40NOR",                        // 只能 20GP/40HQ/HC/40NOR/45HQ/HC；无绝对价则省略该键
      "海运费USD": 6500,                      // 非负整数裸价；附加费一律进备注；只有加价公式则省略
      "币种": "USD",                          // 三位英文代码；未注明默认 USD，EUR/CNY 等保留原币金额
      "有效期船期": "9/8-9/14",               // 原文展示串；pipeline.py 会用 validity_parser 解析出 有效期起/止/船期ETD
      "备注": "free time cost USD50；VP USD29；超重费说明",
      "价格USD": 6500,                        // 仅 space 专用；双入时金额只写 rate 的海运费USD，本字段不填
      "舱位类型": "现舱",                      // space 可省略；填写时只能 11 档：现舱/加班船/放舱开放/售罄/舱位紧张/截关截单/撤载改期/箱子动态/空班/跳港/首航
      "船名航次": "MSC VIRGO FA633A",         // 选填：识别到就照抄原文；现舱消息常只报港口+舱位类型不给船名，留空即可（别编、别写占位符）
      "箱型箱量": "1小2高",
      "箱量": 2
    }
  ]
}
```

## rule_basis 怎么写才算有据可查

`rule_basis` 不是给人看的注释，是**可被 `check_llm_out.py` 反查的锚点**，两种合法形式：

- `规范.md#缩写表（作用域＝本群 + 该列）` —— 判定依据来自**该群规范**的某个小节，小节名取自该文件真实存在的 `##` 标题；
- `extraction-rules.md#19` —— 依据是全局口径的第 19 条（条款号 = 该文件顶层有序列表的编号）。

规则：**用了某群缩写表里的写法（`NB`=宁波、`+TAO`=青岛、段名"加勒比"…）就必须引用本群规范**，只写全局条款号会被自查标为可疑（"用到缩写但依据没引用本群规范"）。锚点解析不到 → 判为错误。这一步的目的不是为难你，而是让"到底有没有读该群规范"变成可验证的事——过去它只能靠自觉。

写完这一批判定，**先跑自查再 advance**：

```
python scripts/check_llm_out.py --date <MMDD>      # ✗ 错误必须清零；⚠ 可疑项交人工，别自己放行
```

如果 OCR 模板已经产出错误/不完整的同一消息记录，经逐行核对原图后，可在该消息判定对象加入 `"replace_template": true`。BUILD 将只使用此判定文件的 `records`，不叠加该消息的模板草稿；修复时必须覆盖原图所有可入库行，并在备注交代明确舍弃的行。默认不加此字段，保留模板和补充判定合并。

## 禁止事项（会被台账校验打回 / 进待审核 failed）

1. **不要填这些键**（pipeline.py 从落盘消息强制注入，保证逐字与真实时间）：来源群、发送人、**消息时间（真实群消息时间，agent 口径服务端保留它）**、消息原文、消息ID、记录状态、有效期起/止/船期ETD（日期列由 validity_parser 统一解析）。
2. 不要自己算派生价：图上明确的 FAK 裸价必须保留为 `海运费USD`，BASKET/T0、折扣、附加费和成本调整原样写入 `备注`；只有纯公式且没有图上绝对价格时才省略 `海运费USD`/`柜型`（规则 11）。
3. 引用回复的**被引用原文由 pipeline 的 `compose_text()` 自动并入 `消息原文`**（本条全文 + `——引用 时间 发送人 mID=<被引用ID>——` + 被引用逐字全文）。所以你**不要**把被引用全文再抄进 `备注`；`备注` 只补一句引用结论（如"售罄""基于原报价另减 X"）+引用时间即可。
4. 核心字段（起运港/目的港/船司）拿不准命中不了主数据时，宁可按原样发出让台账判 failed 交人工，也**不要编一个台账里不存在的值**；航线不确定就整个键省略。
5. noise 判定也要落一个 `{"decision":"noise","rule_basis":"extraction-rules.md#7",...}` 文件，否则下轮会重复进队列。
6. **船名里的公司别当船司**（共船/VSA 常态，见 extraction-rules.md#18）：识别到船名就照抄进 `船名航次`，但 `船司` 只取报价来源（群画像/正文明示/发送人语境），**不得**从 `MSC OSCAR` 这类船名里反推 `船司=MSC`；来源判不出就 `船司` 留空交台账判 failed。
7. **合并船司原样传串**（extraction-rules.md#19）：`EMC/COSCO`、`ONE/HPL/MSC/HMM` 照抄整串，**不要挑一家、也不要自己拆**——台账闸门会拆分归一并展开成 N 条。取第一家会让伴生船司静默丢失且不报错（2026-09-11 事故）。
8. **表头没有目的港列时，分段标题就是目的港**（extraction-rules.md#20）：`加勒比→CAR`、`南美东→ECSA`、`墨西哥→MEX`、`南美西→WCSA`，按港群名存一条；`南美西墨西哥` 这种合并段同一行存 WCSA、MEX 两条。
9. **多档柜型逐档配价**（extraction-rules.md#21）：`柜型="20GP/40HQ/HC/40NOR"` 必须配 `海运费USD="100/200/300"`；只给一个价会被逐条打回。`40GP` 不等于 `40HQ/HC`，别互相代用。
10. **服务码/占位说明不进港口字段**（extraction-rules.md#22）：`ESE`、`BP`、`Base Port`、`Port group`、`(未注明，只去X/Y)` 一律不填 `目的港`，留空并把整串写进 `备注`。
11. **占位符不是值**（extraction-rules.md#23）：拿不到就省略键或留空，不要写 `(未注明船名)`、`未注明`、`待定`、`无`——台账按未填处理。
12. **被放弃的行要在备注里交代**（extraction-rules.md#24）：一格里多船期（`9.6和9.13`）拆多条各带 ETD；判断不入库的整段（如"X号跳港NB"）必须在某条 `备注` 写"本图另有 N 行跳港未拆"，不许静默丢。

13. **证据回读闸门**：原文已经明确出现船司、港口、价格、柜型、船名航次或 ETD 时，必须把能够确认的字段写入记录；不得用一个所有核心字段为空的 `space` 记录代替解析。确实无法确认的字段可以省略，但至少保留已确认事实；机器自查会阻止空占位继续送审。

14. **明示价格/柜型对账**：自查从当前消息正文独立提取 `7500/40HQ` 等四至五位数字明示配对，与该消息所有 Agent 记录及未被 `replace_template` 替换的模板草稿合并核对。少一组就按消息隔离并保留水位；不能只填船司或价格绕过。三位数可能是附加费，本闸门暂不据此阻断。此闸门不解析所有港口、引用语义或图片单元格，也不证明整条消息完整；这些仍须按原文/原图逐项核对。

## 图片识别输出（另一种文件）

新图识完后写 `.work/ocr_cache/<mediaId去掉@>.json`（结构参考已有缓存：rows/价格/有效期/footer_notes/来源消息），
并把下载的**原图放进台账 `Database/images/`，文件名以 mediaId 开头**（看板 `images_for`/`_resolve_image` 按 mediaId 前缀归位取图）；
同一张图跨群只识一次。`pipeline.py` 会把源消息的 mediaId 写进记录 `图片名`，不需要 Agent 处理任何附件上传。

## 结构化表格图 → 零 LLM 生成器（record_builder 按 `type` 分发）

对**规整的表格/矩阵图**，Agent 识图后只要把表格机械地写进 ocr_cache 的规范结构，`record_builder.py`
会按 `cache["type"]` 自动展开成候选记录，**不用再逐条写 llm_out**（省掉最耗时的转写）。
识别不出结构、或字段有大量歧义时，仍回退到逐条 llm_out 判定。三种已支持的规范结构：

**① `type:"space_table"`（现舱表：ETD/POL/POD/VESSEL/VOLUME/RATE/FT/退关费/备注）**
```json
{"type":"space_table","carrier":"HPL","route":"地东","notes":"表级备注",
 "rows":[{"etd":"2026-9-28","pol":"NINGBO","pod":"ISTANBUL","vessel":"A. P. MOLLER",
          "vol":"1X40HC","rate":"USD3750+ENS","ft":"14天","退关费":"USD300","remark":"VIA PORT SAID"}]}
```
每行 → 一条 rate（`rate` 里的数字进海运费USD、`vol` 解析柜型）+ 一条 space（舱位类型=现舱、箱量、船名）。

**② `type:"rate_matrix"`（多 POL 行 × 多 POD 列矩阵，如 CUL）**
```json
{"type":"rate_matrix","carrier":"CUL","route":"地东","vessel":"GFS PRESTIGE V.2638W","notes":"9.20 update 含LSS/FAF",
 "rows":[{"pol":"Qingdao","etd":"9.30","cells":[
            {"pod":"ISTANBUL","20GP":3700,"40HQ/HC":4700,"raw":"3700/4700","tag":"ISTANBUL→Ambarli"},
            {"pod":"ALGIERS","20GP":null,"40HQ/HC":null,"raw":"-","tag":"CIF/VIA ALIAGA"}]}]}
```
每个 (行 POL × 列 POD × 柜型) 有价的格 → 一条 rate，柜型与价格逐档配对；所有价格皆空的格不造价，其他条目的备注保留无价/暂停行数量。
`tag` 原样进备注（CIF/FOB、via 中转等歧义交人工）。

非 USD 价格：图片有明确海运费但币种为 EUR/CNY 等时，`海运费USD` 继续保存原金额裸数字，同时 `币种` 写对应三位代码；禁止换汇或把原币种金额冒充 USD。原文未注明币种才默认 USD。界面直接显示“币种 + 原金额”，不再要求查看原图填写。

**③ `type:"price_card"`（POD×柜型价卡，常无 POL 列，如 MSK 地中海）**
```json
{"type":"price_card","carrier":"MSK","route":"地中海","pol":"","notes":"45HQ+USD800；POL加价…",
 "rows":[{"pod":"Ashdod, Israel","20GP":2830,"40HQ/HC":3850}]}
```
每行每个有报价的柜型 → 一条 rate；`pol` 缺省则**起运港留空交台账判 failed**（价卡没标 POL 时不猜）。

> 新增一类规整图：写个 `t_xxx_cache(msg, cache)` 并在 `record_builder.CACHE_TPL` 里挂 `"type": 函数` 一行即可，
> 分发与落盘不用动。柜型目标为台账四档 `20GP / 40HQ/HC / 40NOR / 45HQ/HC`。价卡/矩阵 OCR 可保留原图列名，不再要求手工拼多档 `a/b` 串。


## 价卡/矩阵的完整性要求（5.4.1）

- OCR 保存原图柜型列和逐行 `validity`（或 `valid_from` / `valid_to`），表级免柜天数写 `free_days`，页脚写 `footer_notes`。`20GP/20DC`、`40HQ/40HC/40HQ/HC`、`40NOR/40N/NOR` 由模板统一处理。
- `40GP` 和 `40HQ` 只有原图同价才能合并为 `40HQ/HC`；异价、仅40GP、同档别名列异价均保留低把握冲突，交预检和用户确认，不自动挑一价。没有NOR列不补NOR。
- 原图明确共价的 `40DV/HC` 列包含HC，归入 `40HQ/HC`；单独40DV不能凭此规则当高柜。
- 日期优先级：单元格/行 → 父行 → 图片全局 → 本条文本 → ETD。所有日期仍交 `validity_parser`；图片更新日期、附加费生效日期不得覆盖报价有效期。明确但无法解析的图示日期须低把握。
- 45HQ/45HC 独立写入 `45HQ/HC`，价格和限制不再冒充40HQ；无价/暂停服务行不造价，其数量写入其他报价备注并保留原图。
- 港口组合依本图明示成员展开，删除线成员排除；不能把一次报价的成员清单变成永久全球港群。港名归一仍只使用台账预校验。
- 引用回复复用原报价图片时，先送语义判定判断询价、调价或状态；不能因为图片缓存命中就生成本回复的新报价。新报价仍需真实发布者、逐字原文和消息时间。
- 回归入口：`python -m unittest discover -s scripts -p test_record_builder.py`；只做离线解析，不写台账或水位。

## HPL价卡定义（5.7.3）

原图行已按多个目的港展开时，另存`raw_source_rows=[{pod:"原图行名",prices:[原图各金额]}]`。原图行数及金额数量须与此清单一致，不能用展开后数量代替原图数量。每条历史回填消息另保存read、status、media_ids的识读回执；临时缓存不代表完整判定，不可据此抢先提交。公式报价明确柜型但未确定实际成本时，只允许保持海运费为空并进入failed；必须存在逐字匹配本条原文的cost_formula及对应备注，不豁免缺失柜型或其他字段。

正文明确要求表价调整时，使用 `type="formula_card"`，其余结构同price_card；`cost_formula`必须逐字匹配本消息正文。原图表价保留在各柜型单元格，仅作为备注中的参考FAK，不能自动作为海运费。保留全部附加费及限制，不计算未经核实的裸运价。`source_rows`及`source_priced_cells`从实际原图计数；相同报价的多个起运栏合并时，另存逐行`raw_price_grid`以核对合并前的金额数量。

2026-10-08例外澄清：本消息明示FAK表价且原图存在可逐格核对的绝对报价时，允许保留原始FAK海运费，备注必须标明“FAK基准、非折后合约成本”，完整保留减价公式及附加费，不代算最终成本。已核实缓存可标 `price_basis="FAK"`；纯公式、无绝对表价或价格适用范围未知时，仍不能补价。行级 `pol` 优先于表级 `pol`。行级 `price_scope_unresolved` 保存具体缺证原因；模板撤去该行未经证实的起运港及海运费，并保留原图组价和低把握提示，不能因表级FAK标记放行未定义分组。

OCR的pod_definitions为本图目的分类→明确成员字符串数组（优先保留LOCODE），definition_evidence引用原图定义区域；pol_definition逐字保留原图起运范围说明，不能将国家名单当港口。定义必须从本图读取，不借用其他图的分类成员。HPL候选通过已有航线服务字段区分via，完整保留footer_notes，不截断。

## 航次矩阵与原图维度清单（5.11.0）

多POL×POD×柜型且明示船名/航次与ETD的本群带价舱位图使用 `type="sailing_matrix"`。`carrier`保留原图明确船司，即使主数据尚未命中；`vessel`逐字保存，`service`保存明示服务码。每个 `rows` 项列出一个 `pol` 和对应ISO `etd`，`cells`逐目的港保存价格、ETA、via及原图POD写法。箱量没写就不生成。

必须同时保存 `source_inventory={pols:[{pol,etd},...],pod_rows:<每个POL的原图POD行数>,priced_cells:<所有POL有价单元格总数，含未支持柜型>,carrier:<原图船司>,vessel:<原图船名航次>}`，先逐图计数再展开。遗漏、重复行、清空已识别字段或用备注替代其他POL会隔离该来源，保留水位，不能靠LLM空占位或replace_template绕过。机器只核对Agent声明与结构是否一致，不能证明原图声明本身无误。

只有本群规范明定带价舱位分类时，旧 `rate_matrix` 的 vessel 会触发同样检查与舱位展开；纯价卡及其他群原分类保持。正常数据库仍是首次validate→修正一轮→直接pending，原图完整性检查不增加数据库预检次数。

模板已完整展开、Agent只确认原图而没有额外记录时，可写 `decision="template", records=[]` 并引用实际群规范。它表示接受该消息已有模板候选，不能新增空space占位；BUILD必须存在对应模板结果且图片维度核对通过，否则隔离并保留水位。`records`仍须非空，`noise`仍不能带记录。
