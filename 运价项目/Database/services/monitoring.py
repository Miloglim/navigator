"""Pure monitoring summaries derived from validated heartbeat receipts."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping

HEALTH_OK = "ok"
HEALTH_ATTENTION = "attention"
HEALTH_UNKNOWN = "unknown"
RECENT_WINDOW_HOURS = 24


def _at(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize_runs(
    receipts: Iterable[Dict[str, Any]],
    now: datetime | None = None,
    group_names: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Summarize receipt facts without filesystem, database, or network access."""
    now = now or datetime.now()
    group_names = group_names or {}
    runs = sorted(receipts, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    groups: Dict[str, Dict[str, Any]] = {}
    total_returned = total_new = total_advanced = 0
    recent_returned = recent_new = recent_advanced = recent_runs = 0
    latest_returned = latest_new = latest_advanced = latest_groups = 0
    latest_stamp = _at(runs[0].get("created_at")) if runs else None
    for run in runs:
        stamp = _at(run.get("created_at"))
        is_recent = bool(stamp and (now - stamp).total_seconds() <= RECENT_WINDOW_HOURS * 3600)
        run_returned = run_new = run_advanced = 0
        for slug, item in (run.get("groups") or {}).items():
            returned = int(item.get("returned") or 0)
            added = int(item.get("new") or 0)
            advanced = int(bool(item.get("watermark_advanced")))
            total_returned += returned
            total_new += added
            total_advanced += advanced
            run_returned += returned
            run_new += added
            run_advanced += advanced
            if is_recent:
                recent_returned += returned
                recent_new += added
                recent_advanced += advanced
            if slug not in groups:
                age_minutes = int((now - stamp).total_seconds() // 60) if stamp else None
                groups[slug] = {
                    "group": slug, "group_name": group_names.get(slug) or slug,
                    "last_run_id": run.get("run_id"), "last_checked_at": run.get("created_at"),
                    "age_minutes": age_minutes, "returned": int(item.get("returned") or 0),
                    "new": int(item.get("new") or 0), "watermark_before": item.get("watermark_before") or "",
                    "watermark_after": item.get("watermark_after") or "", "watermark_advanced": bool(item.get("watermark_advanced")),
                    "status": item.get("status") or HEALTH_UNKNOWN,
                }
        if is_recent:
            recent_runs += 1
        if run is runs[0]:
            latest_returned, latest_new, latest_advanced = run_returned, run_new, run_advanced
            latest_groups = len(run.get("groups") or {})
    healthy = sum(1 for g in groups.values() if g["status"] == HEALTH_OK)
    attention = sum(1 for g in groups.values() if g["status"] == HEALTH_ATTENTION)
    unknown = sum(1 for g in groups.values() if g["status"] == HEALTH_UNKNOWN)
    latest_age_minutes = int((now - latest_stamp).total_seconds() // 60) if latest_stamp else None
    return {
        "latest_run": runs[0] if runs else None,
        "runs": runs,
        "groups": sorted(groups.values(), key=lambda g: (g["status"] != HEALTH_OK, g["group"])),
        "totals": {"run_count": len(runs), "group_count": len(groups), "healthy_groups": healthy,
                   "attention_groups": attention, "unknown_groups": unknown,
                   "returned": total_returned, "new": total_new, "watermark_advanced": total_advanced},
        "latest": {"group_count": latest_groups, "returned": latest_returned, "new": latest_new,
                   "watermark_advanced": latest_advanced, "age_minutes": latest_age_minutes},
        "recent_24h": {"run_count": recent_runs, "returned": recent_returned, "new": recent_new,
                       "watermark_advanced": recent_advanced},
    }
