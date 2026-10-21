"""Pure validation and normalization for product-usage analytics."""
import json
import re
from typing import Any, Mapping

from core.activity_contracts import (
    ACTIVITY_DEFAULT_DAYS,
    ACTIVITY_MAX_CLIENT_EVENT_ID_LENGTH,
    ACTIVITY_MAX_DAYS,
    ACTIVITY_MAX_PAGE_LENGTH,
    ACTIVITY_MAX_PAGE_SIZE,
    ACTIVITY_MAX_SEARCH_LENGTH,
    ACTIVITY_MAX_VALUE_LENGTH,
    ACTIVITY_PAGE_SIZE,
    ACTIVITY_QUERY_FIELDS,
    ACTIVITY_CATEGORIES,
    ACTIVITY_DISPLAY_MODES,
    ACTIVITY_HIDE_ADMIN_PARAM,
    QUOTE_USAGE_ACTIONS,
    QUOTE_USAGE_FIELDS,
    QUOTE_USAGE_KINDS,
    QUOTE_USAGE_MAX_ITEMS,
)


CLIENT_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def quote_usage_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"action", "quote_kind", "item_count", "field_type"}
    unexpected = set(body) - allowed
    if unexpected:
        raise ValueError("报价表行为包含未允许字段：" + "、".join(sorted(unexpected)))
    action = str(body.get("action") or "").strip()
    quote_kind = str(body.get("quote_kind") or "").strip()
    field_type = str(body.get("field_type") or "").strip()
    count = body.get("item_count", 0)
    if action not in QUOTE_USAGE_ACTIONS or quote_kind not in QUOTE_USAGE_KINDS:
        raise ValueError("报价表行为类型无效")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= QUOTE_USAGE_MAX_ITEMS:
        raise ValueError("报价条目数量无效")
    if field_type and field_type not in QUOTE_USAGE_FIELDS:
        raise ValueError("报价字段类型无效")
    if action == "cell_edited" and not field_type:
        raise ValueError("字段编辑行为需要字段类型")
    return {"action": action, "quote_kind": quote_kind,
            "item_count": count, "field_type": field_type}


def page_open_payload(body: Mapping[str, Any]) -> dict[str, str]:
    allowed = {"page", "client_event_id"}
    unexpected = set(body) - allowed
    if unexpected:
        raise ValueError("页面打开事件包含未允许字段：" + "、".join(sorted(unexpected)))
    page = str(body.get("page") or "/").strip()
    client_event_id = str(body.get("client_event_id") or "").strip()
    if not page.startswith("/") or len(page) > ACTIVITY_MAX_PAGE_LENGTH:
        raise ValueError("page 无效")
    if (not client_event_id or len(client_event_id) > ACTIVITY_MAX_CLIENT_EVENT_ID_LENGTH
            or not CLIENT_EVENT_ID_RE.fullmatch(client_event_id)):
        raise ValueError("client_event_id 无效")
    return {"page": page, "client_event_id": client_event_id}


def search_properties(query: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
    properties: dict[str, Any] = {"endpoint": endpoint}
    for field in ACTIVITY_QUERY_FIELDS:
        value = query.get(field)
        if value in (None, ""):
            continue
        text = str(value)
        properties[field] = text[:ACTIVITY_MAX_VALUE_LENGTH]
    if query.get('_client_app') == 'prospector':
        from services.prospector_query import filter_values, MAX_FILTER_LENGTH
        for field in ('terms', 'lanes', 'polExtra', 'podExtra'):
            values = filter_values(query, field)
            if values:
                properties[field] = values
        for field in ('lane', 'container'):
            value = query.get(field)
            if value not in (None, ''):
                properties[field] = str(value)[:MAX_FILTER_LENGTH]
    return properties


def has_business_search(properties: Mapping[str, Any]) -> bool:
    non_business = {"endpoint", "activity", "limit", "offset", "sort", "direction", "status"}
    return any(key not in non_business for key in properties)


def analytics_query(query: Mapping[str, Any]) -> dict[str, Any]:
    try:
        days = int(query.get("days") or ACTIVITY_DEFAULT_DAYS)
        limit = int(query.get("limit") or ACTIVITY_PAGE_SIZE)
        offset = int(query.get("offset") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("days/limit/offset 必须是整数") from error
    if not 1 <= days <= ACTIVITY_MAX_DAYS:
        raise ValueError(f"days 必须在 1-{ACTIVITY_MAX_DAYS} 之间")
    if not 1 <= limit <= ACTIVITY_MAX_PAGE_SIZE or offset < 0:
        raise ValueError("limit/offset 无效")
    event_type = str(query.get("event_type") or "").strip()
    user_id = str(query.get("user_id") or "").strip()
    category = str(query.get("category") or "").strip()
    display_mode = str(query.get("display_mode") or "timeline").strip()
    keyword = str(query.get("q") or "").strip()
    hide_admin = str(query.get(ACTIVITY_HIDE_ADMIN_PARAM) or "0").strip()
    if hide_admin not in {"0", "1"}:
        raise ValueError("hide_admin 必须为 0 或 1")
    if category and category not in ACTIVITY_CATEGORIES:
        raise ValueError("事件分类无效")
    if display_mode not in ACTIVITY_DISPLAY_MODES:
        raise ValueError("展示模式无效")
    if len(keyword) > ACTIVITY_MAX_SEARCH_LENGTH:
        raise ValueError("搜索内容过长")
    return {"days": days, "limit": limit, "offset": offset,
            "event_type": event_type, "user_id": user_id,
            "display_mode": display_mode,
            "category": category, "q": keyword, "hide_admin": hide_admin == "1"}


def event_category(source: str, action: str) -> str:
    if source == "activity":
        return "access" if action == "page_open" else "query"
    if source == "quote":
        return "quote"
    if (action.startswith("login_") or action.startswith("security_")
            or action in {"password_changed", "account_registered"}):
        return "security"
    return "management"


def decode_properties(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
