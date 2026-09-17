# scripts/config/ —— 两份配置讲清楚

> 这两份是"数据/连接配置"，不是代码。改它们不用动脚本；改代码逻辑才动 `../` 里的 .py。

## `ledger_contract.json` —— 枚举 + 台账连接 + 运行态目录（契约单一事实源）

- `ctype`：柜型三档 `20GP / 40HQ/HC / 40NOR`。台账服务端不校验这个，靠 skill 自守。
- `space_type_options`：舱位类型**十一档**（现舱/加班船/放舱开放/售罄/舱位紧张/截关截单/撤载改期/箱子动态 + 2026-09-11 新增 空班/跳港/首航），与台账 `agent-input-contract.md`、`manualdb.SPACE_TYPE_ENUM` 三处对齐（旧钉钉表的第 9 档"报备"已删）。
- `db_conn`：连台账 `manual_server` 的参数——
  - `server_url`（默认 `http://127.0.0.1:8795`，可用环境变量 `FRT_DB_URL` 覆盖）
  - `token_file`（写令牌路径，可用 `FRT_DB_TOKEN` 直接给值覆盖）
  - `images_dir`（台账看板取图的目录，一般是 `Database/images`；运价原图放这儿才能显示）
  - `origin`：固定 `agent`（台账据此区分是 agent 录入还是人工）
- `runtime_dir`：**包外**运行态目录（水位/.work/ocr_cache 落这里）。可用 `FRT_RUNTIME_DIR` 覆盖。
- `batch_limit`：单次 `/api/pending` 上限（台账规定 1000，超了 `pipeline.py` 自动切块）。

## `chat_sources.json` —— 要爬的钉钉群清单

`groups` 数组，每个群：
- `name`：群名（会原样写进记录的 `来源群`）。
- `cid`：会话 ID，`dws chat message list --group <cid>` 用；换账号/换群用 `dws chat search --query <群名>` 重查。
- `slug`：脚本内短名（落盘文件名、模板命中判断都用它）。
- `default_pol`：该群消息没写起运港时的兜底（港名或港群名，需能在台账主数据命中）。
- `profile`：这个群常见承运/航线/发送人的画像，供 LLM 判定与 `record_builder` 模板参考。

> 注意：这里的群是**要爬的钉钉源群**，和台账库里的 `chat_groups`（航管账号可见分区用的群表）不是一回事，别混。
