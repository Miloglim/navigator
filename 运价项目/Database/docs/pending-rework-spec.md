# 驳回回炉（pending rework）契约

2026-10-10 修订。一句话：**驳回可分为垃圾噪声或带原因回炉；放行权始终在人，删除是独立的物理删除动作。**

## 状态流转

```
awaiting ──人工放行──> approved
   │  ──人工编辑(update)──> awaiting（校验态实时翻转）
   └─人工驳回(reject)──> rejected（移入「驳回日志」，行不删）
              ├─note 为空：noise，记录学习信号，不回炉
              └─note 非空：rework，agent 按原因修订后重新递回
                         └─agent 返修(rework)──> 过闸门：回 awaiting（note 前缀「返修第N轮」，卡片显示徽标）
                                     不过闸门：退回 rejected，并把"试过什么/为何仍不过"写进 note 与 edit_log
```

`review_status` 只有 `awaiting / approved / rejected` 三个值，**没有新增状态**——返修成功就是回 `awaiting`，
靠 `note` 前缀与 `edit_log` 的 `rework` 动作识别。表结构零改动。

## 接口

| 口 | 用途 |
|---|---|
| `GET /api/pending?status=rejected` | 捞返修池（现成接口，不新增） |
| `POST /api/pending/rework` `{id, fields, reason, actor}` | 提交修订。护栏全在 `manualdb.rework_pending` |
| `POST /api/pending/restore` | 人工撤回误驳（顺带清掉返修标记） |
| `POST /api/pending/delete` | `{id}` 删除单条，`{ids,kind}` 原子删除合并组；不可恢复 |

采集侧用 `python scripts/rework.py list` / `... submit`，不手写 curl。`list` 只列有驳回原因的回炉项；空原因的垃圾条目不会进入返修池。

## 三条护栏（不可绕）

1. **垃圾条目不回炉**：驳回原因为空的行只记录 `pending_rejection:noise` 学习信号，Agent 调用回炉接口必须被拒绝。
2. **字段白名单**：只能改「该行校验报错的字段」∪「驳回原因里用「」点名的字段」，且限核心字段集合。
   越权字段整条拒绝并返回 `rejected_fields`。另一会话的「组完整性」连坐标记（`GROUP_MARK`）不是真实字段错误，
   既不算可改字段、也不因它把白名单判空。
3. **轮次上限 2**：`edit_log` 里 `rework`/`rework_failed` 计数到 2 就拒绝再返修，要求人工到驳回日志处理——
   防止"改了不过 → 下轮再改"无限刷配额。**失败的尝试也计轮**（否则可以无限试）。
4. **不许编主数据**：提交值仍过台账强匹配闸门（`_strict_rejects`），查无此值就回退；
   若全部被回退（＝等于没改），**整条按失败处理、退回 rejected**，不允许带着 `passed` 伪装成"已处理"。

放行/驳回永远是人的动作：返修路径里 agent 不得调用 `/api/pending/approve`、`/reject`、`/delete`、`/api/record/*`。

## 为什么这条路安全（一处专门核过）

`stage_pending` 的"撤旧留新"只删 `review_status='awaiting'` 的同身份旧行——
**被驳回/返修中的行不会被新消息顶掉**，所以返修池不会因为采集继续跑而蒸发。

## 心跳怎么接

两条心跳任务（低频/高频）提示词的第 8 步：提交完成后跑 `rework.py list`。
空池输出 `REWORK_DONE` 即结束（不产生后续开销）；有项就只改列出的可改字段、用 `submit` 交回；
被服务端以"已满 2 轮"拒绝的条目在汇报里点名交人工，不重试。

## 已知未决（别在这里踩坑）

- **港口改名后历史行筛不到**（`Tianjin New Port` → `Xingang`）：38 条 `freight_rates.pol` 仍是旧字面值。
  本轮已在**读路径**解决可见性：`Master.name_variants`（实体 → 全部原始写法）+ `port_filter` 先展开写法再做群↔成员展开，
  实测 `天津 / Tianjin New Port / Xingang` 三种写法命中同 38 行。**取值必须用原文**——折叠键是小写，SQL `IN` 大小写敏感。
  相关用例已改为容忍两种写法并跑绿（不再是长红标记）。**用户决定：本轮不改数据，留待下次。** 下次直接按这套做，不必重新调研：
  ① 先 `GET /api/export` 出整库快照存 `归档/`（git 管不了 db，这是唯一数据回退点）；② 停服务；③ 一个事务内改 `pol`
  并**用 manualdb 的行构造函数重算 `content_key`**（它由身份字段 sha256 而来，只改值不重算 → 这 38 行永远匹配不上，
  下次同一条报价会变成新增而不是更新）；④ 按 `旧键→新键` 映射修两张表所有 `supersedes` 指针（该列存被覆盖行的
  content_key，不修就断链，历史轨迹与"当前生效"全错）；⑤ 起服务验证：三种写法命中一致、链可走通、折叠组数不变、
  `manual_check` 全绿。`message_text` 含该串的是 0 行，逐字原文不受影响。
- **历史「组完整性」连坐标记仍在污染队列（350 行）**：旧版除往每条 `errors` 写
  `同组（消息 …）共 N 条，其中 M 条未过` 之外，还把整组 `check_status` 刷成 `failed`。前端已改成子行不重复该文案、
  组头统一说明一次；但组头数字来自服务端派生的 `check_status`，**存量不清理就仍显示"54 条全未过"**（真实 12 条）。
  `manualdb.group_mark_cleanup(dry_run=True)` 实测：扫 687 行、剥标记 350 行、状态复原 275 行 —— 等确认后执行。
- **`group_mark_cleanup` 的调用陷阱**：它用 `r["errors"]` 按下标取字段，连接**必须带 `row_factory=sqlite3.Row`**；
  否则抛 `TypeError` 又被循环里的 `except Exception: continue` 吞掉，函数安静返回 `marks_stripped=0,
  status_restored=0`（实测不带 Row 时扫 687 行报"0 需清理"）。接进服务前先加 `conn.row_factory = sqlite3.Row`，
  或把那个 `except` 收窄到只兜 JSON 解析失败。
- 返修原因目前只有自由文本 + 校验 errors；驳回弹窗建议加错误类型下拉（主数据缺录/识别错字/字段漏填/价格柜型错），
  结构化后白名单更准。
