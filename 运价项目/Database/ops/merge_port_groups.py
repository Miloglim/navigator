"""Checkpointed merge of identical port groups; never rewrites quote evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

DATABASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DATABASE))
import manualdb
import postgresdb
from services.master_resolution import alias_list, validate_group_names
MERGE_LOCK_TIMEOUT_SECONDS = 5


def merge(connection, canonical, sources, apply=False):
    if not canonical.strip() or not sources or canonical in sources:
        raise ValueError("canonical must differ from source group names")
    if apply:
        if getattr(connection, "engine", "") == "postgres":
            connection.execute("LOCK TABLE port_groups,alias_scope,chat_groups IN SHARE ROW EXCLUSIVE MODE")
        elif not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
    groups = [dict(row) for row in connection.execute("SELECT * FROM port_groups ORDER BY name")]
    by_name = {row["name"]:row for row in groups}
    if any(name not in by_name for name in sources):
        raise ValueError("source group missing; refusing partial merge")
    rows = [by_name[name] for name in sources]
    baseline = rows[0]
    def meaning(row):
        return (row.get("kind") or "pod", frozenset(alias_list(row.get("members"))),
                frozenset(alias_list(row.get("specials"))))
    if any(meaning(row) != meaning(baseline) for row in rows):
        raise ValueError("member/special/direction differs; these are not duplicate groups")
    existing = by_name.get(canonical)
    if existing and meaning(existing) != meaning(baseline):
        raise ValueError("canonical target already has a different scope")
    aliases = []
    for row in rows + ([existing] if existing else []):
        aliases += [row["name"], row.get("label") or ""] + alias_list(row.get("aliases"))
    aliases = [value for value in dict.fromkeys(aliases) if value and value != canonical]
    proposed = dict(baseline, name=canonical, aliases=",".join(aliases), active=1)
    proposed["note"] = "Merged identical groups: " + ", ".join(sources) + " | " + str(baseline.get("note") or "")
    expected_groups = [dict(row,active=0) if row["name"] in sources else row for row in groups]
    ports = [dict(row) for row in connection.execute("SELECT name,active FROM ports")]
    validate_group_names(proposed, expected_groups, ports)
    known_ports = {row["name"] for row in ports if row["active"]}
    if not set(alias_list(proposed.get("members"))) <= known_ports:
        raise ValueError("group contains noncanonical or inactive port members")
    scopes = [dict(row) for row in connection.execute("SELECT * FROM alias_scope ORDER BY alias,kind,scope_group")]
    defaults = [dict(row) for row in connection.execute("SELECT name,default_pol FROM chat_groups ORDER BY name")]
    scopes_to_update = [row for row in scopes if row["kind"] in ("pol","pod") and row["target"] in sources]
    defaults_to_update = [row for row in defaults if row["default_pol"] in sources]
    snapshot = {"port_groups":groups, "alias_scope":scopes, "chat_group_defaults":defaults}
    plan = {"canonical":canonical, "sources":sources, "group":proposed,
            "scoped_targets":len(scopes_to_update), "defaults":len(defaults_to_update)}
    if apply:
        for name in sources:
            connection.execute("UPDATE port_groups SET active=0 WHERE name=?", (name,))
        columns = ("name","label","aliases","members","specials","kind","active","note","created_at")
        if existing:
            connection.execute("UPDATE port_groups SET " + ",".join(col+"=?" for col in columns if col != "name")
                               + " WHERE name=?", [proposed.get(col) for col in columns if col != "name"] + [canonical])
        else:
            proposed["created_at"] = manualdb.now_cn()
            connection.execute("INSERT INTO port_groups("+",".join(columns)+") VALUES("+",".join("?" for _ in columns)+")",
                               [proposed.get(col) for col in columns])
        for row in scopes_to_update:
            connection.execute("UPDATE alias_scope SET target=? WHERE alias=? AND kind=? AND scope_group=?",
                               (canonical,row["alias"],row["kind"],row["scope_group"]))
        for row in defaults_to_update:
            connection.execute("UPDATE chat_groups SET default_pol=? WHERE name=?", (canonical,row["name"]))
    return {"ok":True,"data":{"plan":plan,"before":snapshot,"applied":apply}}


def business_digest(connection):
    # Database identity and source evidence must remain byte-for-byte intact.
    out = {}
    for table in ("freight_items","pending_records"):
        rows = [dict(row) for row in connection.execute("SELECT * FROM "+table+" ORDER BY id")]
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        out[table] = {"rows":len(rows),"sha256":hashlib.sha256(encoded).hexdigest()}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical",required=True)
    parser.add_argument("--source",action="append",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--apply",action="store_true")
    args = parser.parse_args()
    runtime = json.loads((DATABASE/"ops/runtime.json").read_text(encoding="utf-8-sig"))
    if runtime.get("backend") != "postgres":
        raise RuntimeError("configured runtime is not PostgreSQL")
    args.output.mkdir(parents=True,exist_ok=True)
    backup_result = None
    if args.apply:
        if (args.output/"before.json").exists():
            raise RuntimeError("checkpoint already exists; choose a new output for each apply")
        from ops.postgres_backup import backup
        backup_result = backup(postgresdb.DEFAULT_ENV_FILE)
    connection = postgresdb.connect(postgresdb.DEFAULT_ENV_FILE)
    try:
        if args.apply:
            connection.execute(f"SET LOCAL lock_timeout = '{MERGE_LOCK_TIMEOUT_SECONDS}s'")
            connection.execute("ALTER TABLE port_groups ADD COLUMN IF NOT EXISTS aliases TEXT")
            # Serialize with writers only for the transaction, after backup completes.
            connection.execute("LOCK TABLE pending_records,freight_items IN SHARE MODE")
        before_business = business_digest(connection)
        result = merge(connection,args.canonical,args.source,args.apply)
        if args.apply:
            (args.output/"before.json").write_text(json.dumps(result["data"]["before"],ensure_ascii=False,indent=2),encoding="utf-8")
            after_business = business_digest(connection)
            if before_business != after_business:
                raise RuntimeError("business rows changed during master merge; rolling back")
            result["data"]["business_unchanged"] = True
            result["data"]["business_digest"] = after_business
            result["data"]["database_backup"] = backup_result
            connection.commit()
            manualdb.invalidate_master()
        else:
            connection.rollback()
        result["data"].pop("before")
        (args.output/("receipt.json" if args.apply else "preview.json")).write_text(
            json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
