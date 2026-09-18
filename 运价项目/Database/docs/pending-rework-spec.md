# 驳回回炉（pending rework）契约

2026-09-18 定稿。一句话：**驳回不是丢弃，是带原因退回；采集 agent 改一版后必须回到人工面前，放行权始终在人。**

## 状态流转

```
awaiting ──人工放行──> approved
   │  ──人工编辑(update)──> awaiting（校验态实时翻转）
   └─人工驳回(reject，带原因)──> rejected（移入「驳回日志」，行不删）
              │
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
| `POST /api/pending/delete` | 物理删除，**只在驳回日志内可用** |

采集侧用 `python scripts/rework.py list` / `... submit`，不手写 curl。

## 三条护栏（不可绕）

1. **字段白名单**：只能改「该行校验报错的字段」∪「驳回原因里用「」点名的字段」，且限核心字段集合。
   越权字段整条拒绝并返回 `rejected_fields`。另一会话的「组完整性」连坐标记（`GROUP_MARK`）不是真实字段错误，
   既不算可改字段、也不因它把白名单判空。
2. **轮次上限 2**：`edit_log` 里 `rework`/`rework_failed` 计数到 2 就拒绝再返修，要求人工到驳回日志处理——
   防止"改了不过 → 下轮再改"无限刷配额。**失败的尝试也计轮**（否则可以无限试）。
3. **不许编主数据**：提交值仍过台账强匹配闸门（`_strict_rejects`），查无此值就回退；
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

- **改名后历史行筛不到**：港口实体由 `Tianjin New Port` 改名为 `Xingang` 后，`freight_rates` 里 38 条历史行
  仍存旧字面值，`?pol=天津`（归一成 Xingang）返回 0 行。两种解法未定：
  ① 查询侧把筛选值展开成"该实体全部已知写法"再匹配（不动数据，推荐）；
  ② 改历史列值（**危险**：`起运港` 是判重身份键的组成部分，改了会破坏新增/更新判定与 `supersedes` 链）。
  `manual_check` 里两条相关用例目前故意保持红，就是这个未决事项的可见标记。
- 返修原因目前只有自由文本 + 校验 errors；驳回弹窗建议加错误类型下拉（主数据缺录/识别错字/字段漏填/价格柜型错），
  结构化后白名单更准。
