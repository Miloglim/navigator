# scripts/ —— 每个文件干什么、调什么后端接口

> 全部只依赖 Python 标准库。路径统一从 `paths.py` 取，别处不自己拼目录。

| 文件 | 人话职责 | 调用的后端接口 / 对象 |
|---|---|---|
| `paths.py` | 全 skill 路径的单一真源：算出代码在哪、config 在哪、运行态(包外)在哪 | 读 `config/ledger_contract.json`；提供 `WORK/OCR_CACHE/RUNTIME_DIR/WATERMARK_F/chat_groups()` 给其它脚本 |
| `pipeline.py` | 心跳主编排（状态机）：`PULL→PARSED→LLM_PENDING→BUILD→SUBMIT→DONE`。生成 dws 拉取命令、落盘消息、拉 record_builder、合并 LLM 判定、切块提交、成功才推水位 | 让 Agent 跑 `dws chat message list`（读群消息）；对台账 `POST /api/validate`(预检) + `POST /api/pending`(写入)；子进程调 `record_builder.py`、`watermark.py` |
| `record_builder.py` | 模板引擎：从落盘消息文本里抽字段，**直出中文字段候选记录**（`draft_rate.json`/`draft_space.json`）+ `needs_llm.json`。命中不了模板的丢给 LLM | 不调网络；读 `config/chat_sources.json`(群画像)、消费 `ocr_cache`（识图结果）；调 `validity_parser` 解析日期 |
| `ledger_client.py` | 台账对接的唯一一层：封装对 `manual_server` 的 HTTP。**写入只有 `submit()`→`/api/pending`**；`validate()`→`/api/validate`(只读)、`master()`→`/api/master`(读主数据) | `POST /api/pending`、`POST /api/validate`、`GET /api/master?full=1`（默认 `127.0.0.1:8795`，令牌 `x-push-token`）|
| `watermark.py` | 水位线：记每个群上次拉到哪一刻。`get` 出拉取起点，`set` 在提交成功后推进 | 无网络；读写 `RUNTIME_DIR/watermark.json`，读 `config/chat_sources.json` |
| `validity_parser.py` | 有效期唯一口径：把 `9.10-9.16`/`9/7开`/`8-Sep to 14-Sep` 等自由文本解析成 `valid_from/valid_to/etd` 的 ISO 日期。跨年/半拉子等有兜底 | 无网络、无外部依赖（纯函数） |
| `llm_output_contract.md` | 告诉 LLM/Agent 它产出的 `llm_out_*.json` 该长什么样（中文字段键、别自己算派生价、引用回复怎么表达） | 无代码；是 `pipeline.py` 合并 LLM 判定时的输入格式契约 |

## 和台账的边界（重要）

- 写入**只走 `/api/pending`**（进待审核），绝不碰 `/api/push`、`/api/record/*`、`/api/master/save`——那些是人工/维护口子。
- 港口/船司/航线的归一（别名→规范名、港群双向展开）**只在台账 `Master` 里算**；本包脚本发原值、发 `/api/validate` 看结果，不自建第二套映射。
- 柜型三档、舱位类型八档台账**不兜底**，由 `pipeline.py._enum_ok` 在提交前自守。
