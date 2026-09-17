# 运价台账系统 · 架构与技术方案（人话版）

> 范围：`运价项目` 里的**运价台账（Database）** + **运价心跳采集器（freight-rate-heartbeat skill）**。
> 最后更新：2026-09-11

## 一句话讲清它在干什么

从几个钉钉群里，把海运**运价 / 舱位**报价自动扒出来、整理成规整的结构化数据，走"**机器先校验、人再放行**"送进一个**本地 SQLite 台账**；销售的网页看板只看到自己负责航线/群的价格。核心目标是**数据 100% 标准化**、脏值进不了库。

## 三块能各自独立跑的东西

| 块 | 位置 | 作用 |
|---|---|---|
| 台账库 + HTTP 服务 | `Database/`（`manualdb.py` 数据层 + `manual_server.py` 接口层） | 存数据、归一、校验、查询、鉴权、放行的唯一事实源 |
| 看板 | `Database/web/index.html` | 销售端（查价/折叠/历史/CSV）+ 航管端（录入/价舱管理/候选/待审核）+ 配置端（批量导入/主数据/别名/影子重放） |
| 心跳采集器 | `freight-rate-heartbeat` skill（`scripts/`） | 读钉钉群 → 抽字段 → 只调 `POST /api/pending` 把数据递到待审核 |

三块之间**只有一个连接点**：HTTP 接口 + 一份传入契约 `docs/agent-input-contract.md`。skill 不碰库文件、不写 SQL，只发中文字段的 JSON。

## 数据从群到库的主链路（心跳）

```
取水位 → dws 逐群拉新消息 → 逐字落盘
      → record_builder 模板直出候选记录（命中即 0 LLM，省成本）
      → 没命中 / 图片 / 引用回复 → LLM 补判
      → compose_text 把"被引用原文"并进 消息原文（带 mID）
      → POST /api/validate 预检（只读，报哪几条会被打回）
      → POST /api/pending 暂存待审核
      → 全部成功才推水位（没成功下轮重拉，自愈）
```

**"进待审核" ≠ "入库"**。人工在看板「待审核」页点放行，才落到 `freight_rates / space_records`。skill 全程只读群、只写 pending，**永不自动进业务表**。

## 台账里存什么（数据模型 + 标准化红线）

- **主数据（合法值集）**：`ports`（中英/别名/拼音/LOCODE/国家）、`carriers`（code 为主键）、`routes`、`port_groups`（港群：群名+成员）、`senders`。
- **业务表**：`freight_rates`（运价）、`space_records`（舱位）。
- **闸门表**：`pending_records`（agent/导入先落这里，带 `check_status`/`errors`/`action`）。
- **权限表**：`operators`（航管负责航线/群 + 匹配模式）、`chat_groups`（群可见性）。

**值的硬规范（进库前必须满足）**：起运港/目的港/船司/航线要么命中主数据要么留空；价格/天数一律非负整数；有效期是 ISO 日期；柜型只三档、舱位类型只八档；`消息原文` 逐字（引用回复自带引用块）；`origin=agent` 时 `消息时间` 保留传入的真值。命中不了的 → 打回 `failed` 进待审核交人工，**不污染业务表**。

## 为什么这么设计（技术方案的取舍）

1. **归一只有一个真源 = 台账的 `Master`**。前端联想、批量导入、待审核校验、agent 预检全复用同一套 `fold()` / `lookup_port` / `expand` / 双向展开；skill 侧**明令禁止再拼第二套映射**。一个折叠别名指向多个规范名就**整条丢弃**（宁可不归也不归错港）——所以跨国同名靠 `Manzanillo(MX)` 这种带国家的规范名消歧。
2. **写入两级闸：机械校验 + 人工放行**。非人工入口（agent/导入）先过 `validate_record` 硬闸，不过的带错误进待审核；过的也要人点放行。把"AI/外部数据会脏库"的风险挡在业务表外。
3. **幂等 + 自动判新增/更新**：业务表按内容指纹 `content_key` 唯一；同一 `起运港+目的港+船司+原文` 再传，走 `supersedes` **留痕更新**（旧行标"已被覆盖"、新行叠加），重复报价不堆行，"当前价"由看板折叠视图现算。
4. **分区可见性服务端强制**：查询带 `viewer` 就由服务端叠 `scope` 过滤（销售只看自己航线/群），四处查询入口统一走同一函数，漏带一处就等于越权。
5. **港群"存群名一条 + 查询双向展开"**：一条报价覆盖多港，存群名（如 `ECSA`）不拆成员；筛成员港带出群价、筛群名带出成员价，非成员不串。
6. **只依赖 Python 标准库 + 本机 HTTP**：`manual_server` 用 `http.server`，SQLite 单文件，断网可用、无平台水印、换机器只要 Python；skill 也是标准库 `urllib` 直连台账。
7. **防漂移的工程习惯**：运行态（水位 / `.work` / 识图缓存）全放包外 `heartbeat-data/`，`paths.py` 一处算路径；脚本只存一份（`scripts/`）；细则沉到 `references/`，每个子目录配 `Instruction.md` 用人话讲"每个文件干嘛、调哪个接口"；`learnings.md` 记用户改进/纠错/已废弃做法，开工先读。
8. **再加一个"数据审查 agent"（经许可的第二定时任务）**：先摸清库结构 → 只读 `/api/audit` + `/api/pending(failed)` + `/api/master` → 按踩坑清单**归因**（数据脏 / 主数据缺 / 传入口径错）→ 给**可执行改法**（点名到字段/端点/人工步骤），但**绝不自动写库**。

## 部署 / 运维

- 起台账：`Database/start-manual.bat`（默认 `127.0.0.1:8795`，本机回环免口令、局域网设备只读、写令牌 `token_manual.txt`）。改 `manual_server.py` 后要重启服务。
- 心跳：两个定时任务——低频 `0 9-13 * * *`（每小时）+ 高频 `*/10 14-18 * * *`（每 10 分钟），Asia/Shanghai；建议再经许可建"数据审查 agent"那条。
- 换环境：改 `scripts/config/chat_sources.json` 的群 cid、`scripts/config/ledger_contract.json` 的台账连接 / 图目录 / 运行态目录；首轮用空 `watermark.json`；`python scripts/ledger_client.py` 冒烟连通。

## 关键接口一览

| 接口 | 方向 | 用途 |
|---|---|---|
| `GET /api/rates` `/api/space` | 读 | 扁平查询（港群双向展开） |
| `GET /api/rates_grouped` `/api/space_grouped` | 读 | 折叠视图 + 历史轨迹 |
| `GET /api/master?full=1` | 读 | 主数据合法值（归一以它为准） |
| `POST /api/validate` | 只读预检 | 每条 `{ok, resolved, errors}`，不写库 |
| `POST /api/pending` | **唯一写入口** | agent/导入进待审核（`kind`\|`table` 都可） |
| `GET/POST /api/pending…` `approve`/`reject`/`delete` | 人工 | 待审核页放行/驳回/删 |
| `POST /api/push` `/api/record/*` `/api/master/save` | 人工/维护 | 航管端录入、留痕、主数据维护（agent 不碰） |
| `GET /api/audit` | 读 | 全库标准化体检（审查 agent 用） |
| `GET /images/<名>` | 读 | 看板取运价原图 |

## 演进（现状为何这样）

早期版本的投递目标已整体退役——**唯一写入对象是运价台账 `/api/pending`**，归一/校验交给台账 `Master`。采集 / 模板 / 水位 / 识图缓存这些"能力"原样保留，只是换了投递对象，并陆续补上"消息原文附带引用（带 mID）""船名 ≠ 船司（共船/VSA）""有效期兜底窗 + AI判断""数据审查 agent"等口径。
