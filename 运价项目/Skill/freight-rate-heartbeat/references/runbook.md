# 标准执行流程细则与坑位（从 SKILL.md 移出）

> 2026-09-10 台账化改版：写入对象是「运价台账」`POST /api/pending`（本机 HTTP，python urllib 直连），不再写钉钉 AI 表格、不再有 fileToken/附件上传/record create/覆盖回写。以下"写入/校验"段已按台账重写，"下载图片/识图/水位拉取"段仍适用。

## 下载图片

```bash
dws chat +messages-resource-download --resource-id "<mediaId>" --message-id "<openMessageId>" --open-conversation-id "<cid>" --output "./images/" --format json
```

**坑 1**：`--output` 是相对 DWS 宿主侧工作目录的路径，实际落在
`C:\Users\<user>\AppData\Local\Programs\QwenWorkCN\<运行中版本>\images\`（版本号会变，先 `find ... -name "*.png" -mmin -5` 找），不在项目目录。必须再 `cp` 到 `.work/` 才能用 Read 工具看图，识图完再 `cp` 一份进台账图目录（见坑 4）。

**坑 2**：返回的文件名就是 `<mediaId>_<宽>_<高>.png`，天然可与消息里的 mediaId 对账。别用时间猜是哪张图。

**坑 3**：**本地缓存的图可能是已被撤回消息的旧版本**。每次重灌都要按**当前消息列表里的 mediaId** 重新下载；`.work/ocr_cache/<mediaId>.json` 以 mediaId 为键，mediaId 相同才复用识别结果。

**坑 4**：新看板从 `ledger_contract.json.db_conn.images_dir`（即 `Database/images/`）取真实文件显示，看板 `_resolve_image` **按 mediaId 前缀**匹配文件名。所以原图要按 `<mediaId>...` 命名放进该目录；记录 `图片名` 由 pipeline.py 写 mediaId 列表，缺文件就渲染"图片未同步"。

## 识图

用 Read 工具直接看 PNG，逐格抄出 POD / 各柜型价格 / 有效期 / 表头与页脚附加费说明。**红色字体、"停接"、"SUSPEND"、"N/A"、"/"、"case by case" 都要如实转成备注**，不要猜价格。

**识图后必须写 `.work/ocr_cache/<mediaId>.json`**（结构化：rows/价格/有效期/footer_notes/来源消息），
同一 mediaId 被转发到多个群时，record_builder.py 直接消费缓存，**不再二次识图**（跨群复用，省一半图像 token）。

## 写入（提交台账待审核）

`pipeline.py advance` 到 SUBMIT 阶段自动做，**同步 HTTP、无需 Agent 逐条跑**：

- 对每块先 `POST /api/validate`（不写库，只为把会被打回的条目在 DONE 摘要里报个 `check_failed` 数）。
- 再 `POST /api/pending`（唯一写入口），体 `{"table":"rates|space","origin":"agent","records":[...]}`，一次 ≤1000 条由 `ledger_client.chunks` 自动切。
- 返回 `{ok, staged, ids}` = **已暂存待审核，不代表入库**。人工在台账「待审核」页点放行才进业务表。
- **全部批次 ok 才推水位**；服务没起 / token 失效 → 不推水位、下轮重投（台账 `content_key` 幂等 + 待审核判重兜底）。

`manual_server` 默认 `127.0.0.1:8795`；先 `Database/start-manual.bat` 起服务、`python scripts/ledger_client.py` 冒烟连通。

## 校验

台账侧机械校验（起运港/目的港/船司/航线命中主数据 + 必填 + 整数价 + ISO 有效期），skill 只补台账不做的**柜型三档 / 舱位类型八档**枚举自守（`pipeline.py._enum_ok`，非法直接丢弃不进待审核）。

- 查暂存：`GET /api/pending?status=awaiting|all&kind=` （带 `x-push-token`）。
- 查业务表/历史：`GET /api/rates_grouped`、`/api/space_grouped`（免写令牌）。
- 查重已交台账：同 `起运港+目的港+船司+原文` 全同 = 自动判"更新（留痕）"，不堆重复行。

## 常见故障

| 现象 | 原因与处理 |
|---|---|
| 提交 `unreachable` / 连不上 8795 | 台账服务没起：`Database/start-manual.bat`；水位不推，下轮自动续 |
| `401 unauthorized` | `token_manual.txt` 不对或 `FRT_DB_TOKEN`/`db_conn.token_file` 指错；核对后重跑 advance |
| `500 table must be "rates" or "space"` | 请求体 `kind`/`table` 都没给对（本版服务端两者都收，别再传第三种值） |
| 一批全部 `check_status=failed` | 多半是**主数据缺登记**（如 `华南基本港`、某些航线未进 `routes`）。去台账「主数据库」补，或在待审核页人工放行；不是 skill 逻辑 bug |
| DWS 命令被 blocked | DWS 不能走管道/重定向/`&&`拼接/`cd`前缀，一条命令一次调用；过滤用 `--jq`/`--format` |
| 钉钉掉登录 | `dws profile list` 报 not logged in → 去 设置→连接器 重登；水位未推进，登录后下轮自动补拉 |
| 图片 Read 报文件不存在 | 忘了从 DWS 工作目录 `cp` 到 `.work/` |
| 记录数对不上 | 检查是否漏了 `--direction newer`（默认 older 会往历史翻） |
| 同一消息疑似被重复入库 | 先别删：`chat message list --time <since>` **含边界**（返回 t==since 的消息本身），落盘前应丢弃 `t == 该群 since` 的消息再追加。**查重键必须含起运港**——一张 FAK 表图常含 5+ 个起运港行，漏掉起运港维度会把合法多行误判成重复（2026-09-03 实测：全字段签名比对 0 条完全相同）。台账端 `content_key` 幂等兜底 |

## 已验证的约束（事件推送路线，别再试错）

- **通道配对走不通**：`qwenwork.channels.dingtalk` 虽 connected，但配对要求把机器人拉进群，需群主/管理员配合。用户要求全程单人参与时直接放弃此路。
- **Agent 起不了监听**：`dws event +listen-im` / `event consume` 要 fork 本地 bus 守护进程，Agent 侧宿主执行会报 `fork/exec ... not supported by windows`；DWS 也不允许后台运行或重定向输出。
- **用户自己终端能跑**：`dws.cmd` 是走宿主的 shim，用户在 cmd/PowerShell 里执行 dws 正常，bus 起得来。
- **`--foreground` 不适用**：只跑 bus 不吐事件，是给 systemd/k8s 托管用的。
- 2026-09-01 实测 `events/inbox` 恒空、`processed` 恒 `[]`，推送链路一次都没成功投递过。主流程一律走**水位线拉取**（`watermark.py` + `pipeline.py`）。
