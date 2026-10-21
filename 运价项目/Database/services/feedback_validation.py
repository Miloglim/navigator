"""Pure validation for user-proposed field values."""
from datetime import date

from core.feedback_contracts import (FEEDBACK_DATE_FIELDS, FEEDBACK_FIELDS,
                                     FEEDBACK_MAX_FIELDS, FEEDBACK_MAX_NOTE_LENGTH,
                                     FEEDBACK_MAX_VALUE_LENGTH, FEEDBACK_NUMERIC_FIELDS)


def normalize_feedback(changes, expected, note):
    if not isinstance(changes, dict) or not changes or len(changes) > FEEDBACK_MAX_FIELDS:
        raise ValueError("请至少修改一个字段")
    if not isinstance(expected, dict) or not isinstance(note, str):
        raise ValueError("反馈格式不正确")
    note = note.strip()
    if not note or len(note) > FEEDBACK_MAX_NOTE_LENGTH:
        raise ValueError("请填写简短的修改依据")
    normalized = {}
    for field, value in changes.items():
        if field not in FEEDBACK_FIELDS or not isinstance(value, str) or len(value) > FEEDBACK_MAX_VALUE_LENGTH:
            raise ValueError("反馈包含不允许修改的字段")
        if field not in expected or not isinstance(expected[field], str):
            raise ValueError("请刷新条目后再反馈")
        value = value.strip()
        if field in FEEDBACK_NUMERIC_FIELDS and value:
            if not value.isdecimal() or int(value) < 0:
                raise ValueError(FEEDBACK_FIELDS[field] + "必须是非负整数")
            value = str(int(value))
        if field in FEEDBACK_DATE_FIELDS and value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError as error:
                raise ValueError(FEEDBACK_FIELDS[field] + "必须是有效日期") from error
        normalized[field] = value
    return normalized, note
