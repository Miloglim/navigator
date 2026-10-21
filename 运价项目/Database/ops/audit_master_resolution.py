"""Read-only rollout gate: names, members, scoped targets and group documents."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

DATABASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DATABASE))
import manual_server
import manualdb
from services.master_resolution import alias_list, fold_name
from core.master_resolution_contracts import MASTER_RESOLVE_TYPES


def audit(master, rules_root, group_map=None):
    group_map = group_map or {}
    probes = set()
    for side, rows, primary, columns in (
        ("pod", master.ports, "name", ("name_cn", "name_en", "city")),
        ("pol", master.ports, "name", ("name_cn", "name_en", "city")),
        ("carrier", master.carriers, "code", ("name_cn", "name_en")),
        ("route", master.routes, "name", ()),
    ):
        for row in rows:
            for name in [row[primary]] + [row.get(col) for col in columns] + alias_list(row.get("aliases")):
                if name:
                    probes.add((side, name, ""))
    for row in master.group_rows:
        for side in ("pol", "pod"):
            for name in (row["name"], row.get("label"), re.split(r"[（(]", str(row.get("label") or ""))[0],
                         *alias_list(row.get("aliases"))):
                if name:
                    probes.add((side, name, ""))
    for row in master.scope_rows:
        if row["kind"] in MASTER_RESOLVE_TYPES:
            probes.add((row["kind"], row["alias"], row.get("scope_group") or ""))
    differences, uncertain = [], []
    for side, value, group in sorted(probes):
        new = master.name_resolver.resolve(side, value, group)
        if side in ("pol", "pod"):
            old = master.group_for(value, side)
            if not old:
                hit, known = master.lookup_port(value, side, group)
                old = hit if known else None
        else:
            index = master.carrier_idx if side == "carrier" else master.route_idx
            old = master.scoped(side, value, group) or index.get(fold_name(value)) or index.get(fold_name(value.split(",")[0]))
        row = {"type":side, "input":value, "source_group":group, "legacy":old, "unified":new}
        if old != new["canonical"]:
            differences.append(row)
        if new["status"] in ("ambiguous", "invalid_target"):
            uncertain.append(row)
    member_issues = []
    canonical_ports = {port["name"] for port in master.ports}
    for group in master.group_rows:
        for member in alias_list(group.get("members")):
            if member not in canonical_ports:
                member_issues.append({"group":group["name"], "member":member,
                                      "resolution":master.name_resolver.resolve("pod",member)})
    inventory = []
    # Only explicit mapping tables are machine-readable. Prose remains a review item.
    for path in sorted(rules_root.rglob("*.md")):
        headers = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.startswith("|"):
                headers = []
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if "写法" in cells and "归一目标" in cells:
                headers = cells
                continue
            if not headers or len(cells) != len(headers) or all(re.fullmatch(r"[: -]+",cell) for cell in cells):
                continue
            table = dict(zip(headers, cells))
            scope = table.get("作用域", "")
            side = {"起运港":"pol", "目的港":"pod", "船司":"carrier", "航线":"route"}.get(scope)
            names = re.findall(r"`([^`]+)`", table["写法"])
            targets = re.findall(r"`([^`]+)`", table["归一目标"])
            group = group_map.get(path.parent.name, path.parent.name)
            business_target = any(marker in table["归一目标"] for marker in (
                "加一条", "各自", "对应规范名", "不是港口", "每行存", "剥掉"))
            if side is None or not names or len(targets) != 1 or business_target:
                inventory.append({"file":str(path.relative_to(rules_root)), "line":line_number,
                                  "classification":"review", "reason":"compound or business-dependent mapping", "table":table})
                continue
            for name in names:
                target = targets[0]
                result = master.name_resolver.resolve(side, name, group)
                target_exists = (side,target) in master.name_resolver.targets
                classification = "redundant" if result["canonical"] == target else (
                    "migration_candidate" if result["status"] == "unknown" and target_exists else "conflict")
                if group.startswith("_") or group not in master.group_pol:
                    classification = "review"  # Shared documents have no single source-group scope.
                inventory.append({"file":str(path.relative_to(rules_root)), "line":line_number,
                                  "source_group":group, "type":side, "alias":name, "target":target,
                                  "classification":classification, "resolution":result})
    return {"ok":True, "data":{"stage":"shadow", "probes":len(probes), "differences":differences,
            "uncertain":uncertain, "member_issues":member_issues, "document_inventory":inventory,
            "document_counts":dict(Counter(row["classification"] for row in inventory)),
            "cutover_ready":bool(probes) and not differences and not member_issues
                and not any(row["classification"] in ("conflict", "migration_candidate", "review") for row in inventory)}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("shadow", "port_groups"), default="shadow")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = json.loads((DATABASE / "ops/runtime.json").read_text(encoding="utf-8-sig"))
    if runtime.get("backend") != "postgres":
        raise RuntimeError("This rollout audit requires the configured PostgreSQL runtime")
    connection = manual_server.postgresdb.connect(manual_server.postgresdb.DEFAULT_ENV_FILE)
    try:
        # No schema initialization or business writes. Read a consistent master snapshot.
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        master = manualdb.Master(connection)
        if not master.ports:
            raise RuntimeError("Master snapshot is empty; refusing to report a successful rollout gate")
        snapshot = {"ports":master.ports, "carriers":master.carriers, "routes":master.routes,
                    "port_groups":master.group_rows, "alias_scope":master.scope_rows}
        skill_root = DATABASE.parent / "Skill/freight-rate-heartbeat"
        sources = json.loads((skill_root / "scripts/config/chat_sources.json").read_text(encoding="utf-8-sig"))
        group_map = {Path(row["spec_dir"]).name:row["name"] for row in sources["groups"] if row.get("spec_dir")}
        result = audit(master, skill_root / "references/群聊规范", group_map)
        result["data"]["stage"] = args.stage
        pending_differences, pending_count = [], 0
        for row in connection.execute("SELECT id,entry_id,kind,payload FROM pending_records WHERE review_status='awaiting' ORDER BY id"):
            rec = json.loads(row["payload"])
            resolved, errors = manualdb.resolve_core(master, row["kind"], rec)
            shadow = manualdb.name_resolution_shadow(master, rec, resolved, errors)
            pending_count += 1
            if not shadow["equal"]:
                pending_differences.append({"id":row["id"], "entry_id":row["entry_id"],
                                            "source_group":rec.get("来源群"), "comparison":shadow})
        result["data"]["pending_count"] = pending_count
        result["data"]["pending_differences"] = pending_differences
        result["data"]["cutover_ready"] = result["data"]["cutover_ready"] and not pending_differences
    finally:
        connection.rollback()
        connection.close()
    snapshot_path = args.output / "master-snapshot.json"
    if not snapshot_path.exists() or not json.loads(snapshot_path.read_text(encoding="utf-8")).get("ports"):
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    data = result["data"]
    lines = ["# 名称统一对照检查", "", f"检查名称：{data['probes']}；差异：{len(data['differences'])}；冲突或失效目标：{len(data['uncertain'])}；非规范港群成员：{len(data['member_issues'])}。", "",
             f"全量名称切换门槛：{'通过' if data['cutover_ready'] else '未通过'}。本检查工具只读，不修改任何校验结论、报价规则、群文件或业务数据。当前阶段：{data['stage']}。", "",
             "| 类型 | 名称 | 来源群 | 旧结果 | 新结果 | 状态 |", "|---|---|---|---|---|---|"]
    def cell(value):
        return str(value or "—").replace("|", "\\|").replace("\n", " ")
    for row in data["differences"]:
        lines.append("| " + " | ".join(cell(v) for v in (row["type"],row["input"],row["source_group"],row["legacy"],row["unified"]["canonical"],row["unified"]["status"])) + " |")
    lines += ["", "## 无法确定的名称", "", "新旧均拒绝的歧义是正确行为；它本身不要求删除主数据。失效目标或新旧结果不同需要核对。", "", "| 类型 | 名称 | 来源群 | 状态 | 候选 |", "|---|---|---|---|---|"]
    for row in data["uncertain"]:
        candidates = ", ".join(value["canonical"] for value in row["unified"].get("candidates",[]))
        lines.append("| " + " | ".join(cell(v) for v in (row["type"],row["input"],row["source_group"],row["unified"]["status"],candidates)) + " |")
    lines += ["", "## 当前待审核对照", "", f"共{data['pending_count']}条，{len(data['pending_differences'])}条有名称差异；不改变任何待审核状态。", "", "| 条目编号 | 来源群 | 字段 | 原输入 | 旧结果 | 新结果 |", "|---|---|---|---|---|---|"]
    for row in data["pending_differences"]:
        for field in row["comparison"]["fields"]:
            if not field["equal"]:
                matches = ", ".join(value["canonical"] or value["status"] for value in field["matches"])
                lines.append("| " + " | ".join(cell(v) for v in (row["entry_id"],row["source_group"],field["field"],field["input"],field["legacy"],matches)) + " |")
    lines += ["", "## 群文件迁移清单", "", "| 文件:行 | 分类 | 写法 | 目标 |", "|---|---|---|---|"]
    for row in data["document_inventory"]:
        lines.append("| " + " | ".join(cell(v) for v in (f"{row['file']}:{row['line']}",row["classification"],row.get("alias",row.get("table",{}).get("写法")),row.get("target",row.get("table",{}).get("归一目标")))) + " |")
    (args.output / "检查结果.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    checkpoint = args.output / "checkpoint"
    if checkpoint.exists():
        manifest = {str(path.relative_to(checkpoint)):hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in checkpoint.rglob("*") if path.is_file()}
        (args.output / "checkpoint-hashes.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    print(json.dumps({"ok":True,"probes":data["probes"],"differences":len(data["differences"]),
                      "uncertain":len(data["uncertain"]),"member_issues":len(data["member_issues"]),
                      "documents":data["document_counts"],"cutover_ready":data["cutover_ready"]}))


if __name__ == "__main__":
    main()
