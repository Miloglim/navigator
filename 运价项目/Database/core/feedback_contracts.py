"""Published-item feedback API and field contract."""
from typing import TypedDict

FEEDBACK_ROUTE = "/api/item-feedback"
FEEDBACK_DISMISS_ROUTE = "/api/item-feedback/dismiss"
FEEDBACK_TABLE = "item_feedback"
FEEDBACK_FIELDS = {
    "pol": "起运港", "pod": "目的港", "route": "航线", "carrier": "船司",
    "container_type": "柜型", "price_usd": "价格 USD", "vessel_voyage": "船名航次",
    "cutoff_raw": "截关", "box_desc": "原始箱型箱量", "box_qty": "箱量",
    "space_type": "舱位类型", "validity_raw": "有效期原文",
    "valid_from": "有效期起", "valid_to": "有效期止", "etd": "船期 ETD",
    "free_days": "Freetime（天）", "dead_freight": "亏舱费",
    "transit_days": "T/T 航程（天）", "remark": "备注",
    "service_name": "航线服务", "destination_terminal": "目的码头",
}
FEEDBACK_NUMERIC_FIELDS = frozenset({"price_usd", "box_qty", "free_days", "dead_freight", "transit_days"})
FEEDBACK_DATE_FIELDS = frozenset({"valid_from", "valid_to"})
FEEDBACK_MAX_FIELDS = len(FEEDBACK_FIELDS)
FEEDBACK_MAX_VALUE_LENGTH = 1000
FEEDBACK_MAX_NOTE_LENGTH = 1000


class FeedbackCreate(TypedDict):
    entry_id: str
    changes: dict[str, str]
    expected: dict[str, str]
    note: str


class FeedbackDismiss(TypedDict):
    submission_id: str
    action: str
