"""Derived container information used for search, never a stored price category."""
import re

from core.contracts import (SALES_45_HIGH_CUBE_ALIASES, SALES_45_HIGH_CUBE_FIELDS,
                            SALES_45_HIGH_CUBE_PATTERN)

HIGH_CUBE_45 = re.compile(SALES_45_HIGH_CUBE_PATTERN, re.IGNORECASE)


def has_45_high_cube(value):
    return bool(HIGH_CUBE_45.search(str(value or "")))


def container_filter_clause(types, postgres=False):
    standard = [value for value in types if value not in SALES_45_HIGH_CUBE_ALIASES]
    clauses, args = [], []
    if standard:
        clauses.append("container_type IN (" + ",".join("?" for _ in standard) + ")")
        args.extend(standard)
    if any(value in SALES_45_HIGH_CUBE_ALIASES for value in types):
        for field in SALES_45_HIGH_CUBE_FIELDS:
            if postgres:
                clauses.append("COALESCE(" + field + ", '') ~* ?")
                args.append(SALES_45_HIGH_CUBE_PATTERN)
            else:
                clauses.append("freight_has_45(" + field + ") = 1")
    return "(" + " OR ".join(clauses) + ")" if clauses else "1=0", args
