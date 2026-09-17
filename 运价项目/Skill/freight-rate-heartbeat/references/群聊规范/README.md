# 群聊业务规范（按群分目录）

> 用途：**降低采集失误率**。Agent 每轮处理某个群的消息前，先读该群目录下的 `规范.md`，
> 再动手判定；`references/extraction-rules.md` 是全局口径，本目录是**逐群的差异与坑**。
> 机器配置真源仍是 `scripts/config/chat_sources.json`（cid / slug / default_pol / 画像），
> 本目录只写业务规范，不重复配置字段；两边冲突时以本目录为准并回写配置。

## 检索规则（Agent 照做）

1. 取本轮消息的 `来源群`（或 slug），映射到下表目录 ⇒ 读该目录 `规范.md`。
2. 缩写表按**「列 + 群」作用域**使用：同一个缩写在起运港列和目的港列可能不是一个意思
   （实例：`NB` 在起运港列 = 宁波，实测 98 次无一例外，跟"北巴"无关）。
3. 规范里标「待补」的条目 = 尚无实测依据，**禁止猜测填充**，按全局口径留空交人工。
4. 群里出现规范未覆盖的新写法/新坑 ⇒ 当场在该目录 `规范.md` 追加一条带日期的记录，
   并同步 `references/learnings.md`。

## 目录 ↔ 群 ↔ slug

| 目录（=文件名） | 钉钉群名 | slug | 默认起运港 |
|---|---|---|---|
| `CMA+EMC+PIL交流群` | CMA/EMC/PIL交流群 | `cma_emc_pil` | 华南基本港 |
| `天津拉美询价群` | 天津拉美询价群 | `tianjin_latam` | 天津 |
| `COSCO+HMM+MSK+OOCL交流群` | COSCO/HMM/MSK/OOCL交流群 | `cosco_hmm_msk_oocl` | 华南基本港 |
| `华南-拉美` | 华南-拉美 | `huanan_latam` | 华南基本港 |
| `ONE+ZIM+WHL+HPL+CSP交流群` | ONE/ZIM/WHL/HPL/CSP 交流群 | `one_zim_hpl_csp` | 华南基本港 |
| `青岛拉美运价（海外）` | 青岛拉美运价（海外） | `qingdao_latam` | 青岛 |
| `宁波舱位滚动更新群` | 宁波舱位滚动更新群 | `ningbo_space` | 宁波 |
| `美加线销售天团` | 美加线销售天团 | `meijia_sales` | 华南基本港 |
| `大连-拉美+美加` | 大连-拉美/美加 | `dalian_latam` | 大连 |
| `土耳其价格更新` | 土耳其价格更新 | `turkey_price` | 华南基本港 |
| `欧洲价格更新` | 欧洲价格更新 | `europe_price` | 全国基本港 |

## 每个目录的文件

| 文件 | 内容 | 谁维护 |
|---|---|---|
| `规范.md` | 定位 / 报价格式 / 缩写表（带作用域）/ 已知坑 / 解析要求 | Agent 追加 + 人工修订 |
| `样例/`（可选，暂不建） | 该群典型消息逐字样例与正确解析对照 | 出现新模板时补 |

> 群专属别名已落到台账 `alias_scope` 表（`(scope_group, kind, alias)` 联合键，2026-09-16 seed，现 36 行）；
> 本目录是它的**人工可读来源**，运行时匹配以台账为准。
>
> 2026-09-17 起"两边冲突以本目录为准并回写配置"这句话有机器兜底了，不再只靠 Agent 自觉：
> ① `chat_sources.json` 每群带 `spec_dir` → `scripts/spec_index.py` 把**该群规范路径与「待补」条目注入 llm_queue 队列项**；
> ② 判定文件的 `rule_basis` 必须是真实锚点，`scripts/check_llm_out.py` 反查（`pipeline.py` 在 BUILD 前自动跑并报告）；
> ③ 本目录缩写表 ↔ `alias_scope` 的差集与断链由 `scripts/spec_alias_diff.py` 体检，已挂进 `Database/manual_check.py` 回归。
