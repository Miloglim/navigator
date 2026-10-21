"""Pure growth projection and request validation."""
from core.growth_contracts import (GROWTH_HISTORY_MAX_SIZE, GROWTH_HISTORY_SIZE,
                                   GROWTH_LEGACY_XP_PER_LEVEL, GROWTH_LEVEL_BASE_XP,
                                   GROWTH_LEVEL_MAX_XP, GROWTH_LEVELS_PER_CYCLE)


def pagination(query):
    if set(query) - {"limit", "offset"}:
        raise ValueError("unsupported growth query field")
    try:
        limit = int(query.get("limit", GROWTH_HISTORY_SIZE))
        offset = int(query.get("offset", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid growth pagination") from error
    if not 1 <= limit <= GROWTH_HISTORY_MAX_SIZE or offset < 0:
        raise ValueError("invalid growth pagination")
    return limit, offset


def level_cost(level):
    """Linear 12..30 XP ramp inside each 50-level cycle."""
    index = max(0, min(GROWTH_LEVELS_PER_CYCLE - 1, int(level) - 1))
    span = GROWTH_LEVEL_MAX_XP - GROWTH_LEVEL_BASE_XP
    return GROWTH_LEVEL_BASE_XP + (index * span // (GROWTH_LEVELS_PER_CYCLE - 1))


GROWTH_XP_PER_CYCLE = sum(level_cost(level) for level in range(1, GROWTH_LEVELS_PER_CYCLE + 1))


def threshold_for_completed_levels(completed_levels):
    cycles, levels = divmod(max(0, int(completed_levels)), GROWTH_LEVELS_PER_CYCLE)
    return cycles * GROWTH_XP_PER_CYCLE + sum(level_cost(level) for level in range(1, levels + 1))


def map_legacy_xp(total):
    """Preserve the exact legacy level and proportional in-level progress."""
    completed, remainder = divmod(max(0, int(total)), GROWTH_LEGACY_XP_PER_LEVEL)
    start = threshold_for_completed_levels(completed)
    cost = level_cost(completed % GROWTH_LEVELS_PER_CYCLE + 1)
    return start + remainder * cost // GROWTH_LEGACY_XP_PER_LEVEL


def summary(total, today_awarded):
    total = max(0, int(total))
    cycle = total // GROWTH_XP_PER_CYCLE + 1
    within_cycle = total % GROWTH_XP_PER_CYCLE
    level = 1
    level_start_in_cycle = 0
    for candidate in range(1, GROWTH_LEVELS_PER_CYCLE + 1):
        cost = level_cost(candidate)
        if within_cycle < level_start_in_cycle + cost:
            level = candidate
            break
        level_start_in_cycle += cost
    level_start = (cycle - 1) * GROWTH_XP_PER_CYCLE + level_start_in_cycle
    cost = level_cost(level)
    return {"total_xp": total, "level": level, "cycle": cycle,
            "level_name": f"第{cycle}循环", "level_start": level_start,
            "next_threshold": level_start + cost, "level_cost": cost,
            "level_progress": total - level_start,
            "cycle_start": (cycle - 1) * GROWTH_XP_PER_CYCLE,
            "cycle_end": cycle * GROWTH_XP_PER_CYCLE,
            "cycle_xp": GROWTH_XP_PER_CYCLE,
            "today_awarded": today_awarded}


def award_feedback(current, awarded_points, today_xp, daily_cap):
    """Describe a committed XP change for one consistent UI feedback path."""
    points = max(0, int(awarded_points))
    previous = summary(current["total_xp"] - points, False)
    return {"awarded": points > 0, "awarded_points": points,
            "today_xp": max(0, int(today_xp)), "daily_cap": int(daily_cap),
            "level_up": (current["cycle"], current["level"]) !=
                        (previous["cycle"], previous["level"]),
            "cycle_up": current["cycle"] != previous["cycle"],
            "previous_level": previous["level"],
            "previous_cycle": previous["cycle"], "summary": current}
