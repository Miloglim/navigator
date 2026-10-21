"""Read-only heartbeat monitoring routes backed by pipeline receipt files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

from core.contracts import MONITORING_GROUPS_ROUTE, MONITORING_RUNS_ROUTE, MONITORING_SUMMARY_ROUTE, ok
from services.monitoring import summarize_runs

MAX_RECEIPTS = 500


def load_receipts(runtime_dir: Path) -> List[Dict[str, Any]]:
    """Load compact receipts only; raw DingTalk messages are intentionally never read here."""
    work = runtime_dir / ".work"
    out: List[Dict[str, Any]] = []
    for path in sorted(work.glob("run_*/pull_receipt_*.json"), reverse=True)[:MAX_RECEIPTS]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(item, dict) and isinstance(item.get("groups"), dict):
                item["run_id"] = path.stem.replace("pull_receipt_", "")
                out.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_group_names(project_root: Path) -> Dict[str, str]:
    cfg = project_root / "Skill" / "freight-rate-heartbeat" / "scripts" / "config" / "chat_sources.json"
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: Dict[str, str] = {}
    for item in raw.get("groups") or []:
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or "").strip()
        if slug and name:
            out[slug] = name
    return out


def build_monitoring_get_routes(runtime_dir: Callable[[], Path]) -> Dict[str, Callable[[Dict[str, str]], Dict[str, Any]]]:
    def facts() -> Dict[str, Any]:
        runtime = runtime_dir()
        project_root = runtime.parent.parent if runtime.name == "heartbeat-data" else runtime.parent
        return summarize_runs(load_receipts(runtime), group_names=load_group_names(project_root))

    def summary(_: Dict[str, str]) -> Dict[str, Any]:
        data = facts()
        return ok({"latest_run": data["latest_run"], "totals": data["totals"],
                   "latest": data["latest"], "recent_24h": data["recent_24h"]})

    def groups(_: Dict[str, str]) -> Dict[str, Any]:
        return ok({"groups": facts()["groups"]})

    def runs(q: Dict[str, str]) -> Dict[str, Any]:
        limit = max(1, min(int(q.get("limit") or 30), 200))
        return ok({"runs": facts()["runs"][:limit]})

    return {MONITORING_SUMMARY_ROUTE: summary, MONITORING_GROUPS_ROUTE: groups,
            MONITORING_RUNS_ROUTE: runs}
