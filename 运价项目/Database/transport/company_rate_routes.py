"""Thin HTTP adapters for optional company-rate tasks."""

from core.company_rates_contracts import (
    COMPANY_RATE_QUERY_ROUTE,
    COMPANY_RATE_TASK_ROUTE,
    COMPANY_REALTIME_QUERY_ROUTE,
    COMPANY_MAPPING_ROUTE, COMPANY_MAPPING_SAVE_ROUTE, COMPANY_MAPPING_PROBE_ROUTE,
)
from core.contracts import ADMIN_ROLES, ok


def build_company_rate_routes(runtime):
    def admin(payload, operation):
        if payload.get('_role') not in ADMIN_ROLES or not payload.get('_actor_id'):
            raise PermissionError('仅管理员可维护公司接口映射')
        return ok(operation())
    def create(kind, body):
        user_id = str(body.get("_actor_id") or "")
        if not user_id:
            raise PermissionError("请先登录")
        query = {key: value for key, value in body.items() if not key.startswith("_")}
        return ok(runtime.create(user_id, kind, query))

    def read(query):
        task_id = str(query.get("id") or "").strip()
        user_id = str(query.get("_actor_id") or "")
        if not task_id:
            raise ValueError("缺少查价任务ID")
        task = runtime.get(user_id, task_id)
        if task is None:
            raise LookupError("查价任务不存在")
        return ok(task)

    return ({COMPANY_RATE_TASK_ROUTE: read,
             COMPANY_MAPPING_ROUTE: lambda q: admin(q,runtime.mapping_list)}, {
        COMPANY_MAPPING_SAVE_ROUTE: lambda body: admin(body,lambda: runtime.mapping_save(body)),
        COMPANY_MAPPING_PROBE_ROUTE: lambda body: admin(body,lambda: runtime.mapping_probe(body)),
        COMPANY_RATE_QUERY_ROUTE: lambda body: create("company", body),
        COMPANY_REALTIME_QUERY_ROUTE: lambda body: create("realtime", body),
    })

