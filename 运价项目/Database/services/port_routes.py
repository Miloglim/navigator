"""Pure membership policy; quote classifications are never expanded by membership."""
from __future__ import annotations

import re
from collections.abc import Iterable


def route_tokens(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = re.split(r"[,;\uFF0C\uFF1B]", value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise ValueError("routes_list must be an array of route names")
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def unique_common_route(memberships: Iterable[Iterable[str]]) -> str:
    common: set[str] | None = None
    for routes in memberships:
        names = set(routes)
        if not names:
            return ""
        common = names if common is None else common & names
    return next(iter(common)) if common is not None and len(common) == 1 else ""
