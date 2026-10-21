"""Persistence and administrator reporting for authenticated activity events."""
import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.activity_contracts import PAGE_OPEN_EVENT, SALES_SEARCH_EVENT
from core.contracts import ok
from services.user_activity import (
    analytics_query,
    decode_properties,
    has_business_search,
    page_open_payload,
    quote_usage_payload,
    search_properties,
)


BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _json_object_function(connection):
    return "json_build_object" if getattr(connection, "engine", "sqlite") == "postgres" else "json_object"


def _json_object_cast(connection):
    return "::text" if getattr(connection, "engine", "sqlite") == "postgres" else ""


def _stamp() -> str:
    return datetime.now(BUSINESS_TIMEZONE).isoformat(timespec="seconds")


def _insert(connection, user_id, role, event_type, properties, result_count,
            client_ip, client_event_id=None, stamp=None):
    stamp = stamp or _stamp()
    event_id = "act_" + uuid.uuid4().hex
    inserted = connection.execute(
        "INSERT INTO user_activity_events(event_id,user_id,role,event_type,event_date,"
        "properties_json,result_count,client_ip,client_event_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_event_id) DO NOTHING",
        (event_id, user_id, role, event_type, stamp[:10],
         json.dumps(properties, ensure_ascii=False, sort_keys=True), result_count,
         str(client_ip or "")[:64], client_event_id, stamp),
    )
    connection.commit()
    return {"recorded": inserted.rowcount == 1, "event_id": event_id if inserted.rowcount == 1 else None}


def record_page_open(connection, body):
    public = {key: value for key, value in body.items() if not str(key).startswith("_")}
    payload = page_open_payload(public)
    result = _insert(
        connection, body["_actor_id"], body["_role"], PAGE_OPEN_EVENT,
        {"page": payload["page"]}, None, body.get("_client_ip"),
        payload["client_event_id"],
    )
    return ok(result)


def record_quote_usage(connection, body):
    public = {key: value for key, value in body.items() if not str(key).startswith("_")}
    payload = quote_usage_payload(public)
    stamp = _stamp()
    event_id = "quote_" + uuid.uuid4().hex
    connection.execute(
        "INSERT INTO quote_usage_events(event_id,user_id,role,action,quote_kind,item_count,"
        "field_type,event_date,client_ip,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (event_id, body["_actor_id"], body["_role"], payload["action"],
         payload["quote_kind"], payload["item_count"], payload["field_type"],
         stamp[:10], str(body.get("_client_ip") or "")[:64], stamp),
    )
    connection.commit()
    return ok({"recorded": True, "event_id": event_id})


def record_sales_search(connection, query, endpoint, result):
    properties = search_properties(query, endpoint)
    if (not query.get("_actor_id") or query.get("activity") != "intentional"
            or (query.get('_client_app') != 'prospector' and not has_business_search(properties))):
        return {"recorded": False}
    client_event_id = None
    if query.get('_client_app') == 'prospector':
        from services.prospector_auth import query_identity
        query_id = query_identity(query.get('query_id'))
        properties['client_app'] = 'prospector'
        properties['query_id'] = query_id
        client_event_id = 'prospector:' + query['_actor_id'] + ':' + query_id
    result_count = int(result.get("total") or len(result.get("rows") or result.get("groups") or []))
    return _insert(
        connection, query["_actor_id"], query.get("_role") or "",
        SALES_SEARCH_EVENT, properties, result_count, query.get("_client_ip"), client_event_id,
    )


def _recent_rows(connection, since, hide_admin=False):
    json_object = _json_object_function(connection)
    json_cast = _json_object_cast(connection)
    admin_clause = (" AND e.role<>'admin' AND e.user_id NOT IN "
                    "(SELECT user_id FROM operators WHERE role='admin')") if hide_admin else ""
    rows = [dict(row) for row in connection.execute(
        "SELECT e.event_id,e.user_id,e.role,e.event_type,e.event_date,e.properties_json,"
        "e.result_count,e.client_ip,e.created_at,COALESCE(o.display_name,o.name,e.user_id) AS display_name "
        "FROM user_activity_events e LEFT JOIN operators o ON o.user_id=e.user_id "
        "WHERE e.event_date>=?" + admin_clause +
        " ORDER BY e.created_at DESC,e.event_id DESC", (since,))]
    rows.extend(dict(row) for row in connection.execute(
        "SELECT e.event_id,e.user_id,e.role,'quote_usage' AS event_type,e.event_date,"
        f"{json_object}('quote_kind',e.quote_kind,'item_count',e.item_count,'field_type',e.field_type){json_cast} AS properties_json,"
        "NULL AS result_count,e.client_ip,e.created_at,COALESCE(o.display_name,o.name,e.user_id) AS display_name "
        "FROM quote_usage_events e LEFT JOIN operators o ON o.user_id=e.user_id WHERE e.event_date>=?" + admin_clause,
        (since,)))
    return rows


def summary(connection, query):
    options = analytics_query(query)
    today = datetime.now(BUSINESS_TIMEZONE).date()
    since = (today - timedelta(days=options["days"] - 1)).isoformat()
    metric_since = (today - timedelta(days=max(options["days"], 30) - 1)).isoformat()
    metric_rows = _recent_rows(connection, metric_since, options["hide_admin"])
    rows = [row for row in metric_rows if row["event_date"] >= since]
    daily_users = defaultdict(set)
    daily_events = Counter()
    daily_searches = Counter()
    daily_page_opens = Counter()
    daily_zero_results = Counter()
    top_pods = Counter()
    top_pols = Counter()
    top_routes = Counter()
    zero_results = 0
    search_users = set()
    for row in rows:
        daily_users[row["event_date"]].add(row["user_id"])
        daily_events[row["event_date"]] += 1
        if row["event_type"] == PAGE_OPEN_EVENT:
            daily_page_opens[row["event_date"]] += 1
        if row["event_type"] != SALES_SEARCH_EVENT:
            continue
        search_users.add(row["user_id"])
        daily_searches[row["event_date"]] += 1
        props = decode_properties(row["properties_json"])
        for field, counter in (("pod", top_pods), ("pol", top_pols), ("route", top_routes)):
            for value in str(props.get(field) or "").split(","):
                if value.strip():
                    counter[value.strip()] += 1
        if int(row["result_count"] or 0) == 0:
            zero_results += 1
            daily_zero_results[row["event_date"]] += 1
    dates = [(today - timedelta(days=offset)).isoformat()
             for offset in reversed(range(options["days"]))]
    daily = [{"date": day, "active_users": len(daily_users[day]),
              "events": daily_events[day], "page_opens": daily_page_opens[day],
              "searches": daily_searches[day],
              "zero_result_searches": daily_zero_results[day]}
             for day in dates]
    return ok({
        "days": options["days"], "since": since, "through": today.isoformat(),
        "dau": len({row["user_id"] for row in metric_rows if row["event_date"] == today.isoformat()}),
        "wau": len({row["user_id"] for row in metric_rows if row["event_date"] >= (today - timedelta(days=6)).isoformat()}),
        "mau": len({row["user_id"] for row in metric_rows if row["event_date"] >= (today - timedelta(days=29)).isoformat()}),
        "search_users": len(search_users),
        "events": len(rows), "page_opens": sum(daily_page_opens.values()),
        "searches": sum(daily_searches.values()),
        "zero_result_searches": zero_results, "daily": daily,
        "top": {
            "pods": [{"value": value, "count": count} for value, count in top_pods.most_common(10)],
            "pols": [{"value": value, "count": count} for value, count in top_pols.most_common(10)],
            "routes": [{"value": value, "count": count} for value, count in top_routes.most_common(10)],
        },
    })


def events(connection, query):
    options = analytics_query(query)
    today = datetime.now(BUSINESS_TIMEZONE).date()
    since = (today - timedelta(days=options["days"] - 1)).isoformat()
    clauses = ["e.event_date>=?"]
    args = [since]
    if options["event_type"]:
        clauses.append("e.event_type=?")
        args.append(options["event_type"])
    if options["user_id"]:
        clauses.append("e.user_id=?")
        args.append(options["user_id"])
    if options["hide_admin"]:
        clauses.append("e.role<>'admin'")
        clauses.append("e.user_id NOT IN (SELECT user_id FROM operators WHERE role='admin')")
    where = " WHERE " + " AND ".join(clauses)
    total = connection.execute(
        "SELECT COUNT(*) FROM user_activity_events e" + where, args).fetchone()[0]
    rows = [dict(row) for row in connection.execute(
        "SELECT e.event_id,e.user_id,COALESCE(o.display_name,o.name,e.user_id) AS display_name,"
        "e.role,e.event_type,e.event_date,e.properties_json,e.result_count,e.client_ip,e.created_at "
        "FROM user_activity_events e LEFT JOIN operators o ON o.user_id=e.user_id" + where +
        " ORDER BY e.created_at DESC,e.event_id DESC LIMIT ? OFFSET ?",
        [*args, options["limit"], options["offset"]])]
    for row in rows:
        row["properties"] = decode_properties(row.pop("properties_json"))
    return ok({"rows": rows, "total": total, **options})


def timeline(connection, query):
    options = analytics_query(query)
    today = datetime.now(BUSINESS_TIMEZONE).date()
    since = (today - timedelta(days=options["days"] - 1)).isoformat()
    json_object = _json_object_function(connection)
    json_cast = _json_object_cast(connection)
    unified = (
        "SELECT e.event_id,'activity' AS source,"
        "CASE WHEN e.event_type='page_open' THEN 'access' ELSE 'query' END AS category,"
        "e.event_type AS action,e.user_id,COALESCE(o.display_name,o.name,e.user_id) AS display_name,"
        "e.role,e.user_id AS actor_id,COALESCE(o.display_name,o.name,e.user_id) AS actor_name,"
        "e.properties_json AS detail_json,e.result_count,e.client_ip,e.created_at "
        "FROM user_activity_events e LEFT JOIN operators o ON o.user_id=e.user_id "
        "UNION ALL "
        "SELECT e.event_id,'quote' AS source,'quote' AS category,e.action,e.user_id,"
        "COALESCE(o.display_name,o.name,e.user_id),e.role,e.user_id,"
        "COALESCE(o.display_name,o.name,e.user_id),"
        f"{json_object}('quote_kind',e.quote_kind,'item_count',e.item_count,'field_type',e.field_type){json_cast},"
        "NULL,e.client_ip,e.created_at FROM quote_usage_events e "
        "LEFT JOIN operators o ON o.user_id=e.user_id "
        "UNION ALL "
        "SELECT e.event_id,'account' AS source,"
        "CASE WHEN e.action LIKE 'login_%' OR e.action LIKE 'security_%' "
        "OR e.action IN ('password_changed','account_registered') THEN 'security' ELSE 'management' END,"
        "e.action,e.user_id,COALESCE(o.display_name,o.name,e.user_id),COALESCE(o.role,''),"
        "e.actor_id,e.actor_name,e.detail_json,NULL,'',e.created_at "
        "FROM account_events e LEFT JOIN operators o ON o.user_id=e.user_id"
    )
    clauses = ["substr(created_at,1,10)>=?"]
    args = [since]
    if options["user_id"]:
        clauses.append("user_id=?")
        args.append(options["user_id"])
    if options["hide_admin"]:
        clauses.append("COALESCE(role,'')<>'admin'")
        clauses.append("actor_id NOT IN (SELECT user_id FROM operators WHERE role='admin')")
    if options["category"]:
        clauses.append("category=?")
        args.append(options["category"])
    if options["q"]:
        escaped = options["q"].replace("!", "!!").replace("%", "!%").replace("_", "!_")
        clauses.append("LOWER(COALESCE(display_name,'') || ' ' || COALESCE(actor_name,'') || ' ' || "
                       "COALESCE(action,'') || ' ' || COALESCE(detail_json,'')) LIKE LOWER(?) ESCAPE '!'")
        args.append("%" + escaped + "%")
    where = " WHERE " + " AND ".join(clauses)
    if options["display_mode"] == "account":
        grouped = "SELECT user_id,MAX(display_name) AS display_name,MAX(role) AS role," \
                  "COUNT(*) AS event_count,MAX(created_at) AS latest_at " \
                  "FROM (" + unified + ") timeline" + where + " GROUP BY user_id"
        total = connection.execute("SELECT COUNT(*) FROM (" + grouped + ") account_rows", args).fetchone()[0]
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM (" + grouped + ") account_rows "
            "ORDER BY replace(latest_at,'T',' ') DESC,user_id LIMIT ? OFFSET ?",
            [*args, options["limit"], options["offset"]])]
        return ok({"rows": rows, "total": total, **options})
    total = connection.execute("SELECT COUNT(*) FROM (" + unified + ") timeline" + where, args).fetchone()[0]
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM (" + unified + ") timeline" + where +
        " ORDER BY replace(created_at,'T',' ') DESC,event_id DESC LIMIT ? OFFSET ?",
        [*args, options["limit"], options["offset"]])]
    for row in rows:
        details = decode_properties(row.pop("detail_json"))
        row["details"] = details
        if row["source"] == "account":
            row["client_ip"] = str(details.get("client_ip") or "")
    return ok({"rows": rows, "total": total, **options})

