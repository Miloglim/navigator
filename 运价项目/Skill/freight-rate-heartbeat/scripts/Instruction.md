# scripts/ —— 每个文件干什么、调什么后端接口

## 审查隔离与完整性（5.9.0）

每次BUILD强制来源、结构和规则依据自查，严重错误按消息生成 evidence-only 待审核行；普通提醒继续机械预检。修正一轮后不重复业务预检，但不能绕过来源/结构检查。解析不完整不拖住其他消息：原文、消息ID、来源和原因先进入待审核，所有本轮消息覆盖后水位推进并以 DONE 收尾。提交回执保留服务端coverage清单，条目缺失与正常精确重复分开；顶层ok不再被当成完整性证明。test_semantic_gate和test_precheck_repair覆盖证据保留、修复、无重复提交。

> 全部只依赖 Python 标准库。路径统一从 `paths.py` 取，别处不自己拼目录。

| 文件 | 人话职责 | 调用的后端接口 / 对象 |
|---|---|---|
| `paths.py` | 全 skill 路径的单一真源：算出代码在哪、config 在哪、运行态(包外)在哪 | 读 `config/ledger_contract.json`；提供 `WORK/OCR_CACHE/RUNTIME_DIR/WATERMARK_F/chat_groups()` 给其它脚本 |
| `pipeline.py` | 心跳主编排（状态机）：`PULL→PARSED→LLM_PENDING→BUILD→SUBMIT→DONE`。生成 dws 拉取命令、落盘消息、拉 record_builder、合并 LLM 判定、切块提交、成功才推水位 | 让 Agent 跑 `dws chat message list`（读群消息）；对台账 `POST /api/validate`(预检) + `POST /api/pending`(写入)；子进程调 `record_builder.py`、`watermark.py` |
| `knowledge_gate.py` | 知识读取闸门：按本轮 `new_ids` 路由全局规则和对应群规范，校验存在/可读/非空并记录 SHA-256 回执；文件异常时阻断后续解析 | 无网络；读取 skill、references 和群规范，写当前 `run_<D>/knowledge_read_receipt.json` |
| `record_builder.py` | 模板引擎：从落盘消息文本里抽字段，**直出中文字段候选记录**（`draft_rate.json`/`draft_space.json`）+ `needs_llm.json`。稳定模板核对港口/条目/产品唯一性指纹，失败整条丢给 Agent | 不调网络；读 `config/chat_sources.json`(群画像)、消费 `ocr_cache`（识图结果）；调 `validity_parser` 解析日期 |
| `ledger_client.py` | 台账对接的唯一一层：封装对 `manual_server` 的 HTTP。**写入只有 `submit()`→`/api/pending`**；`validate()`→`/api/validate`(只读)、`master()`→`/api/master`(读主数据) | `POST /api/pending`、`POST /api/validate`、`GET /api/master?full=1`（默认 `127.0.0.1:8795`，令牌 `x-push-token`）|
| `watermark.py` | 水位线：记每个群上次拉到哪一刻。`get` 出拉取起点，`set` 在提交成功后推进 | 无网络；读写 `RUNTIME_DIR/watermark.json`，读 `config/chat_sources.json` |
| `validity_parser.py` | 有效期唯一口径：把 `9.10-9.16`/`9/7开`/`8-Sep to 14-Sep` 等自由文本解析成 `valid_from/valid_to/etd` 的 ISO 日期。跨年/半拉子等有兜底 | 无网络、无外部依赖（纯函数） |
| `test_record_builder.py` | 价卡/矩阵业务回归：柜型同价/异价、有效期优先级、零价、暂停行、45HQ限制、引用图片语义分流 | 只调用本地模板，不联网、不写库 |
| `llm_output_contract.md` | 告诉 LLM/Agent 它产出的 `llm_out_*.json` 该长什么样（中文字段键、别自己算派生价、引用回复怎么表达） | 无代码；是 `pipeline.py` 合并 LLM 判定时的输入格式契约 |

## 和台账的边界（重要）

- 写入**只走 `/api/pending`**（进待审核），绝不碰 `/api/push`、`/api/record/*`、`/api/master/save`——那些是人工/维护口子。
- 港口/船司/航线的归一（别名→规范名、港群双向展开）**只在台账 `Master` 里算**；本包脚本发原值、发 `/api/validate` 看结果，不自建第二套映射。
- 柜型三档、舱位类型八档台账**不兜底**，由 `pipeline.py._enum_ok` 在提交前自守。

## 预检失败的执行顺序（5.7.2）

`AGENT_REPAIR`只允许一轮业务修正。读precheck_review.json、repair_master.json、本群/共享规范及原图；查库匹配、必要时官方研究，有依据的增量主数据走ledger_client.ensure_master。改源OCR/llm_out后advance重建，下一次advance直接pending，不重复业务预检；每次重建仍执行来源、结构和依据自查。未知字段保留，不编造。pending内部最终校验未过的原条目保留failed/awaiting和errors交人工，不重开Agent修复或自动调用repair_pending；提交成功才推进水位。

test_precheck_repair.py覆盖首次失败分流、一轮修正直接送审、修后仍无效交人工、无复检/重复提交、传输重试、旧后检状态迁移。Database/test_postgres_runtime.py的单轮修正HTTP用例确认真实PostgreSQL中未过条目带错误留在待审核，正式表无写入。

## 待审核主动业务复核（5.12.0）

pending_quality.py提供next/refresh/finish/status；pending_quality/core声明常量/报告契约，services只算来源分组、内容指纹、排序和结案校验，transport读待审/证据并存本地领取状态。复核Agent依据references/pending-quality-review.md调用受限修复端点，队列本身不写业务数据。运行态位于paths.WORK/pending-quality/reviews.sqlite3，与业务PostgreSQL分离；500条分页必须读全total，30分钟领取、最多2次中断、同版本不重复。
