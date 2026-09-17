# -*- coding: utf-8 -*-
"""把「钉钉多维 AI 表格」相关的表述从心跳 skill 与架构文档里清干净。

这些句子多数是"不再写钉钉 AI 表格"式的退役对比——机制早已删，留着措辞反而让人以为还有第二条写入链路。
改法是一律换成正向的现行口径（唯一写入口 = 台账 POST /api/pending），不删掉任何 operative 约束。

    python strip_aitable_wording.py           预演
    python strip_aitable_wording.py --apply    写盘（两份副本同步）
"""
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

PROJ = pathlib.Path(r"C:\Users\YQN\Documents\正式版运价库")
SKILL_PROJ = PROJ / "运价项目" / "Skill" / "freight-rate-heartbeat"
SKILL_INST = pathlib.Path(r"C:\Users\YQN\.qwenworkcn\skills\freight-rate-heartbeat")

# (相对 skill 根的路径, 原文, 新文)
RULES = [
    ("Instruction.md",
     "，由人工在看板点放行才真正入库。它不再写钉钉 AI 表格、也不再维护旧的本机看板。",
     "，由人工在看板点放行才真正入库。"),

    ("SKILL.md",
     "- **不再写钉钉 AI 表格，也不再镜像旧本机看板。** 唯一写入口是运价台账 `manual_server` 的 `POST /api/pending`。",
     "- **唯一写入口**是运价台账 `manual_server` 的 `POST /api/pending`。"),

    ("references/extraction-rules.md",
     "入库对象从钉钉 AI 表格换成「运价台账」`POST /api/pending`。",
     "入库对象是「运价台账」`POST /api/pending`。"),
    ("references/extraction-rules.md",
     "下文第 3 条的 `record update`/覆盖标记/字段ID 写法已废弃，见改写。",
     "下文第 3 条已按台账改写。"),
    ("references/extraction-rules.md",
     "**台账里 `航线` 填了必须命中 `routes` 主数据（可空），不像旧钉钉表会自建选项**",
     "**台账里 `航线` 填了必须命中 `routes` 主数据（可空）**"),
    ("references/extraction-rules.md",
     "- **agent 不调任何 record delete/update、也不写覆盖标记字段**（那是旧钉钉表机制，已废）。",
     "- **agent 只产新记录，不改旧行、不写任何状态回写字段**。"),

    ("references/learnings.md",
     "- 写钉钉 AI 表格（`dws aitable record create` / baseId `6LeBq…`/`mExel2…` / fieldId 映射 / "
     "singleSelect 选项比对）——本版起全部删除。\n",
     ""),
    ("references/learnings.md",
     "- `init_ledger.py`（建钉钉 base）、`check_spec.py`（比对钉钉字段选项）、`price_history.py`"
     "（查钉钉快照，历史改走 `/api/rates_grouped`）、`validate.py`（规则移交台账 `/api/validate`）、"
     "`check_v3.py`/`save_pull.py`（钉钉批次产物）——已移除。",
     "- `init_ledger.py`、`check_spec.py`、`price_history.py`（历史轨迹改走台账 `/api/rates_grouped`）、"
     "`validate.py`（规则移交台账 `/api/validate`）、`check_v3.py`/`save_pull.py` —— 已移除。"),

    ("references/runbook.md",
     "（本机 HTTP，python urllib 直连），不再写钉钉 AI 表格、不再有 fileToken/附件上传/record create/覆盖回写。",
     "（本机 HTTP，python urllib 直连），只暂存：不传附件、不回写旧行。"),

    ("scripts/config/Instruction.md",
     ' 三处对齐（旧钉钉表的第 9 档"报备"已删）。',
     " 三处对齐。"),

    ("scripts/config/ledger_contract.json",
     "注意：本 skill 不再写钉钉 AI 表格、也不再镜像本机旧看板，唯一写入口是",
     "唯一写入口是"),

    ("scripts/record_builder.py",
     "# 契约值权威真源已迁到运价台账库（GET /api/master）；本文件不再持有钉钉 fieldId 映射。",
     "# 契约值权威真源在运价台账库（GET /api/master）；本文件只做模板与推断，不持有字段映射。"),
    ("scripts/record_builder.py",
     '"""产出台账契约要求的【中文字段键】候选记录（不再映钉钉 fieldId）。"""',
     '"""产出台账契约要求的【中文字段键】候选记录。"""'),

    ("scripts/llm_output_contract.md",
     "（见 `Database/docs/agent-input-contract.md`），不再是钉钉 fieldId。",
     "（见 `Database/docs/agent-input-contract.md`）。"),
    ("scripts/llm_output_contract.md",
     "不需要 Agent 处理任何 fileToken/附件上传。",
     "不需要 Agent 处理任何附件上传。"),
]

# 架构文档单独一条（只在项目里）
DOC = (PROJ / "运价项目" / "Database" / "docs" / "architecture.md",
       "旧版把数据写进**钉钉 AI 表格**并镜像到一份**本机旧看板**；现在这两条都已退役——",
       "早期版本的投递目标已整体退役——")

roots = [("项目副本", SKILL_PROJ), ("已安装副本", SKILL_INST)]
changed, skipped = [], []

for label, root in roots:
    if not root.exists():
        skipped.append("%s：目录不存在" % label)
        continue
    for rel, old, new in RULES:
        p = root / rel
        if not p.exists():
            skipped.append("%s/%s 不存在" % (label, rel))
            continue
        s = p.read_text(encoding="utf-8")
        n = s.count(old)
        if n == 0:
            skipped.append("%s/%s 已无该表述" % (label, rel))
            continue
        if n > 1:
            raise SystemExit("%s/%s 命中 %d 次，不敢自动改" % (label, rel, n))
        changed.append((label, rel, old[:38]))
        if "--apply" in sys.argv:
            p.write_text(s.replace(old, new), encoding="utf-8")

if "--apply" in sys.argv:
    s, old, new = DOC
    t = s.read_text(encoding="utf-8")
    if t.count(old) == 1:
        s.write_text(t.replace(old, new), encoding="utf-8")
        changed.append(("架构文档", "docs/architecture.md", old[:38]))
    elif t.count(old) > 1:
        raise SystemExit("architecture.md 命中多次")

print("预演/已改 %d 处" % len(changed))
for label, rel, frag in changed:
    print("  %-12s %-38s  …%s…" % (label, rel, frag))
if skipped:
    print("\n未命中/跳过 %d 处（可能已改过或路径不同）" % len(skipped))
    for x in skipped[:12]:
        print("  -", x)

if "--apply" in sys.argv:
    # 版本号：措辞修订属 patch
    for label, root in roots:
        km = root / "SKILL.md"
        if km.exists():
            t = km.read_text(encoding="utf-8")
            if "version: 5.1.0" in t:
                km.write_text(t.replace("version: 5.1.0", "version: 5.1.1"), encoding="utf-8")
                print("\n%s SKILL.md 版本 5.1.0 → 5.1.1" % label)
    for root in (SKILL_PROJ, SKILL_INST):
        cj = root / "scripts" / "config" / "ledger_contract.json"
        if cj.exists():
            json.loads(cj.read_text(encoding="utf-8"))
            print("ledger_contract.json 仍是合法 JSON:", root.name)
    import py_compile
    rb = SKILL_PROJ / "scripts" / "record_builder.py"
    py_compile.compile(str(rb), doraise=True)
    print("record_builder.py 编译通过")
