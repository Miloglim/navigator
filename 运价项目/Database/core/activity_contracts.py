"""Contracts for authenticated product-usage analytics."""
from typing import Final


ACTIVITY_TABLE: Final = "user_activity_events"
ACTIVITY_PAGE_OPEN_ROUTE: Final = "/api/activity/page-open"
ACTIVITY_QUOTE_USAGE_ROUTE: Final = "/api/activity/quote-usage"
ACTIVITY_SUMMARY_ROUTE: Final = "/api/admin/activity/summary"
ACTIVITY_EVENTS_ROUTE: Final = "/api/admin/activity/events"
ACTIVITY_TIMELINE_ROUTE: Final = "/api/admin/activity/timeline"

PAGE_OPEN_EVENT: Final = "page_open"
SALES_SEARCH_EVENT: Final = "sales_search"
ACTIVITY_EVENT_TYPES: Final = frozenset({PAGE_OPEN_EVENT, SALES_SEARCH_EVENT})
ACTIVITY_QUERY_FIELDS: Final = (
    "pol", "pod", "carrier", "route", "ctype", "group", "valid_since",
    "time_from", "time_to", "stype", "status", "sort", "direction",
    "display_mode", "display_routes", "display_groups", "activity", "limit", "offset",
)
ACTIVITY_MAX_VALUE_LENGTH: Final = 512
ACTIVITY_MAX_PAGE_LENGTH: Final = 120
ACTIVITY_MAX_CLIENT_EVENT_ID_LENGTH: Final = 96
ACTIVITY_DEFAULT_DAYS: Final = 30
ACTIVITY_MAX_DAYS: Final = 90
ACTIVITY_PAGE_SIZE: Final = 50
ACTIVITY_MAX_PAGE_SIZE: Final = 200
ACTIVITY_MAX_SEARCH_LENGTH: Final = 512
ACTIVITY_CATEGORIES: Final = frozenset({"access", "query", "quote", "security", "management"})
ACTIVITY_DISPLAY_MODES: Final = frozenset({"timeline", "account"})
ACTIVITY_HIDE_ADMIN_PARAM: Final = "hide_admin"

ACTIVITY_DDL: Final = """CREATE TABLE IF NOT EXISTS user_activity_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    properties_json TEXT NOT NULL DEFAULT '{}',
    result_count INTEGER,
    client_ip TEXT NOT NULL DEFAULT '',
    client_event_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (event_type IN ('page_open','sales_search')),
    CHECK (result_count IS NULL OR result_count >= 0)
)"""

ACTIVITY_INDEX_COLUMNS: Final = ("user_id", "event_type", "event_date", "created_at")

QUOTE_USAGE_TABLE: Final = "quote_usage_events"
QUOTE_USAGE_ACTIONS: Final = frozenset({
    "editor_opened", "item_selected", "item_deselected", "row_added", "row_removed",
    "table_cleared", "format_reset", "copied", "cell_edited", "column_changed",
})
QUOTE_USAGE_KINDS: Final = frozenset({"rates", "space", "both"})
QUOTE_USAGE_FIELDS: Final = frozenset({
    "price", "port", "date", "route", "transit", "carrier", "vessel",
    "container", "remark", "custom", "format", "other",
})
QUOTE_USAGE_MAX_ITEMS: Final = 500
QUOTE_USAGE_DDL: Final = """CREATE TABLE IF NOT EXISTS quote_usage_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    quote_kind TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    field_type TEXT NOT NULL DEFAULT '',
    event_date TEXT NOT NULL,
    client_ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (action IN ('editor_opened','item_selected','item_deselected','row_added','row_removed','table_cleared','format_reset','copied','cell_edited','column_changed')),
    CHECK (quote_kind IN ('rates','space','both')),
    CHECK (item_count >= 0 AND item_count <= 500),
    CHECK (field_type IN ('','price','port','date','route','transit','carrier','vessel','container','remark','custom','format','other'))
)"""
QUOTE_USAGE_INDEX_COLUMNS: Final = ("user_id", "action", "event_date", "created_at")
