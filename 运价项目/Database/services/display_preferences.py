"""Validate display-only browsing parameters without changing search permissions."""
import json

from core.contracts import ACCOUNT_PREFERENCE_MAX_ITEMS, DISPLAY_MODE_PARAM, DISPLAY_MODE_PREFERRED


def preferred_values(query, key):
    if query.get(DISPLAY_MODE_PARAM) != DISPLAY_MODE_PREFERRED:
        return []
    raw = query.get(key)
    if not raw:
        return []
    values = json.loads(raw)
    if (not isinstance(values, list) or len(values) > ACCOUNT_PREFERENCE_MAX_ITEMS
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError("默认展示偏好须为名称数组")
    return list(dict.fromkeys(values))
