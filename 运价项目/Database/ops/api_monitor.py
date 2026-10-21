"""Persistence and administrator reporting for API request metadata."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.api_monitor_contracts import API_EVENT_TABLE
from core.contracts import ok
from services.api_monitor import monitor_query

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def record_request(connection, request_id, method, path, status_code, duration_ms,
                   user_id=None, role="", client_ip="", created_at=None):
    if not str(path or "").startswith("/api/"):
        return False
    connection.execute(
        f"INSERT INTO {API_EVENT_TABLE}(request_id,created_at,method,path,status_code,duration_ms,user_id,role,client_ip) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
        (str(request_id)[:96], created_at or datetime.now(BUSINESS_TIMEZONE).isoformat(timespec="seconds"),
         str(method).upper()[:8], str(path)[:160], int(status_code), max(0, int(duration_ms)),
         str(user_id)[:120] if user_id else None, str(role or "")[:24], str(client_ip or "")[:64]),
    )
    connection.commit()
    return True


def _where(options, since):
    clauses = ["substr(created_at,1,10)>=?"]
    args = [since]
    if options["method"]:
        clauses.append("method=?")
        args.append(options["method"])
    if options["path"]:
        clauses.append("path=?")
        args.append(options["path"])
    if options["user_id"]:
        clauses.append("user_id=?")
        args.append(options["user_id"])
    if options["failures_only"]:
        clauses.append("status_code>=400")
    return " WHERE " + " AND ".join(clauses), args


def events(connection, query):
    options = monitor_query(query)
    today = datetime.now(BUSINESS_TIMEZONE).date()
    since = (today - timedelta(days=options["days"] - 1)).isoformat()
    where, args = _where(options, since)
    total = connection.execute(f"SELECT COUNT(*) FROM {API_EVENT_TABLE}" + where, args).fetchone()[0]
    rows = [dict(row) for row in connection.execute(
        "SELECT e.request_id,e.created_at,e.method,e.path,e.status_code,e.duration_ms,e.user_id,e.role,e.client_ip,"
        "COALESCE(o.display_name,o.name,e.user_id,'未登录') AS display_name "
        f"FROM {API_EVENT_TABLE} e LEFT JOIN operators o ON o.user_id=e.user_id" +
        where.replace("created_at", "e.created_at").replace("method", "e.method").replace("path", "e.path")
              .replace("user_id", "e.user_id").replace("status_code", "e.status_code") +
        " ORDER BY e.created_at DESC,e.request_id LIMIT ? OFFSET ?",
        [*args, options["limit"], options["offset"]])]
    return ok({"rows": rows, "total": total, **options})


def summary(connection, query):
    options = monitor_query(query)
    today = datetime.now(BUSINESS_TIMEZONE).date()
    since = (today - timedelta(days=options["days"] - 1)).isoformat()
    where, args = _where(options, since)
    rows = connection.execute(
        f"SELECT path,status_code,duration_ms,user_id FROM {API_EVENT_TABLE}" + where, args).fetchall()
    routes = {}
    users = set()
    failures = 0
    duration_total = 0
    for row in rows:
        item = routes.setdefault(row["path"], {"path": row["path"], "requests": 0, "failures": 0, "duration_ms": 0})
        item["requests"] += 1
        item["duration_ms"] += int(row["duration_ms"])
        duration_total += int(row["duration_ms"])
        failures += int(row["status_code"] >= 400)
        item["failures"] += int(row["status_code"] >= 400)
        if row["user_id"]:
            users.add(row["user_id"])
    ranked = sorted(routes.values(), key=lambda item: (-item["requests"], item["path"]))[:12]
    for item in ranked:
        item["average_ms"] = round(item["duration_ms"] / item["requests"]) if item["requests"] else 0
        del item["duration_ms"]
    return ok({"days": options["days"], "requests": len(rows), "failures": failures,
               "users": len(users), "average_ms": round(duration_total / len(rows)) if rows else 0,
               "routes": ranked})
