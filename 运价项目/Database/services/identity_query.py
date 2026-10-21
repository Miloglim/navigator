"""Pure normalization for user-pasted source and entry identifiers."""
from core.contracts import SOURCE_MESSAGE_ID_PREFIX

ID_LABEL_PREFIXES = ("组:", "组：", "消息:", "消息：", "条目:", "条目：")


def normalize_identity_query(value: str) -> str:
    """Remove display labels and DWS message padding without fuzzy matching."""
    value = str(value or "").strip()
    for prefix in ID_LABEL_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    if value.startswith(SOURCE_MESSAGE_ID_PREFIX):
        value = value[len(SOURCE_MESSAGE_ID_PREFIX):]
    return value.rstrip("=").strip()
