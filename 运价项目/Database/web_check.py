# -*- coding: utf-8 -*-
"""局域网版前端结构体检：括号/引号平衡 + 关键功能在位断言。

bash 里内联 Python 会被转义搞坏（反斜杠、中文路径都是坑），所以固化成文件。
    python web_check.py
"""
import sys
from pathlib import Path
from js_check import InlineScripts

sys.stdout.reconfigure(encoding='utf-8')

HERE = Path(__file__).resolve().parent
F = HERE / "web" / "index.html"
BS = "\\"
NL = "\n"
FAILS = []


def strip_js(js):
    """去掉注释与字符串内容，只留结构字符，便于配对检查（含模板字面量与正则字面量）。"""
    out, i, n, errs = [], 0, len(js), []
    RQP = set("=(,:[!&|?{};")

    def looks_regex(buf):
        for ch in reversed(buf):
            if ch in " \t\r\n":
                continue
            return ch in RQP
        return True

    while i < n:
        c = js[i]
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            j = js.find(NL, i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and looks_regex(out):
            j, in_class = i + 1, False
            while j < n:
                if js[j] == BS:
                    j += 2
                    continue
                if js[j] == "[":
                    in_class = True
                elif js[j] == "]":
                    in_class = False
                elif js[j] == "/" and not in_class:
                    break
                elif js[j] == NL:
                    break
                j += 1
            j += 1
            while j < n and js[j].isalpha():
                j += 1
            i = j
            out.append(" ")
            continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if js[j] == BS:
                    j += 2
                    continue
                if js[j] == c:
                    break
                if js[j] == NL:
                    errs.append("字符串未闭合 @ 第 %d 行" % js[:i].count(NL))
                    break
                j += 1
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n:
                if js[j] == BS:
                    j += 2
                    continue
                if js[j] == "$" and j + 1 < n and js[j + 1] == "{":
                    d, j = 1, j + 2
                    while j < n and d:
                        if js[j] == "{":
                            d += 1
                        elif js[j] == "}":
                            d -= 1
                        j += 1
                    continue
                if js[j] == "`":
                    break
                j += 1
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), errs


def ck(name, ok, detail=""):
    print(("  OK   " if ok else "  FAIL ") + name + ("  | " + str(detail)[:110] if detail else ""))
    if not ok:
        FAILS.append(name)


src = F.read_text(encoding="utf-8")
scripts = InlineScripts()
scripts.feed(src)
js = '\n'.join(''.join(script['parts']) for script in scripts.scripts)
code, errs = strip_js(js)
stack, pairs = [], {"(": ")", "[": "]", "{": "}"}
close = {v: k for k, v in pairs.items()}
for ch in code:
    if ch in pairs:
        stack.append(ch)
    elif ch in close:
        if not stack or stack[-1] != close[ch]:
            errs.append("括号不匹配 " + ch)
        else:
            stack.pop()
ck("括号/引号平衡", not errs and not stack, (errs[:3], stack[:3]))

for probe, desc in [
    ("function gpCell", "群名显示为可悬停查看成员的样式"),
    ("function acGroups", "录入端候选含港群"),
    ("function renderGroupHint", "筛选栏下方群快捷筛选"),
    ("function btParse", "批量导入解析器"),
    ("function openRangePicker", "有效期区间选择器"),
    ("function hardNormalizePort", "录入归一（港口硬匹配命中主数据）"),
    ("data-ac=", "自定义候选列表输入框"),
    ("port_groups", "后台港群维护"),
    ("/api/local-token", "本机免口令"),
]:
    ck(desc, probe in src)
# 原断言 gPod.concat(pod.sort()) 随旧筛选实现一起下线，换成现架构下等价的两条在位断言
ck("枚举下拉以库里真实值为准（fieldOptions 合并 rates+space）",
   "function fieldOptions" in src and "new Set([...((o.rates || {})[col] || []), ...((o.space || {})[col] || [])])" in src)
ck("销售端筛选框走统一联想组件且不列港群",
   'data-nogroup="1"' in src and "dataset.nogroup" in src)
ck("销售详情/历史保持固定文案，展开只更新状态",
   'class="sales-disclosure" data-did=' in src and 'aria-expanded="false"' in src
   and "function setSalesDetailOpen" in src and "btn.textContent = next==='1'" not in src)
ck("销售详情锁定列表初始列宽且长文本只在详情内换行",
   "function salesLockColumns()" in src and "table.prepend(group);" in src
   and "table.style.tableLayout = 'fixed';" in src
   and "window.addEventListener('resize', salesLockColumns);" in src
   and "if (r === 'sales') salesLockColumns();" in src
   and src.count("  salesLockColumns();") >= 2
   and "#view-sales .sales-rate-table .drow > td," in src
   and ".sales-space-table .sales-flat-detail > td { max-width:0; overflow-wrap:anywhere; }" in src)
ck("销售舱位详情局部插入，不重画结果表",
   "row.insertAdjacentHTML('afterend', salesFlatDetailHtml(record))" in src
   and "function toggleRow(key) { S.sales.openKey" not in src)
ck("价舱管理详情局部展开，不重画管理表",
   "row.insertAdjacentHTML('afterend', mgDetailRowHtml(record, mgKind === 'rates'))" in src
   and "function mgToggleRow(key) { mgOpenKey" not in src)
ck("价舱管理卡片长文本换行且来源与操作分层",
   ".rcard .lane { font: 600 15px/1.3 var(--font-display); color: var(--navy); white-space:normal; overflow-wrap:anywhere; }" in src
   and ".rcard .raw {" in src and "white-space:pre-wrap; overflow-wrap:anywhere;" in src
   and ".rcard .foot { display:flex; min-width:0; flex-direction:column;" in src
   and ".rcard .acts { display:flex; flex-wrap:wrap; justify-content:flex-end;" in src)
ck("航管待审表保留表头行列，按容器宽度整表缩放",
   ".pd-entry-table thead, .pd-group .pd-gtable thead, .pd-group .pd-gtable colgroup { display:none; }" not in src
   and "function pdFitTables()" in src and "table.style.zoom" in src
   and ".pd-entry-row, .pd-group .pd-gtable tbody > tr:not(.pd-gedit) {" not in src
   and "<td colspan=\"10\" style=\"padding:0\"" in src)
ck("运价和舱位身份码只在详情显示并保留完整复制值",
   "const identity = '<div class=\"pd-detail-id\">" in src
   and "idChip(idCode(r))" not in src[src.index('function pdGroupCellsHtml'):src.index('function pdEntriesHtml')]
   and "(isRates && r.entry_id ? ' · '" not in src
   and 'data-code="' in src and 'title="身份码 ' in src)
ck("组码只在同源组主标题显示，条目详情只放条目码",
   src.count("(p['消息ID'] ? idChip('组:' + p['消息ID']) : '')") == 2
   and "idChip('组:'" not in src[src.index('function pdCardHtml'):src.index('function pdSourceGroupHtml')]
   and "const identity = '<div class=\"pd-detail-id\"><span>条目 ID</span>'" in src)
ck("航管列表不显示航线服务注释，字段仍保留供详情编辑",
   "服务：" not in src[src.index('function pdGroupCellsHtml'):src.index('function pdEditRow')]
   and "{key:'service_name', label:'航线服务', req:false}" in src
   and "const service = p['航线服务'] || ''" in src)
ck("销售详情图片自适应且可点击预览，窄屏结果不横滚",
   'class="sales-detail-images"' in src
   and "imagesBlock(r.images,120)" in src
   and "imagesBlock(q.images,200)" in src
   and "imagesBlock(r.images,120,false)" not in src
   and "imagesBlock(q.images,200,false)" not in src
   and "(openPreview ? ' onclick=\"openLightbox(this.src,this.dataset.name)\"' : '')" in src
   and ".sales-rate-table tr.qrow" in src and ".sales-space-table tr.sales-result-row" in src
   and "@media (max-width: 1100px) {\n  #view-sales #table-container { overflow-x: hidden !important; }" in src)
ck("销售有效期按起止上下两行展示，其他摘要字段仍保持单行",
   "function salesValidityHtml" in src
   and "class=\"sales-validity\"" in src
   and "<span>起 '" in src and "<span>止 '" in src
   and "${salesValidityHtml(r)}" in src
   and "#view-sales .sales-validity{display:inline-flex;flex-direction:column;" in src)
ck("销售入库时间去年份并按日期时间上下两行展示",
   "function salesEntryTimeHtml" in src
   and "class=\"sales-entry-stamp\"" in src
   and "salesEntryTimeHtml(q.synced_at)" in src
   and "salesEntryTimeHtml(r.synced_at)" in src
   and "#view-sales .sales-entry-stamp{display:inline-flex;flex-direction:column;" in src)

# 队列批量同步的三个易退化点：入口在位、保存后就地刷行（否则改完显示还是旧值）、有效期成对
ck("待审卡片有「同步到队列」入口与面板容器",
   "pdSyncToggle(" in src and 'id="pd-sync-' in src)
ck("编辑保存后就地刷新该条目行与本批计数（不再等整表重画）",
   "function pdRefreshRowDisplay" in src and "pdRefreshRowDisplay(row);" in src
   and "function pdGroupCellsHtml" in src)
ck("有效期按「起|止」成对同步（一个控件两个键，绝不半刷成起晚于止）",
   "SYNC_PAIR" in src and "'有效期': ['有效期起', '有效期止']" in src)
# 未过原因必须直显（截断 + 悬停 tooltip），不能只留"未过"两个字逼人逐条点开
ck("待审核「校验」列直显未过原因（.pd-why 截断 + title 全文 + 多条＋N项角标）",
   'class="pd-why" title="' in src and ".pd-why {" in src
   and "text-overflow:ellipsis" in src and "pd-why-n" in src)
# 强匹配字段：打不到主数据的值不许发出去覆盖原值；同步候选也只能取服务端已认下的值
ck("自动保存先剔除打不到主数据的强匹配值（手打半个词不覆盖原值）",
   "function pdSplitStrict" in src and "pdSplitStrict(raw)" in src and "function cnResolves" in src
   and "r.rejected" in src)
ck("同步候选只取已存 payload（被退回的值不会被批量刷进整条队列）",
   "const cur = (PD.byid[id] || {}).payload || {};" in src)
# 组码只在编辑详情正文显示，复制走 data-code + 文档级委托。
ck("身份码芯片在位（统一 entry_id/组码，data-code 委托复制）",
   "function idChip" in src and 'class="id-chip"' in src and "function copyCode" in src
   and 'data-code="' in src and "idChip(idCode(r))" in src
   and "entry_id" in src and "idChip('组:'" in src)

# 括号平衡放过过一次"注释写坏"（整段脚本不执行、所有页签点不动），所以必须再做一次真语法解析
import subprocess
proc = subprocess.run([sys.executable, str(HERE / "js_check.py")], capture_output=True,
                      text=True, encoding="utf-8", errors="replace")
ck("JS 语法解析通过（js_check）", proc.returncode == 0,
   ((proc.stdout or "") + (proc.stderr or "")).strip().replace("\n", " ⏎ ")[:170])

print("\n结果:", "全部通过" if not FAILS else "失败 %d 项 -> %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
