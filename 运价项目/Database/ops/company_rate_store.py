"""Persistence for company-rate history and reusable results."""

import json
import time


def create_task(connection, task_id, user_id, kind, signature, query, now_epoch):
    connection.execute(
        "INSERT INTO company_rate_tasks(task_id,user_id,kind,query_signature,query_json,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'queued',?,?)",
        (task_id, user_id, kind, signature, json.dumps(query, ensure_ascii=False, sort_keys=True), now_epoch, now_epoch),
    )
    connection.commit()


def update_task(connection, task_id, status, now_epoch, error=None):
    connection.execute(
        "UPDATE company_rate_tasks SET status=?,error=?,updated_at=? WHERE task_id=?",
        (status, error, now_epoch, task_id),
    )
    connection.commit()


def save_result(connection, task_id, pair_key, items, created_at, expires_at, error=None):
    connection.execute(
        "INSERT INTO company_rate_results(task_id,pair_key,items_json,error,created_at,expires_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(task_id,pair_key) DO UPDATE SET items_json=excluded.items_json,error=excluded.error,created_at=excluded.created_at,expires_at=excluded.expires_at",
        (task_id, pair_key, json.dumps(items, ensure_ascii=False), error, created_at, expires_at),
    )
    connection.commit()


def _task(connection, row, now_epoch):
    if not row:
        return None
    task = dict(row)
    task["query"] = json.loads(task.pop("query_json"))
    task["results"] = []
    for result in connection.execute(
        "SELECT pair_key,items_json,error,created_at,expires_at FROM company_rate_results WHERE task_id=? ORDER BY pair_key",
        (task["task_id"],),
    ):
        item = dict(result)
        item["items"] = json.loads(item.pop("items_json"))
        item["fresh"] = item["expires_at"] >= now_epoch
        task["results"].append(item)
    return task


def get_task(connection, task_id, user_id, now_epoch=None):
    row = connection.execute(
        "SELECT * FROM company_rate_tasks WHERE task_id=? AND user_id=?", (task_id, user_id)
    ).fetchone()
    return _task(connection, row, int(time.time()) if now_epoch is None else now_epoch)


def find_fresh(connection, user_id, kind, signature, now_epoch):
    row = connection.execute(
        "SELECT t.* FROM company_rate_tasks t WHERE t.user_id=? AND t.kind=? AND t.query_signature=? "
        "AND t.status IN ('complete','partial') AND EXISTS (SELECT 1 FROM company_rate_results r "
        "WHERE r.task_id=t.task_id AND r.expires_at>=?) ORDER BY t.created_at DESC LIMIT 1",
        (user_id, kind, signature, now_epoch),
    ).fetchone()
    return _task(connection, row, now_epoch)

