"""Published-item feedback storage; never writes freight_items."""
import json
import uuid

from core.contracts import ACCOUNT_AVATAR_ALL_IDS, ACCOUNT_AVATAR_PREF, ADMIN_ROLES
from core.feedback_contracts import FEEDBACK_FIELDS, FEEDBACK_TABLE
from services.feedback_validation import normalize_feedback

SQLITE_DDL = """CREATE TABLE IF NOT EXISTS item_feedback (
    id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, entry_id TEXT NOT NULL,
    field TEXT NOT NULL, old_value TEXT NOT NULL, suggested_value TEXT NOT NULL,
    author_user_id TEXT NOT NULL, author_name TEXT NOT NULL, note TEXT NOT NULL,
    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    resolved_at TEXT, resolved_by TEXT,
    CHECK (status IN ('active','withdrawn','rejected','stale'))
)"""
POSTGRES_DDL = SQLITE_DDL
ACTIVE_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS idx_item_feedback_active ON item_feedback(entry_id,field) WHERE status='active'"
ENTRY_INDEX = "CREATE INDEX IF NOT EXISTS idx_item_feedback_entry ON item_feedback(entry_id,created_at)"
QUERY_BATCH_SIZE = 400


def _as_text(value):
    return "" if value is None else str(value)


def _row(row):
    return dict(row)


def list_for_entries(conn, entry_ids):
    ids = list(dict.fromkeys(value for value in entry_ids if value))
    if not ids:
        return {}
    result = {entry_id: [] for entry_id in ids}
    for start in range(0, len(ids), QUERY_BATCH_SIZE):
        batch = ids[start:start + QUERY_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT id,submission_id,entry_id,field,old_value,suggested_value,author_user_id,author_name,note,created_at "
            f"FROM {FEEDBACK_TABLE} WHERE status='active' AND entry_id IN ({placeholders}) ORDER BY created_at,id", batch).fetchall()
        if not rows:
            continue
        author_ids = list(dict.fromkeys(row["author_user_id"] for row in rows))
        avatars = {}
        for author_start in range(0, len(author_ids), QUERY_BATCH_SIZE):
            author_batch = author_ids[author_start:author_start + QUERY_BATCH_SIZE]
            author_rows = conn.execute(
                f"SELECT user_id,preferences_json FROM operators WHERE user_id IN ({','.join('?' for _ in author_batch)})",
                author_batch).fetchall()
            for author in author_rows:
                try:
                    preferences = json.loads(author["preferences_json"] or "{}")
                except (TypeError, ValueError):
                    preferences = {}
                avatar_id = preferences.get(ACCOUNT_AVATAR_PREF) if isinstance(preferences, dict) else None
                avatars[author["user_id"]] = avatar_id if avatar_id in ACCOUNT_AVATAR_ALL_IDS else None
        active_ids = list(dict.fromkeys(row["entry_id"] for row in rows))
        current = {row["entry_id"]: row for row in conn.execute(
            f"SELECT * FROM freight_items WHERE entry_id IN ({','.join('?' for _ in active_ids)})", active_ids).fetchall()}
        for row in rows:
            item = current.get(row["entry_id"])
            view = _row(row)
            view["stale"] = not item or _as_text(item[row["field"]]) != row["old_value"]
            view["author_avatar_id"] = avatars.get(row["author_user_id"])
            result[row["entry_id"]].append(view)
    return result


def read(conn, entry_id):
    item = conn.execute("SELECT * FROM freight_items WHERE entry_id=? AND record_type IN ('rates','space')", (entry_id,)).fetchone()
    if not item:
        raise ValueError("条目不存在")
    return {"item": {field: item[field] for field in FEEDBACK_FIELDS} | {"entry_id": entry_id, "record_type": item["record_type"]},
            "feedback": list_for_entries(conn, [entry_id]).get(entry_id, [])}


def create(conn, entry_id, changes, expected, note, actor_id, actor_name, now):
    changes, note = normalize_feedback(changes, expected, note)
    if not isinstance(entry_id, str) or not entry_id.startswith("frt_") or not actor_id:
        raise ValueError("条目或账号无效")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if getattr(conn, "engine", "") == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("item-feedback:" + entry_id,))
        item = conn.execute("SELECT * FROM freight_items WHERE entry_id=? AND record_type IN ('rates','space')", (entry_id,)).fetchone()
        if not item:
            raise ValueError("条目不存在")
        existing = list_for_entries(conn, [entry_id]).get(entry_id, [])
        for row in existing:
            if row["stale"]:
                conn.execute("UPDATE item_feedback SET status='stale',resolved_at=?,resolved_by='system' WHERE id=?",
                             (now, row["id"]))
        occupied = {row["field"] for row in existing if not row["stale"]}
        if occupied.intersection(changes):
            raise ValueError("已有用户反馈这个字段，请先查看或撤下原建议")
        for field, value in changes.items():
            if _as_text(item[field]) != expected[field]:
                raise ValueError("条目已更新，请刷新后重新提交")
            if value == expected[field]:
                raise ValueError("建议值不能与原值相同")
        submission_id = "fb_" + uuid.uuid4().hex
        for field, value in changes.items():
            conn.execute("INSERT INTO item_feedback(id,submission_id,entry_id,field,old_value,suggested_value,"
                         "author_user_id,author_name,note,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,'active')",
                         ("fbi_" + uuid.uuid4().hex, submission_id, entry_id, field, expected[field],
                          value, actor_id, actor_name, note, now))
        conn.commit()
        return {"submission_id": submission_id, "entry_id": entry_id, "fields": list(changes)}
    except Exception:
        conn.rollback()
        raise


def dismiss(conn, submission_id, action, actor_id, role, now):
    if action not in ("withdraw", "reject") or not isinstance(submission_id, str) or not submission_id.startswith("fb_"):
        raise ValueError("无效的反馈操作")
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT author_user_id FROM item_feedback WHERE submission_id=? AND status='active'", (submission_id,)).fetchall()
        if not rows:
            raise ValueError("反馈已处理或不存在")
        owner = all(row["author_user_id"] == actor_id for row in rows)
        if action == "withdraw" and not owner and role not in ("operator", *ADMIN_ROLES):
            raise PermissionError("无权撤下此反馈")
        if action == "reject" and role not in ("operator", *ADMIN_ROLES):
            raise PermissionError("仅航管或管理员可驳回")
        status = "withdrawn" if action == "withdraw" else "rejected"
        conn.execute("UPDATE item_feedback SET status=?,resolved_at=?,resolved_by=? WHERE submission_id=? AND status='active'",
                     (status, now, actor_id, submission_id))
        conn.commit()
        return {"submission_id": submission_id, "status": status}
    except Exception:
        conn.rollback()
        raise
