"""Shared publisher inference contract, with no IO or third-party dependencies."""
from typing import TypedDict

PUBLISHER_RULE_VERSION = "1.5.0"
EXCLUDED = "excluded"
SHADOW_MODE = "shadow"
ROSTER_SCHEMA_VERSION = "1.3.0"
ROSTER_ACTIONS = frozenset({"confirm", "exclude", "reset"})
ROSTER_LOCK_TIMEOUT_SECONDS = 5
ROSTER_LOCK_RETRY_SECONDS = 0.05
RAW_PULL_ARCHIVE_FIELD = "raw_file"
PUBLISHER = "publisher"
CANDIDATE = "candidate"
INSUFFICIENT = "insufficient"
MENTION_ALL_MARKER = "@所有人"
QUOTED_TEXT_SEPARATOR = "——引用 "
LOOKBACK_DAYS = 28
MIN_COVERED_WORKDAYS = 10
MIN_ACTIVE_WORKDAY_RATIO = 0.8
MAX_DAILY_COEFFICIENT_VARIATION = 0.8
MIN_VOLUME_RATIO = 3.0
MIN_WEEKDAY_MESSAGES = 10
MIN_PEER_IDENTITIES = 2
PEER_BASELINE_DIVISOR = 2
WEEKEND_START = 5
BUSINESS_TIMEZONE_HOURS = 8
MESSAGE_EVIDENCE_LIMIT = 3

class PublisherConfirmation(TypedDict, total=False):
    group_cid: str
    name: str
    sender_id: str
    sender_open_id: str
    confirmed_on: str
    source: str
    scope: str  # group for exact-name exclusions without a DingTalk ID.
    decision: str  # publisher / excluded; absent means legacy confirmation.


class PublisherExclusion(TypedDict, total=False):
    name: str
    sender_id: str
    sender_open_id: str
    excluded_on: str
    source: str
    scope: str  # global
    decision: str  # excluded
    source_groups: list[str]


class PublisherMessage(TypedDict, total=False):
    message_id: str
    sender: str
    sender_id: str
    sender_open_id: str
    time: str
    content: str  # This sender's body only; quoted body is separate.

class PublisherAssessment(TypedDict):
    identity_key: str
    status: str
    basis: str
    reasons: list[str]
    identity_ready: bool
    mention_all_count: int
    mention_all_message_ids: list[str]
    message_count: int
    historical_message_count: int
    weekday_messages: int
    observed_volume_ratio: float | None
    observed_volume_gap: bool
    sample_share: float
    volume_ratio: float | None
    volume_gap: bool
    covered_workdays: int
    observed_workdays: int
    active_workdays: int
    activity_ratio: float | None
    daily_cv: float | None
    frequency_ready: bool
    stable_frequency: bool
    daily_counts: list[dict]


class PullReceiptEvidence(TypedDict, total=False):
    raw_sha256: str
    raw_file: str  # Relative to its run directory; immutable content-addressed copy.
