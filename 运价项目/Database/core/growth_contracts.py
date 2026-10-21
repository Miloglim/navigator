"""Account growth API and event contract; no role or quote authorization changes."""
from typing import TypedDict

GROWTH_ROUTE = "/api/account/growth"
GROWTH_SEARCH_ROUTE = "/api/account/growth/search"
GROWTH_TABLE = "account_experience_events"
GROWTH_LEGACY_SEARCH_EVENT = "daily_quote_search"
GROWTH_SEARCH_EVENT = "daily_effective_search"
GROWTH_REVIEW_EVENT = "daily_review_activity"
GROWTH_GOVERNANCE_EVENT = "daily_governance_activity"
GROWTH_GOVERNANCE_ITEM_EVENT = "governance_item_completed"
GROWTH_APPROVAL_EVENT = "pending_approved"
GROWTH_CORRECTION_EVENT = "pending_correction_approved"
GROWTH_RESET_EVENT = "account_growth_reset"
GROWTH_SEARCH_POINTS = 18
GROWTH_DAILY_ACTIVITY_POINTS = 18
GROWTH_APPROVAL_POINTS = 2
GROWTH_CORRECTION_POINTS = 6
GROWTH_GOVERNANCE_POINTS = 6
GROWTH_DAILY_CAP = 30
GROWTH_RULE_VERSION = 2
GROWTH_HISTORY_SIZE = 10
GROWTH_HISTORY_MAX_SIZE = 50
GROWTH_LEGACY_XP_PER_LEVEL = 10
GROWTH_LEVELS_PER_CYCLE = 50
GROWTH_LEVEL_BASE_XP = 12
GROWTH_LEVEL_MAX_XP = 30


class GrowthSummary(TypedDict):
    total_xp: int
    level: int
    cycle: int
    level_name: str
    level_start: int
    next_threshold: int
    level_cost: int
    level_progress: int
    cycle_start: int
    cycle_end: int
    cycle_xp: int
    today_awarded: bool


class GrowthAward(TypedDict):
    awarded: bool
    awarded_points: int
    today_xp: int
    daily_cap: int
    level_up: bool
    cycle_up: bool
    previous_level: int
    previous_cycle: int
    summary: GrowthSummary


GROWTH_DDL = """CREATE TABLE IF NOT EXISTS account_experience_events (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event_type TEXT NOT NULL,
    subject_key TEXT NOT NULL, points INTEGER NOT NULL, rule_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id,event_type,subject_key), CHECK(points > 0))"""
