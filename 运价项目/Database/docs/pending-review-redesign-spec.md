# 待审核页重设计规范（卡片工单化 + 可编辑放行 + 驳回日志两级删除）

> 目标：把「待审核」从一张只读表格，改成**每条一张工单卡片**——信息展示对齐销售端详情，
> 审核者能对照**消息原文 + 图片**核字段，字段**可就地编辑**，改完按「更新」放行；
> **主队列移除删除按钮**，「驳回」= 软释放，把这条移入**驳回日志**（留驳回原因）；
> **只有在驳回日志里点「删除」才是物理删除**暂存行。两级设计：误驳可恢复，真要清才彻底删。
>
> 铁律沿用 `standardization-review-spec.md`：港口/船司/航线/港群归一只有 `manualdb.Master` 一个真源，
> 图片解析只有 `manual_server.images_for/_resolve_image` 一个真源，前端不另拼第二套。

涉及文件：`web/index.html`（卡片 UI + 编辑 + 灯箱 + 驳回日志）、`manual_server.py`（pending 列表附图片、新增 update 端点、reject/delete 保留）、
`manualdb.py`（reject 软释放 + 恢复、编辑重校验）、`manual_check.py`（回归用例跟随）、`docs/standardization-review-spec.md`（§8 端点表同步）。

---

## 需求映射（逐条对应本次诉求）

| 诉求 | 落地 |
|---|---|
| 条目换成卡片式 | §1 卡片工单布局，表格 → 卡片列表 |
| 传入信息可编辑 | §2 卡片内字段即编辑态，复用录入表单组件 |
| 编辑后按「更新条目」放行 | §2 放行前先落编辑 payload + 重校验 + 重判新增/更新 |
| 主队列移除删除按钮；驳回=软释放入日志；日志内删除=物理删除 | §3 reject 移入驳回日志（留原因），日志内「删除」才 DELETE 行，另有「恢复」撤回误驳 |
| 布局/信息展示大改，工单化 | §1 卡头(库/动作/校验/来源/时间) + 核心区 + 原文 + 图片 + 卡脚操作 |
| 信息和销售端展示相同效果 | §1 核心区复用销售端 `detail-grid` 字段布局与 `RATE_FIELDS/SPACE_FIELDS` |
| 看得到原文记录和图片 | §1 原文 `<pre>`（同销售端）+ §4 后端补图片解析 |
| 图片可点击预览 | §5 灯箱覆盖层（替换现在的 `window.open`） |

---

## 1. 卡片工单布局（前端）

`panel-pending`（约 723 行）整体重写：顶部筛选条 + 卡片列表容器，弃用 `<table class="data-table">`。

### 1.1 顶部工具条
- 状态筛选：`待处理 / 已放行 / 驳回日志 / 全部`（`驳回日志` = `review_status='rejected'` 的行，见 §3）。
- 库类型筛选：`全部 / 运价 / 舱位`（分段按钮，二选一不用下拉，遵用户偏好）。
- 右侧：计数 `共 N 条` + 「全库体检」按钮（沿用 `runAudit()`）。
- 刷新按钮。

### 1.2 单张卡片结构（一条 pending = 一张工单）
```
┌─ 卡头 ───────────────────────────────────────────────┐
│ [运价/舱位] [新增/更新] [校验:通过/未过]  来源:agent · 09-10 14:22   │
│ 未过时：错误摘要红字（哪个字段/传了什么/为什么不合规）              │
├─ 核心区（可编辑，布局=销售端 detail-grid）───────────────┤
│ 起运港* 目的港* 船司* 航线 柜型* 海运费USD* 有效期(起→止) ETD …  │
│ 运价用 RATE_FIELDS，舱位用 SPACE_FIELDS；核心字段带 data-ac 联想    │
├─ 原文 & 图片（对照区）─────────────────────────────┤
│ 消息原文：<pre> 原样展示（可编辑 textarea，改原文会影响判重→提示）  │
│ 图片：缩略图，点击开灯箱预览（§5）                              │
├─ 元信息 ───────────────────────────────────────────┤
│ 来源群 · 发送人 · 消息时间 · 归一对照(resolved)                    │
├─ 卡脚操作 ─────────────────────────────────────────┤
│ [保存修改]  [放行]  [驳回]        （无删除按钮）                  │
└──────────────────────────────────────────────────┘
```

- 核心字段渲染直接复用 `fieldHtml(f, parsed)`：`parsed` 由 payload 的英文列反查中文键得到（见 §2.1）。
  这样联想、硬校验、有效期区间选择器、整数框全部免费继承，和录入页/编辑弹层完全一致。
- 归一对照 `resolved`（服务端机械校验后的规范核心字段）在元信息区只读展示，方便审核者看"系统把它归成了什么"。
- 卡片默认展开核心字段；原文/图片区可折叠（`▸ 详情` 风格），长列表时不占屏。

### 1.3 CSS
- 新增 `.pd-card`（白底、1px 边框、圆角、卡头浅灰分隔），错误态 `.pd-card.failed` 左边框橙红。
- 复用现有 `.detail-grid / .lbl / .val / .form-field / .invalid`，不新造第二套字段样式。
- 错误态底色不透明（遵用户偏好）。

---

## 2. 可编辑 + 按「更新」放行

### 2.1 payload ↔ 表单字段桥接
payload 存的是**中文键**记录；`fieldHtml` 吃的是英文列 `parsed`。前端做一层反查：
```js
function payloadToParsed(kind, payload) {
  const map = kind === 'rates' ? RATE_MAP_INV : SPACE_MAP_INV;   // 中文→英文列，与服务端 RATES_MAP/SPACE_MAP 同源
  const p = {};
  Object.keys(payload || {}).forEach(cn => { const en = map[cn]; if (en) p[en] = payload[cn]; });
  // 有效期起止合成区间控件初值
  return p;
}
```
中文↔英文映射不新拼：直接复用页面已有的 `cnKey()`（英→中）反向表，或从 `/api/bootstrap` 带的列定义生成，保证和服务端 MAP 一致。

### 2.2 「保存修改」
收集卡片内字段 → `collectFormFields(cardEl)` 得中文键对象 → 先跑前端硬校验
（`gateCoreFields` + `checkIntFields` + `checkValidRange`，与录入/编辑弹层同一套闸门）→
POST 新端点 `POST /api/pending/update`：
```python
def act_pending_update(body):
    pid = int(body.get("id") or 0); fields = body.get("fields") or {}   # 中文键记录
    c = conn(); m = manualdb.get_master(c)
    try:
        return manualdb.update_pending(c, m, pid, fields)
    finally:
        c.close()
```
`manualdb.update_pending`：重跑 `validate_record` + `classify_new_or_update`，
回写 `payload / resolved / action / target_key / check_status / errors`。
即**编辑后校验状态实时刷新**：failed 的修好就变 passed，放行按钮解禁。

### 2.3 「放行」（一步到位）
放行 = 若卡片有未保存改动，先隐式 `update` 落 payload + 重校验 → 再 `approve`。
- 服务端 `approve_pending` 逻辑不变（读最新 payload，自动判新增/更新，update 走 `revise_record` 留痕）。
- 只有 `check_status='passed'` 才允许放行；failed 禁用按钮并提示先修。
- 前端 `approvePending(id)` 改为：`gateCoreFields(card)` → 若有改动 POST update → POST approve → toast「已放行入库（运价/舱位）· 新增/更新」。

> 「更新条目放行」不需要用户手动选新增/更新：编辑 payload 后服务端按
> `起运港+目的港+船司+原文` 自动判定（§9 既有规则）。审核者改了价/有效期→多半判更新留痕；改了原文→判新增。卡片实时显示当前判定徽标。

---

## 3. 驳回日志：两级删除（软释放 → 日志内物理删除）

### 3.1 语义（两级）
- **第一级·驳回（软释放）**：主队列卡片点「驳回」→ 填驳回原因 → 该行 `review_status='rejected'`、记 `note/reviewed_at/reviewed_by`，
  **移入「驳回日志」视图**，从待处理队列消失。**行仍在库里**，可恢复、可回查驳了什么、为什么驳。
- **第二级·删除（物理删除）**：只有在「驳回日志」视图里点「删除」，才真正 `DELETE FROM pending_records`，彻底清掉这条暂存。
- **恢复（撤回误驳）**：驳回日志里每条带「恢复」→ `review_status` 改回 `awaiting`，重回待处理队列。误驳不必让 agent 重传。
- 安全性：pending 行只是暂存，从未进业务表；原图在 `images/`、原文在云端群消息，物理删除也不丢任何台账数据 → 低风险。
  两级设计的意义在于**主队列永不出现不可逆删除**：审核手滑只会软驳，真要清库得专门进日志再删一道。

### 3.2 后端
`reject_pending` 恢复为软驳（保留行，写原因）；`delete_pending` 保留，仅供驳回日志调用；新增 `restore_pending`：
```python
def reject_pending(conn, pid, note="", reviewed_by=""):
    cur = conn.execute(
        "UPDATE pending_records SET review_status='rejected', note=?, reviewed_at=?, reviewed_by=? "
        "WHERE id=? AND review_status='awaiting'", (note, now_cn(), reviewed_by, pid))
    conn.commit()
    return {"ok": bool(cur.rowcount)} if cur.rowcount else {"ok": False, "error": "not found/已处理"}

def restore_pending(conn, pid):                      # 撤回误驳：rejected → awaiting
    cur = conn.execute("UPDATE pending_records SET review_status='awaiting', note=NULL "
                       "WHERE id=? AND review_status='rejected'", (pid,))
    conn.commit()
    return {"ok": bool(cur.rowcount)} if cur.rowcount else {"ok": False, "error": "not found/非驳回态"}

def delete_pending(conn, pid):                       # 物理删除：仅驳回日志内触发
    cur = conn.execute("DELETE FROM pending_records WHERE id = ?", (pid,))
    conn.commit()
    return {"ok": bool(cur.rowcount), "deleted": cur.rowcount}
```
- 端点：`/api/pending/reject`（软驳）、`/api/pending/delete`（物理删，写令牌）**都保留**；新增 `POST /api/pending/restore`（写令牌）。
- `list_pending` 的 `status='rejected'` 分支即「驳回日志」数据源，无需改查询。

### 3.3 前端
- **主队列卡片**操作：`[保存修改] [放行] [驳回]`——**无删除按钮**。`rejectPending(id)` 弹原因输入（`prompt`，可空）→ 调 reject → toast「已移入驳回日志」→ 该行离开待处理。
- **驳回日志视图**（状态筛选选「驳回日志」时）：每条紧凑卡片展示 核心字段 + 驳回原因 + 驳回时间，操作 `[恢复] [删除]`。
  - `restorePending(id)` → confirm「恢复到待处理队列？」→ 调 restore → 回待处理。
  - `deletePending(id)` → confirm「彻底删除这条驳回记录？不可恢复」→ 调 delete → 行消失。
- 状态筛选 options（730 行）：`待处理 / 已放行 / 驳回日志 / 全部`。

---

## 4. 待审核列表附图片解析（后端补，关键）

现状：`list_pending` 回的 payload 里图片只是文件名，前端无法显示。
改法：`act_pending_list`（manual_server.py 431 行）对每行 payload 跑 `images_for`，附 `images`：
```python
def act_pending_list(q):
    ...
    res = manualdb.list_pending(c, status=..., kind=..., limit=..., offset=...)
    for row in res.get("rows", []):
        pl = row.get("payload") or {}
        row["images"] = images_for(pl)      # 复用销售端同一解析：image_file/image_url/原文 mediaId → /images/<名> 或 missing
    return {"ok": True, **res}
```
- `images_for` 认 `image_file / image_url`，并在无显式引用时按 `消息原文` 里的 mediaId 兜底找同名图（既有逻辑，单一真源）。
- 前端卡片用 `imagesBlock(row.images, 160)` 渲染；缺文件显示"图片未同步"，不静默空白。

---

## 5. 图片灯箱预览（前端）

现状 `imagesBlock` 里缩略图 `onclick="window.open(this.src)"`——新开标签页，不算"预览"。
改为灯箱覆盖层：
```js
function openLightbox(src) {
  let lb = $('lightbox');
  if (!lb) { lb = document.createElement('div'); lb.id = 'lightbox';
    lb.innerHTML = '<img id="lightbox-img"><div class="lb-hint">点击任意处或按 ESC 关闭</div>';
    lb.onclick = closeLightbox; document.body.appendChild(lb);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); }); }
  $('lightbox-img').src = src; lb.classList.add('open');
}
function closeLightbox() { const lb = $('lightbox'); if (lb) lb.classList.remove('open'); }
```
- `imagesBlock` 的 `onclick` 从 `window.open(this.src)` 改 `openLightbox(this.src)`（销售端详情图同步受益，统一体验）。
- CSS：全屏暗遮罩（无白底，遵用户偏好）、大图 `max-width/height:92vh` 居中、`cursor:zoom-out`。

---

## 6. 接线清单（改动点汇总）

**前端 `web/index.html`**
- `panel-pending`（723）：重写为工具条 + `#pd-cards` 容器。
- `renderPending`（3100）：表格 → 卡片渲染（`pdCardHtml(row)`）；`status='rejected'` 时走驳回日志紧凑卡片（`pdLogCardHtml(row)`，带 `[恢复][删除]`）。
- 新增 `pdCardHtml / pdLogCardHtml / payloadToParsed / savePendingEdit / restorePending / openLightbox / closeLightbox`。
- `approvePending`（3133）：放行前隐式保存 + 硬校验。
- `rejectPending`（3142）：保留原因 `prompt`，文案改「已移入驳回日志」。
- `deletePending`（3149）：保留，但只在驳回日志视图触发，confirm 改「彻底删除这条驳回记录？不可恢复」。
- `imagesBlock`（1268）：`onclick` 改灯箱。
- 状态筛选 options（730）：`待处理 / 已放行 / 驳回日志 / 全部`；加库类型分段按钮。

**后端 `manual_server.py`**
- `act_pending_list`（431）：每行附 `images_for(payload)`。
- 新增 `act_pending_update` + 路由 `POST /api/pending/update`（写令牌）。
- 新增 `act_pending_restore` + 路由 `POST /api/pending/restore`（写令牌）。
- `/api/pending/reject`、`/api/pending/delete` 路由**都保留**。

**数据层 `manualdb.py`**
- 新增 `update_pending(conn, master, pid, fields)`：重校验 + 重判 + 回写。
- `reject_pending`（677）：软驳（`review_status='rejected'` + note/reviewed_at/reviewed_by），**保留行**。
- 新增 `restore_pending(conn, pid)`：rejected → awaiting。
- `delete_pending`（684）：保留（物理 DELETE），仅驳回日志调用。

**回归 `manual_check.py`**
- 待审核段（214–234）：补 `update` 后校验状态翻转用例；`reject` 后行进 `status=rejected` 列表（未消失）用例；`restore` 后回 awaiting 用例；`delete` 后行彻底消失用例。
- 沙箱 `CREATE TABLE pending_records` 若有列变动同步（本次无列变动）。

**文档**
- `standardization-review-spec.md` §8.2 端点表：`reject` 语义标「软驳入日志」、`delete` 标「仅日志内物理删」、加 `update` / `restore` 两行。

---

## 7. 验收清单

1. `python validity_rule_check.py` + `python manual_check.py` 全绿。
2. 待审核页每条是一张卡片；核心字段布局与销售端详情一致；运价/舱位各自字段齐全。
3. 卡片能看到**消息原文**（`<pre>`）与**图片缩略图**；点图开灯箱大图，ESC/点击关闭。
4. 卡片字段可编辑；改脏值（不存在的船司/港）→ 标红 + 放行被挡 + 明确提示。
5. 「保存修改」后校验状态实时刷新（failed 修好变 passed，放行解禁）。
6. 「放行」用编辑后的最新 payload 入库，自动判新增/更新（更新留痕），toast 标清。
7. 主队列**无删除按钮**；「驳回」→ 填原因 → 该行进「驳回日志」（库里仍在），离开待处理队列。
8. 驳回日志每条带 `[恢复][删除]`：恢复→回待处理；删除→物理删行、不可恢复、列表消失。
9. 未过校验的卡片：放行禁用、错误摘要红字可见、不能放行。

---

## 8. 第二轮修订（对齐评审 7 点反馈）

- **卡片更紧凑·两栏**：`.pd-two` 左栏可编辑字段（`fieldHtml` 复用），右栏上=消息原文 textarea（`flex:1` 撑高）、下=图片块；**无图时图片块不渲染，原文自动占满右栏**。窄屏 `@media(max-width:980px)` 塌成单栏。
- **运价⇄舱位互切（点 4）**：卡片头部 `.sub-tabs` 分段按钮调 `pdSwitchKind`，先并入当前编辑值再换 kind、只重画这张卡；`update_pending(conn, master, pid, fields, kind)` 支持覆盖 `kind`，保存/放行带 kind 落库。契约侧：运价/舱位共用字段并集，`kind` 只是提示（见 `agent-input-contract.md`）。
- **归一对照移到放行旁（点 5）**：`pdResolvedInline` 以 chips 形式内联在 `.pd-foot`，紧跟放行按钮。
- **去掉"来源 agent"废话（点 6）**：卡头 meta 只留提交时间。
- **合并港口必拆（点 2）**：`stage_pending` 先跑 `master.expand_cn` 把 `A/B`、顿号合并港按港拆成**每港一张工单**（港群名不拆），中文/别名/拼音顺带归一。**注意**：这条拆分闸**不能**塞进 `resolve_core.port_side`——那会连人工 `push` 的"多港自动拆两行"一起挡掉（实测翻车）。合并港的治理放在暂存入口，`port_side` 维持逐 token 解析原样。
- **中文别名不影响标准化（点 7）**：`lookup_port`/`S.portIdx` 都索引了 `name_cn`，`青岛→Qingdao` 服务端会归一；输入框输"青岛"能匹配到、只是候选显示规范名。经暂存拆分归一后，卡片里直接是规范英文名。
- **测试去写死值（点 1）**：`manual_check.py` 港群数不再钉死 6（改"主数据非空 + NEB 在列"），目的港规范名不再钉死 `Manzanillo(MX)`（改断言"拆成两行且都 ∈ master 规范名集合"）。**以系统实时为准，agent/用例不必读死值——校验口强制对齐**。

验收追加：合并港 `Santos/曼萨尼约` 暂存 → 出 2 张单港卡、payload 无 `/`；卡片右上运价/舱位可切、切后备注/原文等共享字段不丢；`manual_check.py` 全绿（含上述新用例）。


---

## 9. 放行自动登记来源群 / 发送人（库管理不再"啥都没有"）

**现象**：录入端「来源群」下拉有预设值，但库管理里的**群聊库**是空的。根因——下拉在群聊库为空时兜底吃业务表 `source_group` 的使用频次（`S.opts.rates.source_group`），而放行入库这条路径从来只写业务表，从不往 `chat_groups` / `senders` 两库写；两库此前只能靠 `groups.json` 导入或手工录入。

**决策**：两库设为可写入（人工端 `saveGroup` / `master_save(senders)` 本就已开），并新增自动写入口——**条目放行入库时**，把该条里库里还没有的来源群、发送人分别登记进群聊库、发送人库。

**契约（`manualdb.approve_pending`）**：
- 放行成功（业务表已落库、状态置 approved）后，调用 `register_source_entities(conn, payload)`；响应回带 `registered = {group, sender, group_added, sender_added}`。
- **只补缺、绝不覆盖**已有条目：群已在 `chat_groups` 就不动它（保护维护好的 `default_pol` / `owner` / `cid`），人已在 `senders` 同理；命中判定用 `name` 精确匹配（`strip` 后）。
- 来源群/发送人为空 → 跳过、不报错。
- 新增了群或人才 `invalidate_master()`，让候选下拉与归一索引即时跟上；未新增不动缓存。
- **幂等**：整批放行里同群同人多条只登记一次；重复放行（同 `name`）第二次 `*_added=false`。
- 作用域只挂在**放行**入口（`approve_pending`）；人工 `push` 录入不触发（录入端本就按库选/带出）。

**前端（`web/index.html`）**：`syncLibAfterApprove(res)` 读取 `registered`，若有新增则 `refreshChatGroups()+loadMasterData()` 刷新候选，并把"已登记…进库"拼进放行 toast；整批放行在循环里用 `Set` 去重累计新群/新发送人数量后统一提示。切到「库管理」页 `loadAdmin()` 会重新拉取，新登记项即时可见。

**验收**：
- 副本库跑：造 `来源群/发送人` 库里都没有的记录 → 放行 → 两库各 +1、`registered` 两个 `*_added=true`；再放同群同人 → `*_added=false` 且无重复行；预置 `default_pol` 后放同群 → `default_pol` 未被抹掉；空群/人放行 → 不报错。
- 生产：服务重启后，此后每放行一条含新群/新人的待审核，库管理群聊库/发送人库随之增长。
