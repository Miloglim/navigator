# 标准化 + 待审核 改造方案（交 flash 执行）

> 目标：把台账做成 **100% 标准化** 的库——核心字段只能落在主数据预设值上，非人工入口先机械校验再人工放行。
> 本文只给方案 + 核心代码改动，逐条对应需求 1–9。改完先跑 `python validity_rule_check.py` 和 `python manual_check.py` 全绿再收工。
>
> **贯穿全局的一条铁律**：港口/船司/航线/港群的归一只有 `manualdb.Master` 一个真源。前端、批量、待审核、agent 校验全部复用它，**任何地方都不许再拼第二套映射**。

涉及文件：`web/index.html`（前端）、`manualdb.py`（数据层 + 校验）、`manual_server.py`（接口）、`docs/agent-input-contract.md`（需求 7）。

---

## 0. 总原则与三处集中改动

1. **校验前移到写入闸门**：新增 `manualdb.validate_record()`，人工录入 / 批量 / 待审核 / agent 四条入口共用（需求 3、6、8、9）。
2. **核心字段硬匹配**：解析不到主数据预设值 = 拒绝入库（不再像现在 `expand_cn` 那样"原样入库 + 记 unmatched"）。
3. **新增/更新统一判定**：`起运港 + 目的港 + 船司 + 原文` 全同 = 更新，走留痕 `revise_record`；否则新增（需求 9）。

新增到 `manualdb.py` 的常量（放在 `FILTERS` 附近）：

```python
CORE_MATCH = ("起运港", "目的港", "船司", "航线")   # 两库通用：填了必须命中主数据（航线可空）
# "不得为空"分库：运价要 目的港/柜型/海运费；舱位动态常无目的港，改要 船名航次+舱位类型。
# 有效期两库都必填（舱位库已补 validity_raw/valid_from/valid_to 三列）。
REQUIRED_BY_KIND = {
    "rates": ("起运港", "目的港", "船司", "柜型", "海运费USD"),
    "space": ("起运港", "船司", "船名航次", "舱位类型"),
}
VALIDITY_REQUIRED_KINDS = {"rates", "space"}       # 运价、舱位都强制有效期非空
```

---

## 1. 有效期区间选择器"点一下就消失，选不了"

**根因（已定位）**：`web/index.html` 末尾的 document 级点击关闭监听（约 2981–2983 行）：

```js
document.addEventListener('click', e => {
  const el = e.target;
  if (!el.closest || !el.closest('#rp-pop') && !el.closest('.range-btn')) rpClose();
});
```

点日历里的某天 → 按钮 `onclick="rpPick(...)"` 先跑 → 第一次点只设起点、调 `rpRender()`，而 `rpRender` 用 `pop.innerHTML=...` **重建了整棵日历**，被点的那个 `<button>` 当场从 DOM 脱离。等这个 click 冒泡到 document 时，`e.target` 已是**游离节点**，`el.closest('#rp-pop')` 返回 null → 判定为"点在弹层外" → `rpClose()`。于是永远点不满两天，表现为"一点就没"。

**修法（择一，推荐 A）**：

A. 在 `rpPop()` 创建弹层时，让弹层内的点击不冒泡到 document（一处根治，日历/快捷/清除全受益）：

```js
function rpPop() {
  let el = document.getElementById('rp-pop');
  if (!el) {
    el = document.createElement('div');
    el.id = 'rp-pop';
    el.style.cssText = '...';           // 原样不动
    el.addEventListener('click', e => e.stopPropagation());   // ← 新增这一行
    document.body.appendChild(el);
  }
  return el;
}
```

B.（备选）把关闭判定改成不依赖可能已游离的 target，用派发时的路径：

```js
document.addEventListener('click', e => {
  const path = e.composedPath();                       // 派发时快照，innerHTML 重建也还在
  if (path.some(n => n && (n.id === 'rp-pop' || (n.classList && n.classList.contains('range-btn'))))) return;
  rpClose();
});
```

选 A 即可，B 不必同时上。

---

## 2. 销售端没有航管端那样的输入联想

**现状**：航管端录入框用自研联想组件（`data-ac` + `acShow/acQuery/acPop`，约 1707–1850 行）；销售端筛选框（约 513–525 行）用的是浏览器原生 `<input list="dl-pol"><datalist>`，样式/触发都弱，用户感知为"没有联想"。

**方案**：销售端筛选框也改用同一个 `data-ac` 组件，删掉 `datalist`。document 上的 `input` 监听（1822 行）对静态输入框同样生效，改属性即可复用。

改动（销售端每个筛选框，513–525 行）：

```html
<!-- 改前 -->
<div class="filter-group"><label>起运港</label><input id="sf-pol" list="dl-pol"><datalist id="dl-pol"></datalist></div>
<!-- 改后：list/datalist 去掉，换 data-ac -->
<div class="filter-group"><label>起运港</label><input id="sf-pol" data-ac="pol" autocomplete="off"></div>
```

对应映射：`sf-pol→pol`、`sf-pod→pod`、`sf-carrier→carrier`、`sf-route→route`、`sf-group→group`、`sf-stype→stype`、`sf-ctype→ctype`。有效期/消息时间是 `type="date"`，不动。

`acSource(kind)`（1707 行）补两个枚举来源，让柜型/舱位类型也能联想（数据取自 `S.opts`）：

```js
if (kind === 'ctype') return enumItems('container_type');
if (kind === 'stype') return enumItems('space_type');
// 顶部加个小工具：
function enumItems(col) {
  const o = S.opts || { rates:{}, space:{} };
  const vals = [...new Set([...((o.rates||{})[col]||[]), ...((o.space||{})[col]||[])])];
  return vals.map(v => ({ v, s:'', k:[v] }));
}
```

`populateFilters()`（816 行）里给这些 `dl-*` 填充 datalist 的代码可留可删（datalist 已不存在，填了也没用），建议顺手删掉对应 `fillList('dl-pol',...)` 等调用，避免死代码（用户偏好：删功能连带清死代码）。

---

## 3. 起运港/目的港/船司/柜型/航线 硬匹配（选不到固定值就不给录）

**现状**：`normalizePortField`（1853）、`normalizeMasterField`（1874）是**软归一**——命中就替换，命不中就**保留用户乱填的原文**；后端 `expand_cn` 也只把 unmatched 记一笔、照样入库。柜型已是三档 select（OK）。

**方案**：改成硬闸门，前后端双保险，且**都走 Master 单一真源**。

### 3.1 前端：命不中主数据就标红 + 挡提交（不清空，让用户看见自己填错了什么）

```js
function markField(el, ok, msg) {
  el.classList.toggle('invalid', !ok);          // .invalid{border-color:#c2410c;background:#fff5f2}
  if (!ok && msg) toast(msg, 'error');
  return ok;
}
// 归一 + 硬校验：命中→写规范值；命不中→标红，返回 false
function hardNormalizePort(el, side) {
  const raw = String(el.value || '').trim();
  if (!raw) return markField(el, el.dataset.req !== '1', '起运港/目的港必填');
  const g = (((S.master||{}).portGroups)||[]).find(x => [x.n,x.l].some(k => k && foldKey(k)===foldKey(raw)));
  if (g) { el.value = g.n; el.classList.remove('invalid'); el.classList.add('filled'); return true; }
  const hit = S.portIdx && (S.portIdx.get(foldKey(raw)) || S.portIdx.get(foldKey(raw.split(',')[0])));
  if (hit) { el.value = hit; el.classList.remove('invalid'); el.classList.add('filled'); return true; }
  return markField(el, false, (side==='pol'?'起运港':'目的港')+'「'+raw+'」不在港口/港群主数据里，请从联想里选');
}
function hardNormalizeMaster(el, which) {       // carrier / route
  const raw = String(el.value||'').trim();
  if (!raw) return which === 'route';           // 航线可空；船司空由必填闸拦
  const idx = which==='carrier' ? S.carrierIdx : S.routeIdx;
  const hit = idx && idx.get(foldKey(raw));
  if (hit) { el.value = hit; el.classList.remove('invalid'); el.classList.add('filled'); return true; }
  return markField(el, false, (which==='carrier'?'船司':'航线')+'「'+raw+'」不在主数据里，请从联想里选');
}
```

- `fieldHtml`（1393–1404）里把 `onchange="normalizePortField(...)"` 换成 `hardNormalizePort(...)`、`normalizeMasterField` 换成 `hardNormalizeMaster`；必填项加 `data-req="1"`。
- `submitRate`（1945）/`submitSpace`（2029）提交前，对四个核心框逐个跑硬校验，任一 false 就 `return`（把现在的 `normalizeRecordPorts` 软归一替换成硬校验）：

```js
function gateCoreFields(containerId) {
  let ok = true;
  $(containerId).querySelectorAll('[data-ac]').forEach(el => {
    const k = el.getAttribute('data-ac');
    if (k==='pol')      ok = hardNormalizePort(el,'pol') && ok;
    else if (k==='pod') ok = hardNormalizePort(el,'pod') && ok;
    else if (k==='carrier') ok = hardNormalizeMaster(el,'carrier') && ok;
    else if (k==='route')   ok = hardNormalizeMaster(el,'route') && ok;
  });
  return ok;
}
// submitRate 里： if (!gateCoreFields('rate-fields')) return;
```

新增 CSS：`.invalid{border-color:#c2410c !important;background:#fff5f2 !important}`（错误态底色要不透明，用户明确偏好）。

### 3.2 后端：`validate_record` 硬闸（见第 6 节），`push_records` 命中错误直接拒收

现在 `expand_cn` 对 unmatched 港"原样入库"，要改成：核心字段解析不到 → 该记录进 `rejected`，不写库，回执里带原因。

---

## 4. 批量录入"模式"改成按钮选择（不用下拉）

**现状**：`<select id="bt-mode">`（622 行），两个 option：new / revise。用户偏好二选一用分段按钮。

**方案**：换成 `.sub-tabs` 分段按钮（样式已存在，350–381 行）：

```html
<div class="sub-tabs" id="bt-mode-tabs">
  <button data-mode="new" class="active" onclick="setBtMode('new',this)">新增</button>
  <button data-mode="revise" onclick="setBtMode('revise',this)">留痕更新</button>
</div>
```

```js
function setBtMode(m, btn) {
  BT.mode = m;
  document.querySelectorAll('#bt-mode-tabs button').forEach(b => b.classList.toggle('active', b===btn));
}
```

把所有 `$('bt-mode').value` 引用（`previewBatch` 2248/2249、`clearBatch` 2395）改成 `BT.mode`。`BT` 初值 `mode:'new'` 保持。

---

## 5. 批量录入支持"运价 / 舱位"双选 + 与目标库字段取交集

**现状**：批量只入运价（`btCommitNew`/`btCommitRevise` 写死 `table:'rates'`，2316/2355）。列定义 `BT_COLS`（2083）是运价周表。

**方案**：

1. 模式区再加一组**多选**分段按钮（可只选一个，也可两个都选）：

```html
<div class="sub-tabs" id="bt-target-tabs">
  <button data-t="rates" class="active" onclick="toggleBtTarget('rates',this)">运价库</button>
  <button data-t="space" onclick="toggleBtTarget('space',this)">舱位库</button>
</div>
```
```js
BT.targets = ['rates'];                       // 初值
function toggleBtTarget(t, btn) {
  const i = BT.targets.indexOf(t);
  if (i>=0) { if (BT.targets.length===1) return toast('至少选一个目标库','error'); BT.targets.splice(i,1); }
  else BT.targets.push(t);
  btn.classList.toggle('active', BT.targets.includes(t));
}
```

2. **字段取交集**：每个目标库只收它自己 MAP 里有的中文字段（需求原话："运价库没有的字段不录入运价库"）。用 `manualdb` 的 `RATES_MAP`/`SPACE_MAP` 的中文键做投影。前端已有 `cnKey`，但交集应以服务端 MAP 为准——最稳是**在服务端投影**：`push_records` 已经按 `spec["map"]` 取值（`normalize` 只认 map 里的中文键），天然就是交集，多余字段被忽略。所以前端把整行中文字段原样发过去，服务端按目标 kind 各投一次即可。

3. `btCommitNew` 改成对 `BT.targets` 每个 kind 各发一批：

```js
function btCommitNew(t0) {
  const recs = BT.rows.map(r => r.rec);
  const jobs = [];
  BT.targets.forEach(kind => {
    for (let i=0;i<recs.length;i+=200) jobs.push({kind, off:i, part:recs.slice(i,i+200)});
  });
  // 逐个 job fetch('/api/push',{table:job.kind, records:job.part, origin:'manual'})
  // 成功/失败/unmatched 汇总时按 kind 分别计数，回执标清"运价库 X 条 / 舱位库 Y 条"
}
```

4. `btCommitRevise` 同理：定位与 `revise` 的 `table` 用当前 kind；两个库都选就各定位各的。

**决策/取舍（需 flash 注意，也请用户过目）**：运价周表的列（20GP/40HQ/40NOR 价、有效期、Freetime…）里**没有**舱位库的必填列（船名航次、舱位类型）。取交集后，舱位记录会因缺必填被第 6 节校验拦下并在回执里报"舱位库：缺船名航次/舱位类型"。这是**预期行为**（交集语义的自然结果），不是 bug。若以后要批量导舱位，另做一套舱位列模板即可，本次不做。

---

## 6. 统一校验规则（预检 + 全库检查共用）

**规则（需求 6）**：
- 起运港、目的港、船司、有效期 **不得为空**（有效期＝`有效期起`/`有效期止` 至少一端非空且为 `YYYY-MM-DD`）。
- 起运港、目的港、船司、航线 **必须命中主数据预设值**（港口/港群、carriers、routes），不得出现预设以外的内容。
- 沿用既有：价格/天数必须整数（`INT_COLS`）。

**核心代码（新增到 `manualdb.py`）**——所有解析走 `Master`，单一真源：

```python
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def resolve_core(master: "Master", kind: str, rec: Dict[str, Any]) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """把核心字段解析成规范值；返回 (resolved, errors)。群名/成员港/别名/拼音全走 Master。"""
    errors: List[Dict[str, str]] = []
    resolved: Dict[str, str] = {}

    def port_side(cn: str, side: str) -> str:
        raw = str(rec.get(cn) or "").strip()
        if not raw:
            return ""
        g = master.group_for(raw, side)                  # 港群名（含中文标签）
        if g:
            return g
        name, ok = master.lookup_port(raw)               # 港口别名/中英/拼音/LOCODE
        if ok:
            return name
        errors.append({"field": cn, "input": raw, "msg": f"{cn}「{raw}」不在主数据里"})
        return raw                                        # 保留原值供报错展示，但不算通过

    resolved["起运港"] = port_side("起运港", "pol")
    resolved["目的港"] = port_side("目的港", "pod")

    def master_side(cn: str, idx: Dict[str, str]) -> str:
        raw = str(rec.get(cn) or "").strip()
        if not raw:
            return ""
        hit = idx.get(fold(raw)) or idx.get(fold(raw.split(",")[0]))
        if hit:
            return hit
        errors.append({"field": cn, "input": raw, "msg": f"{cn}「{raw}」不在主数据里"})
        return raw

    resolved["船司"] = master_side("船司", master.carrier_idx)
    resolved["航线"] = master_side("航线", master.route_idx)

    # 非空闸（分库）
    for cn in REQUIRED_BY_KIND.get(kind, ()):
        if not str(rec.get(cn) or "").strip():
            errors.append({"field": cn, "input": "", "msg": f"{cn}不得为空"})
    # 有效期闸（仅运价强制）
    if kind in VALIDITY_REQUIRED_KINDS:
        vf, vt = str(rec.get("有效期起") or "").strip(), str(rec.get("有效期止") or "").strip()
        if not (vf or vt):
            errors.append({"field": "有效期", "input": "", "msg": "有效期不得为空"})
        for cn, v in (("有效期起", vf), ("有效期止", vt)):
            if v and not _ISO_DATE.match(v):
                errors.append({"field": cn, "input": v, "msg": f"{cn}必须是 ISO 日期 YYYY-MM-DD"})
    return resolved, errors


def validate_record(conn, master, kind: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    """机械校验（预检 / 全库 / 待审核 / 录入闸 共用）。返回 {ok, resolved, errors}。"""
    resolved, errors = resolve_core(master, kind, rec)
    # 整数列
    inv = {v: k for k, v in TABLES[kind]["map"].items()}
    for col, label in INT_COLS.get(kind, {}).items():
        cn = inv.get(col)
        v = str(rec.get(cn) or "").strip()
        if v and not re.fullmatch(r"-?\d+", v):
            errors.append({"field": cn or col, "input": v, "msg": f"{label} 必须是整数"})
    return {"ok": not errors, "resolved": resolved, "errors": errors}
```

**接线**：
- `push_records`（441 行）在 `normalize` 之前对每条 `rec` 跑 `validate_record`；`ok=False` 的进 `rejected` 列表（带 errors），不写库。回执从 `{accepted, unmatched}` 扩成 `{accepted, rejected[], unmatched}`。前端 `btFinish`/`submitRate` 展示 rejected 原因。
- **预检**（前端 `previewBatch` 2246）：新增只读端点 `GET/POST /api/validate {table, records}` → 服务端批量跑 `validate_record`，回每行 `{ok, resolved, errors}`。前端预检表用服务端结果渲染（把现在纯前端的归一预览换成"服务端权威校验"，避免前后端两套判断）。
- **全库检查**（需求 6"整个数据库的全局数据检查"）：新增 `GET /api/audit`：

```python
def act_audit() -> Dict[str, Any]:
    c = conn(); m = manualdb.get_master(c); out = []
    try:
        for kind in ("rates", "space"):
            inv = {v: k for k, v in manualdb.TABLES[kind]["map"].items()}
            for r in c.execute(f"SELECT * FROM {manualdb.TABLES[kind]['table']}"):
                rec = {inv[col]: r[col] for col in r.keys() if col in inv}
                res = manualdb.validate_record(c, m, kind, rec)
                if not res["ok"]:
                    out.append({"kind": kind, "content_key": r["content_key"],
                                "pol": r["pol"], "pod": r["pod"], "carrier": r["carrier"],
                                "errors": res["errors"]})
        return {"ok": True, "violations": out, "count": len(out)}
    finally:
        c.close()
```
在"待审核"页放一个「全库体检」按钮调它，列出所有不达标存量行（正则/主数据双查）。

---

## 7. 给 agent 的库结构 + 字段契约

见 **`docs/agent-input-contract.md`**（单独一份，直接发给接入方）。要点：agent 识别完群聊报价后，产出**中文字段 JSON**，核心字段必须已是主数据规范值，POST 到 `/api/pending` 进待审核，不直接落库。

---

## 8. 待审核页（加在"账号 & 群聊"后面）

**用途**：非人工入口（agent / 外部导入）的数据先机械校验（第 6 节规则），暂存待人工放行，通过后才入真库。人工录入已在录入页当场校验，不进这里。

### 8.1 新表 `pending_records`（加进 `manualdb.DDL` + `INDEX_PLAN`）

```python
"pending_records": """CREATE TABLE IF NOT EXISTS pending_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                          -- rates | space
    payload TEXT NOT NULL,                       -- agent 传入的中文字段记录 JSON
    origin TEXT NOT NULL DEFAULT 'agent',
    resolved TEXT,                               -- 机械校验后的规范核心字段 JSON（审核对照用）
    action TEXT,                                 -- new | update（第 9 节判定）
    target_key TEXT,                             -- action=update 时指向的 content_key
    check_status TEXT NOT NULL DEFAULT 'pending',-- passed | failed
    errors TEXT,                                 -- 校验错误 JSON
    review_status TEXT NOT NULL DEFAULT 'awaiting', -- awaiting | approved | rejected
    final_key TEXT,                              -- 放行后落库的 content_key
    reviewed_by TEXT, reviewed_at TEXT, note TEXT, created_at TEXT)""",
```
`INDEX_PLAN["pending_records"] = ["review_status", "check_status", "kind", "created_at"]`

### 8.2 端点（`manual_server.py`，写操作都要 `x-push-token`）

> 待审核页已重设计为卡片工单 + 可编辑放行 + 驳回日志两级删除，详见 `docs/pending-review-redesign-spec.md`。

```python
POST /api/pending           # {kind, records:[中文字段], origin='agent'} → 逐条 validate_record + 判新增/更新 → 落 pending_records
GET  /api/pending           # ?status=awaiting|approved|rejected|all&kind=&limit=&offset= → 列表（每行附 images_for(payload) 解析后的图片）
POST /api/pending/approve   # {id, reviewed_by?} → 放行：action=update 走 revise_record(target_key)，否则 push_records；写 final_key + review_status=approved
POST /api/pending/update    # {id, fields} → 编辑暂存记录：重跑 validate_record + 判新增/更新，回写 payload/resolved/check_status/errors
POST /api/pending/reject    # {id, note?, reviewed_by?} → 软驳：review_status=rejected，移入驳回日志（留原因，行仍在库，可恢复）
POST /api/pending/restore   # {id} → 撤回误驳：rejected → awaiting，重回待处理
POST /api/pending/delete    # {id} → 物理删除暂存行（仅驳回日志内触发，不可恢复）
```

`POST /api/pending` 核心（机械校验 + 新增/更新判定，一次做掉需求 8-1 和 9）：

```python
def act_pending(body):
    kind = _kind(body); records = body.get("records") or []
    if not records: raise ValueError("records empty")
    origin = body.get("origin") or "agent"
    c = conn(); m = manualdb.get_master(c); now = manualdb.now_cn(); ids = []
    try:
        for rec in records:
            res = manualdb.validate_record(c, m, kind, rec)
            action, target = manualdb.classify_new_or_update(c, kind, rec, res["resolved"])  # 第 9 节
            cur = c.execute(
                """INSERT INTO pending_records(kind,payload,origin,resolved,action,target_key,
                     check_status,errors,review_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?, 'awaiting', ?)""",
                (kind, json.dumps(rec, ensure_ascii=False), origin,
                 json.dumps(res["resolved"], ensure_ascii=False), action, target,
                 "passed" if res["ok"] else "failed",
                 json.dumps(res["errors"], ensure_ascii=False), now))
            ids.append(cur.lastrowid)
        c.commit()
        return {"ok": True, "staged": len(ids), "ids": ids}
    finally:
        c.close()
```

放行（approve）——**只有 `check_status='passed'` 才允许放行**，failed 的必须人工先修 payload 或驳回：

```python
def act_pending_approve(body):
    pid = int(body.get("id") or 0); c = conn(); m = manualdb.get_master(c)
    try:
        row = c.execute("SELECT * FROM pending_records WHERE id=?", (pid,)).fetchone()
        if not row: return {"ok": False, "error": "not found"}
        if row["review_status"] != "awaiting": return {"ok": False, "error": "已处理"}
        if row["check_status"] != "passed": return {"ok": False, "error": "机械校验未通过，不能放行", "errors": json.loads(row["errors"] or "[]")}
        rec = json.loads(row["payload"]); kind = row["kind"]
        if row["action"] == "update" and row["target_key"]:
            r = manualdb.revise_record(c, m, kind, row["target_key"], rec, origin=row["origin"])
            final = (r.get("new_keys") or [None])[0]
        else:
            r = manualdb.push_records(c, m, kind, [rec], origin=row["origin"])
            final = None
        c.execute("UPDATE pending_records SET review_status='approved', final_key=?, reviewed_at=?, reviewed_by=? WHERE id=?",
                  (final, manualdb.now_cn(), body.get("reviewed_by") or "", pid))
        c.commit()
        return {"ok": True, "action": row["action"], "result": r}
    finally:
        c.close()
```

### 8.3 前端面板

- `ops-tabs`（约 552–557 行）在 `admin` 按钮后加：`<button onclick="switchOpsTab('pending')">待审核</button>`。
- `switchOpsTab` 的 `tabs` 数组（1291 行）改成 `['rate-entry','space-entry','batch','manage','admin','pending']`，并加 `if (t==='pending') loadPending();`。
- 新增 `<div class="ops-panel" id="panel-pending">`：一个筛选（awaiting/passed/failed）+ 表格（来源、动作[新增/更新]、校验状态、归一对照、错误、操作[放行/驳回]）+「全库体检」按钮（调 `/api/audit`）。
- 放行/驳回按钮走 `ensureToken()`→`requireToken()`→fetch，复用现有令牌逻辑（1307/1322 行）。
- 待审核计数可在 `admin` 按钮上挂个红点（有 awaiting 时），但**按用户偏好不要加括号数字角标**，用红点即可。

---

## 9. 任何录入都做"新增 / 更新"判定

**规则（需求 9）**：`起运港 / 目的港 / 船司 / 原文` 四者完全相同 → 视为**更新**（走留痕 `revise_record`，旧行标"已被覆盖"）；否则**新增**。

**核心代码（`manualdb.py`）**：

```python
def classify_new_or_update(conn, kind: str, rec: Dict[str, Any],
                           resolved: Dict[str, str]) -> Tuple[str, Optional[str]]:
    """按 起运港+目的港+船司+原文 找当前生效行；命中→('update', content_key)，否则→('new', None)。"""
    table = TABLES[kind]["table"]
    text = str(rec.get("消息原文") or "").strip()
    row = conn.execute(
        f"""SELECT content_key FROM {table}
            WHERE pol=? AND pod=? AND carrier=? AND COALESCE(message_text,'')=? AND status=?
            ORDER BY message_time DESC, id DESC LIMIT 1""",
        (resolved.get("起运港",""), resolved.get("目的港",""), resolved.get("船司",""),
         text, DEFAULT_STATUS[kind])).fetchone()
    return ("update", row["content_key"]) if row else ("new", None)
```

**接线**：
- 待审核入库时已用（8.2）。
- 人工录入 `submitRate`/`submitSpace` 与批量 `new` 模式：`push_records` 之前先 `classify_new_or_update`；命中 update 就改走 `revise_record`，回执标"更新（留痕）"而非"新增"。这样"同港同司同原文再录一次"不会堆出重复行，而是自动留痕更新。
- 批量 `revise` 模式已有自己的定位逻辑（`btCommitRevise`，按 船司+起运港+目的港+ETD），与本判定并存：`revise` 模式是用户显式指定更新，`new` 模式叠加本自动判定兜底。

---

## 附：数据库结构标准（本次已落地，供参照）

- 价格/天数列 = **INTEGER + 非负 CHECK**；`valid_from/valid_to` = **ISO 日期 CHECK**；`origin` = **NOT NULL DEFAULT 'manual'**。
- `content_key`(SHA256 指纹) UNIQUE 幂等；留痕 `supersedes` 指回旧行、旧行 `status='已被覆盖'`；每张业务表 `supersedes` 只留一个索引。
- 新增 `pending_records` 表（第 8 节）。
- 建表 SQL 只在 `manualdb.DDL` 一处；改了 DDL 记得同步 `manual_check.py` 里测试沙箱的 `CREATE TABLE`（否则回归会因缺列/缺表报错）。

## 验收清单（flash 改完逐条自检）

1. `python validity_rule_check.py` + `python manual_check.py` 全绿。
2. 有效期区间：点开能连续选起、止两天，选完才关；快捷 7/15/30 天可用。
3. 销售端每个筛选框键入有联想弹层，点候选能填值。
4. 录入页乱填一个不存在的船司/港口 → 标红 + 提交被挡 + 明确提示。
5. 批量模式是分段按钮；目标库可勾运价/舱位/两者；两者都勾时回执分别计数、舱位缺必填被拦并报错。
6. 预检走服务端 `/api/validate`；「全库体检」能列出不达标存量行。
7. 待审核页在"账号&群聊"后；agent 传入→机械校验→passed 才能放行；放行后按新增/更新正确落库；failed 不能放行。
8. 同港同司同原文再录一次 = 更新（留痕），不产生重复行。
