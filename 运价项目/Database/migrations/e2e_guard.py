"""Shared safety guard for manual E2E scripts.

These scripts are allowed to mutate only an explicitly supplied disposable
database and service.  They must never infer the running board's defaults.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


def target() -> tuple[Path, str, str]:
    try:
        db = Path(os.environ["FRT_E2E_DB"]).resolve()
        base = os.environ["FRT_E2E_URL"].rstrip("/")
        token = os.environ["FRT_E2E_TOKEN"].strip()
    except KeyError as exc:
        raise SystemExit("E2E disabled: set FRT_E2E_DB, FRT_E2E_URL and FRT_E2E_TOKEN") from exc
    production = (Path(__file__).resolve().parent.parent / "freight_manual.db").resolve()
    parsed = urlparse(base)
    if not db.exists() or db == production or parsed.port == 8795 or not token:
        raise SystemExit("E2E target must be a non-production db and a non-8795 service with a token")
    return db, base, token
