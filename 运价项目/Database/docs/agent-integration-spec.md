# Agent 接入查询规范（人工维护台账 · freight_manual.db）

> 状态：预置规范，暂不实现。以后给 agent 开 token/接口查询时**必须**按本规范落地。

## 0. 一句话原则

台账能把港查准，靠的是 `manualdb.py` 里 `Master` 那层的归一逻辑，不是数据库表本身。
**agent 只能走接口，禁止直连库写 SQL。** 直连 SQL 必栽：漏群价、认不出中文/拼音/别名、
遇到跨国重名瞎猜。

## 1. 为什么结构够用（也只需要够用）

- `ports` 已带全查找列：`name / name_cn / name_en / city / city_pinyin / country / aliases / locode_ref / kind`。
- `carriers` 以稳定 `code` 为主键，业务表只存 code，改名安全。
- `port_groups` 存群名 + 成员，港价按群名存一条不拆成员。
- 业务表 `pol/pod` 是**重载**字段：可能存规范港名，也可能存港群名。

结构上唯一为 agent 可选做的（当前不做）：把 `aliases`/`members` 从分隔符文本炸成行，
加一张 `port_alias(alias_folded → 规范名)` 视图表，纯只读原始查询才需要。走接口不需要。

## 2. 归一真源（唯一，不可复制第二套）

所有映射只在 `Master` 里算，agent 侧不得另拼一套：

- `fold()` 折叠 + 别名索引：中文/英文/拼音/国家/LOCODE/别名 → 规范名。
- `build_index` **歧义即丢弃**：一个折叠别名指向多个规范名时整条扔掉，宁可不归也不归错港。
  → 跨国重名（如两个 Manzanillo）不靠 DB 消歧，靠规范名带国家后缀（`Manzanillo(MX)`）。
- `expand_cn`：纯国家名丢弃、多港拆分、群价按群名存一条。
- `port_filter`：**查询时双向展开**——筛成员港带出群价，筛群名带出成员精确价，非成员不串。

## 3. agent 必须走这两个工具（实现时落地）

1. `resolve_port(文本)` → `{canonical, candidates[], confidence, matched}`
   - 命中唯一：给规范名 + 置信。
   - **不唯一 / 命中不了：返回候选让人确认，绝不自动选**（对齐既有铁律：读工具返候选+置信、不唯一勿静默选）。
2. `query_rates(pol?, pod?, carrier?, ...)`
   - 入参先过 `resolve_port`，再经 `Master.port_filter` 双向展开，否则群价永远漏。
   - 返回行 + `unmatched` 提示（哪些词没归上，原样回给 agent/用户）。

## 4. token / 权限口径

- 读接口（查询）与写接口（录入/留痕）分开鉴权；agent 默认只给读令牌。
- 写操作（改价改期走留痕 `revise_record`）必须显式确认，agent 不得静默写盘。

## 5. 库结构标准（本仓库现状，新库按此建）

- 价格/天数列 `freight_usd / price_usd / free_days / dead_freight / transit_days` = **INTEGER + 非负 CHECK**（既能数值排序又挡脏值）。
- `valid_from / valid_to` = **ISO 日期 CHECK**。
- `origin` = **NOT NULL DEFAULT 'manual'**（本库是人工台账；`heartbeat` 是云端旧版遗留值，不要带回）。
- 内容指纹 `content_key` UNIQUE 幂等；留痕 `supersedes` 指回旧行、旧行标 `status='已被覆盖'`。
- 每张业务表 `supersedes` 只留**一个**索引（历史上曾有重复，已清）。

## 6. 统一条目 ID（看板 → agent 的定位协议，2026-09-28）

待审核、正式台账、销售端和航管端统一使用持久化字段 `entry_id`。新条目格式为 `frt_` 加 32 位小写十六进制字符；同一条记录从待审核放行到正式台账，`entry_id` 不变。用户把这个完整编号粘给 agent，即可直接定位，不需要先猜 pending 数字 ID 或 `content_key` 前缀。

| 位置 | 查询方式 | 说明 |
|---|---|---|
| 待审核 | `GET /api/pending?entry_id=frt_...` 或 `SELECT * FROM pending_records WHERE entry_id='frt_...'` | 可继续结合 `status/kind/check` 筛选 |
| 正式台账 | `GET /api/rates?entry_id=frt_...` 或 `GET /api/space?entry_id=frt_...` | 返回唯一正式条目 |
| 同源消息组 | `组:<消息ID>` | 仍用于查看一条来源消息拆出的整组，不替代个体 `entry_id` |

旧的 `pending_records.id`、`freight_items.id`、`content_key` 和 `消息ID` 保留用于内部兼容、幂等、溯源或整组查询；不再作为销售端/航管端对外主定位码。

诊断"这条有什么问题"的顺序（别跳步）：

1. **机械闸门**：`errors`（JSON 数组，`field/input/msg` 逐条）——"目的港「X」不在主数据""柜型只三档"这类就是闸门原话；
2. **人工改动史**：`edit_log`（`{ts,actor,action,field,old,new,可带 sync_from}`）——`sync_from` 表示该值是从锚点条目批量同步来的；
3. **归一对照**：`payload`（人工/agent 提交的中文字段版）vs `orig_payload`（agent 原始判定快照）vs `resolved`（主数据归一结果）；
   判"这条是不是被归一改写过"用 `orig_payload ↔ payload` 对比，别拿消息原文反推；
4. **台账行**：`status`（当前生效/当前有效/已被覆盖）、`supersedes` 指回的旧行、以及 `POST /api/audit` 的全库体检结果。

码只用于定位，不用于改写：任何修改仍走既有接口（`/api/pending/update`、`/api/pending/sync`、`/api/record/revise`），强匹配字段与批量同步的护栏照常生效。
