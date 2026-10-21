"""Pure release gate. It deliberately has no database or HTTP dependency."""
from __future__ import annotations

from typing import Any, Dict, List

from core.settings import RELEASE_MODE_GUARDED_AUTO, RELEASE_MODE_REVIEW

MIN_VALIDITY_HOURS_FOR_AUTO_RELEASE = 24
MAX_AUTO_PRICE_CHANGE_PERCENT = 15
MIN_GROUP_CLEAN_RATE_PERCENT = 98


def _number(facts: Dict[str, Any], field: str) -> tuple[float | None, bool]:
    """Convert user-supplied numeric facts without allowing a route-level 500."""
    value = facts.get(field)
    if value in (None, ""):
        return 0.0, True
    try:
        return float(value), True
    except (TypeError, ValueError):
        return None, False


def decide_release(facts: Dict[str, Any], release_mode: str = RELEASE_MODE_REVIEW) -> Dict[str, Any]:
    """Return a conservative, explainable publication decision.

    This function does not publish anything. Until the business owner approves
    the thresholds and has sufficient historical evidence, ``review`` remains
    the only configured production mode.
    """
    reasons: List[str] = []
    if not facts.get("mechanical_check_passed"):
        reasons.append("mechanical_check_failed")
    if not facts.get("source_known"):
        reasons.append("unknown_source")
    if not facts.get("sender_known"):
        reasons.append("unknown_sender")
    if facts.get("ai_inferred_fields"):
        reasons.append("ai_inferred_fields")
    if facts.get("master_data_changed"):
        reasons.append("master_data_changed")
    validity_hours, validity_valid = _number(facts, "validity_hours")
    price_change_percent, price_valid = _number(facts, "price_change_percent")
    group_clean_rate_percent, clean_rate_valid = _number(facts, "group_clean_rate_percent")
    if not validity_valid:
        reasons.append("validity_hours_invalid")
    elif validity_hours < MIN_VALIDITY_HOURS_FOR_AUTO_RELEASE:
        reasons.append("validity_too_short")
    if not price_valid:
        reasons.append("price_change_invalid")
    elif abs(price_change_percent) > MAX_AUTO_PRICE_CHANGE_PERCENT:
        reasons.append("price_change_outside_threshold")
    if not clean_rate_valid:
        reasons.append("group_quality_invalid")
    elif group_clean_rate_percent < MIN_GROUP_CLEAN_RATE_PERCENT:
        reasons.append("group_quality_below_threshold")

    auto_allowed = release_mode == RELEASE_MODE_GUARDED_AUTO and not reasons
    return {
        "decision": "auto_publish" if auto_allowed else "review_required",
        "release_mode": release_mode if release_mode == RELEASE_MODE_GUARDED_AUTO else RELEASE_MODE_REVIEW,
        "reasons": reasons if reasons else ["guarded_auto_rules_satisfied"],
        "thresholds": {
            "min_validity_hours": MIN_VALIDITY_HOURS_FOR_AUTO_RELEASE,
            "max_price_change_percent": MAX_AUTO_PRICE_CHANGE_PERCENT,
            "min_group_clean_rate_percent": MIN_GROUP_CLEAN_RATE_PERCENT,
        },
    }
