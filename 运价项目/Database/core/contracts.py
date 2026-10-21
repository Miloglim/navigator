"""Stable HTTP contracts for new transport modules.

Existing dashboard endpoints keep their compatibility payloads. New endpoints
must use these helpers so callers always receive ``ok`` and ``data``/``error``.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, TypedDict, Literal, NotRequired

API_VERSION = "2026-09"
ALIAS_PAGE_SIZE = 50
ALIAS_PAGE_MAX_SIZE = 200


class AliasPage(TypedDict):
    rows: list[Dict[str, Any]]
    total: int
    limit: int
    offset: int

CONTEXT_ALIAS_APPROVAL_NOTE = "仅对本发送人、本船司、本群生效"
CONTEXT_ALIAS_SCOPE = "sender+carrier+group"
SUBMISSION_COVERAGE_VERSION = 1
SUBMISSION_IDENTITY_FIELDS = ('来源群', '消息ID', '发送人', '消息时间', '消息原文', '起运港', '目的港', '船司', '柜型',
    '航线服务', '目的码头', '船期ETD', '有效期起', '有效期止', '船名航次', '舱位类型',
    '箱量', '目免', '亏舱费', 'T/T航程', '备注')
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_SALES = "sales"
ACCOUNT_ROLES = frozenset({ROLE_ADMIN, ROLE_OPERATOR, ROLE_SALES})
AUTH_LOGIN_ROUTE = "/api/auth/login"
AUTH_LOGOUT_ROUTE = "/api/auth/logout"
AUTH_SETUP_ROUTE = "/api/auth/setup"
AUTH_STATUS_ROUTE = "/api/auth/status"
LIVENESS_ROUTE = "/api/health/live"
CENTER_NAVIGATOR_DASHBOARD_ROUTE = "/api/center/navigator/dashboard"
SALES_SCOPED_GET = frozenset({"/api/rates", "/api/space", "/api/rates_grouped", "/api/space_grouped", "/api/sales/groups", "/api/pending", "/api/audit", "/api/audit-hints", "/api/group-stats", "/api/queue-stats"})
SALES_GROUP_SUMMARY_ROUTE = "/api/sales/groups"
SALES_GROUP_VISIT_ROUTE = "/api/sales/groups/visit"
SALES_GROUP_NAME_MAX_LENGTH = 512

class SalesGroupVisitRequest(TypedDict):
    name: str

class SalesGroupVisitResult(TypedDict):
    click_count: int

DISPLAY_MODE_PARAM = "display_mode"
DISPLAY_MODE_PREFERRED = "preferred"
DISPLAY_ROUTES_PARAM = "display_routes"
DISPLAY_GROUPS_PARAM = "display_groups"
OPS_OWNERSHIP_GET = frozenset({'/api/pending','/api/audit','/api/audit-hints','/api/group-stats','/api/queue-stats'})
OWNERSHIP_QUERY_PARAM = 'ownership'
OWNERSHIP_MINE = 'mine'
OWNERSHIP_ALL = 'all'

class OwnershipQuery(TypedDict, total=False):
    ownership: Literal['mine','all']
LOGIN_ACCOUNT_ATTEMPTS = 8
LOGIN_IP_ATTEMPTS = 600
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_TRACKED_KEYS = 10000
LOGIN_MAX_INFLIGHT = 8
LOGIN_RETRY_SECONDS = 1
HTTP_MAX_ACTIVE_REQUESTS = 32
HTTP_REQUEST_TIMEOUT_SECONDS = 30
AUTH_REQUEST_MAX_BYTES = 8192

AUTH_REGISTER_ROUTE = "/api/auth/register"
AUTH_FORGOT_ROUTE = "/api/auth/forgot-password"
AUTH_PASSWORD_ROUTE = "/api/auth/change-password"
ACCOUNT_RECOVERY_ROUTE = "/api/account/recovery-requests"
ACCOUNT_PROFILE_ROUTE = "/api/account/profile"
ACCOUNT_GROWTH_LIQUID_PREF = "growth_liquid_enabled"
ACCOUNT_GROWTH_LIQUID_DEFAULT = True
ACCOUNT_AVATAR_PREF = "avatar_id"
ACCOUNT_COMPANY_RATES_PREF = "company_rates_enabled"
ACCOUNT_CARRIER_REALTIME_PREF = "carrier_realtime_enabled"
ACCOUNT_AVATAR_LEGACY_IDS = tuple(f"av-{index:02d}" for index in range(1, 31))
ACCOUNT_AVATAR_SEXES = (("w", "女"), ("m", "男"))
ACCOUNT_AVATAR_PEOPLE_PER_SEX = 5
ACCOUNT_AVATAR_LOOKS = ("日常", "眼镜", "出行")
ACCOUNT_AVATAR_IDS = tuple(
    f"av2-{sex}{person:02d}-{look}"
    for sex, _ in ACCOUNT_AVATAR_SEXES
    for person in range(1, ACCOUNT_AVATAR_PEOPLE_PER_SEX + 1)
    for look in range(1, len(ACCOUNT_AVATAR_LOOKS) + 1)
)
ACCOUNT_AVATAR_ALL_IDS = ACCOUNT_AVATAR_LEGACY_IDS + ACCOUNT_AVATAR_IDS


def account_avatar_groups():
    return [{"sex": sex, "label": label, "people": [
        {"label": f"人物 {person:02d}", "looks": [
            {"id": f"av2-{sex}{person:02d}-{look}", "label": look_label}
            for look, look_label in enumerate(ACCOUNT_AVATAR_LOOKS, 1)
        ]} for person in range(1, ACCOUNT_AVATAR_PEOPLE_PER_SEX + 1)
    ]} for sex, label in ACCOUNT_AVATAR_SEXES]
ACCOUNT_PREFERENCE_MAX_ITEMS = 50
ACCOUNT_DISPLAY_NAME_MAX_LENGTH = 64
ACCOUNT_CONTAINER_TYPES = ("20GP", "40HQ/HC", "40NOR", "45HQ/HC")
ACCOUNT_ROUTE_REGION_ORDER = ("欧地线", "美加线", "拉美线", "亚洲线", "澳新线", "非洲线")


class AccountPreferences(TypedDict):
    routes: list[str]
    groups: list[str]
    ctypes: list[str]  # Deprecated personal preference; always empty. Sales query ctype remains supported.
    start_tab: str
    growth_liquid_enabled: bool
    avatar_id: str | None
    company_rates_enabled: bool
    carrier_realtime_enabled: bool


class AccountRouteSection(TypedDict):
    name: str
    routes: list[str]


class AccountProfileUpdate(TypedDict):
    display_name: str
    preferences: AccountPreferences

ACCOUNT_NAME_MIN_LENGTH = 2
ACCOUNT_NAME_MAX_LENGTH = 64
ACCOUNT_PASSWORD_MIN_LENGTH = 6
ACCOUNT_PASSWORD_MAX_LENGTH = 128


class AccountRegistration(TypedDict):
    name: str
    password: str
    confirm_password: str


class AccountPasswordChange(TypedDict):
    current_password: str
    new_password: str
    confirm_password: str


class AccountRecoveryRequest(TypedDict):
    name: str


class AccountIdentity(TypedDict):
    user_id: str
    name: str
    role: str
    views: list[str]
    display_name: str


class AccountDirectoryPerson(TypedDict):
    user_id: str
    name: str
    role: str
    active: bool
    has_password: bool
    login_enabled: bool
    publisher_groups: list[str]

PENDING_REJECT_GROUP_ROUTE = "/api/pending/reject_group"
REVIEW_ISSUES_ROUTE = "/api/review-issues"
REVIEW_ISSUES_RESOLVE_ROUTE = "/api/review-issues/resolve"
AGENT_REVIEW_ISSUES_SETTLE_ROUTE = "/api/agent/review-issues/settle"


class ReviewIssueSettlement(TypedDict):
    source_group: str
    message_id: str
    pending_ids: list[int]
    expected_count: int
    classification: NotRequired[str]
    reason: NotRequired[str]
    media_ids: NotRequired[list[str]]


class ReviewIssue(TypedDict):
    source_group: str
    message_id: str
    message_text: str
    media_ids: list[str]
    reasons: list[str]

PENDING_PRESERVE_OTHER_SOURCES = "preserve_other_sources"
PENDING_PRESERVE_EXISTING_SOURCE_ROWS = "preserve_existing_source_rows"


class PendingSubmitRequest(TypedDict, total=False):
    table: str
    origin: str
    records: list[Dict[str, Any]]
    preserve_other_sources: bool  # Per-source dedup; defaults on for identified Agent records.
    preserve_existing_source_rows: bool  # Add missing products without replacing audited rows in that source.


class PendingRejectGroupRequest(TypedDict, total=False):
    ids: list[int]  # Exact awaiting rows displayed in the source card.
    group_key: Dict[str, str]  # Legacy clients only.
    kind: str
    note: str
    reviewed_by: str


SYSTEM_HEALTH_ROUTE = "/api/system/health"
SYSTEM_READINESS_ROUTE = "/api/system/readiness"
RELEASE_DECISION_ROUTE = "/api/release/decide"
MONITORING_SUMMARY_ROUTE = "/api/monitoring/summary"
MONITORING_GROUPS_ROUTE = "/api/monitoring/groups"
MONITORING_RUNS_ROUTE = "/api/monitoring/runs"

HEALTH_OK = "ok"
HEALTH_ATTENTION = "attention"
HEALTH_BLOCKED = "blocked"


def ok(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the success envelope mandated for newly introduced APIs."""
    return {"ok": True, "data": dict(data)}


def fail(error: str, code: str) -> Dict[str, Any]:
    """Build a machine-readable failure envelope without leaking internals."""
    return {"ok": False, "error": error, "code": code}


# Additive sales filters: legacy valid remains a single-day overlap query.
SALES_VALID_SINCE_PARAM = "valid_since"
PORT_ROUTES_FIELD = "routes_list"
PORT_ROUTES_COMPACT_FIELD = "rs"
MASTER_SAVE_ROUTE = "/api/master/save"
PORT_ROUTES_TABLE = "port_routes"


class PortRouteFields(TypedDict, total=False):
    route: str  # Legacy compatibility mirror, comma-separated canonical names.
    routes_list: list[str]  # Authoritative membership list; [] clears all.


class SalesQueryFilters(TypedDict, total=False):
    ctype: str  # Comma-separated canonical types; alternatives use OR.
    route: str  # Exact canonical route; space queries also match an exact service name.
    group: str  # One exact registered source-group name.
    valid_since: str  # ISO date; validity overlaps this date or any later date.


OPS_ID_QUERY_PARAM = "id_query"
SOURCE_MESSAGE_ID_PREFIX = "msg//"
OPS_ENTRY_ID_PARAM = "entry_id"
OPS_MESSAGE_ID_PARAM = "message_id"


class OpsIdQueryFilters(TypedDict, total=False):
    id_query: str  # Matches entry ID OR normalized source message ID.
    entry_id: str  # Exact stable freight entry identity.
    message_id: str  # Exact source message identity; derived group, no new stored state.

# Source view pages complete messages; entry view retains record pagination.
PENDING_SOURCE_VIEW = "source"
PENDING_SOURCE_MAX_GROUPS = 50
PENDING_WHOLE_PASSED_PARAM = "whole_passed"


class PendingBulkApprovalResult(TypedDict):
    ok: bool
    total: int
    approved: int
    skipped: list[Dict[str, Any]]
    remaining_passed: int  # Eligible rows still awaiting in wholly passed sources.
    held_by_group: int  # Passed rows held by a failed or out-of-scope sibling.

# Independent products on the same port pair must survive staging and release.
QUOTE_VARIANT_FIELDS = {"航线服务": "service_name", "目的码头": "destination_terminal"}
SPACE_DESTINATION_SCOPE_FIELDS = ("目的港", "航线", "航线服务")


class QuoteVariantFields(TypedDict, total=False):
    service_name: str  # Explicit carrier service; empty preserves legacy behavior.
    destination_terminal: str  # Explicit terminal within the destination port.


class PendingSourceListResult(TypedDict):
    ok: bool
    total: int  # Number of matching source messages.
    record_total: int  # Records in all matching messages.
    pagination_unit: str
    rows: list[dict[str, Any]]  # Whole messages on this page; original checks retained.


# Evidence-backed additive master repair; existing entities are never overwritten.
AGENT_MASTER_ENSURE_ROUTE = "/api/agent/master/ensure"
AGENT_PENDING_REPAIR_ROUTE = "/api/agent/pending/repair"
AGENT_PENDING_SUPPRESS_ROUTE = "/api/agent/pending/suppress"
AGENT_PENDING_REPAIR_FIELDS = frozenset({"起运港", "目的港", "船司", "航线", "柜型",
    "海运费USD", "价格USD", "有效期起", "有效期止", "有效期船期", "目免", "备注",
    "航线服务", "目的码头", "船期ETD", "船名航次", "舱位类型", "AI判断"})
AGENT_MASTER_ENTITIES = frozenset({"port", "alias"})


class AgentRepairEvidence(TypedDict):
    kind: str  # official_source / group_spec / original_image
    reference: str
    reason: str


class AgentMasterEnsureRequest(TypedDict):
    entity: str
    source_group: str
    message_id: str
    row: Dict[str, Any]
    evidence: list[AgentRepairEvidence]


class AgentPendingRepairRequest(TypedDict, total=False):
    kind: str  # Optional source-backed rates/space correction; review state stays awaiting.
    id: int
    expected_signature: str
    fields: Dict[str, Any]
    evidence: list[AgentRepairEvidence]
    retain_failed: bool  # Image-backed or text-source correction may remain failed; cannot release.

PUBLISHER_ACCOUNTS_ROUTE = "/api/publishers/accounts"


class PublisherDecisionRequest(TypedDict):
    candidate_id: str
    action: str  # confirm is group-scoped; exclude/reset use the global person denylist.
    expected_revision: int
    note: str


class PublisherAccountLibraryStatus(TypedDict, total=False):
    registered: bool
    ambiguous: bool
    user_id: str
    login_name: str
    role: str
    active: bool
    has_password: bool
    login_enabled: bool


class PublisherDecisionResult(TypedDict, total=False):
    revision: int
    action: str
    unchanged: bool  # Repeating the current decision must not bump roster revision or audit.
    account_created: int
    account_sync_pending: bool

# Sales sorting is applied to the entire filtered result before pagination.
SALES_SORT_PARAM = "sort"
SALES_SORT_DIRECTION_PARAM = "direction"
SALES_SORT_FIELDS = frozenset({"etd", "validity", "synced_at", "price",
                               "price_20gp", "price_40hq", "price_40nor", "price_45hq"})
SALES_SORT_DIRECTIONS = frozenset({"asc", "desc"})

SALES_45_HIGH_CUBE_TYPE = "45HQ/HC"
SALES_45_HIGH_CUBE_ALIASES = frozenset({SALES_45_HIGH_CUBE_TYPE, "45HQ", "45HC"})
SALES_45_HIGH_CUBE_FIELDS = ("container_type", "box_desc", "remark", "message_text")
SALES_45_HIGH_CUBE_PATTERN = r"(?:^|[^A-Za-z0-9])45\s*['’′\"]?\s*(?:HQ|HC)(?![A-Za-z0-9])|45\s*(?:英尺|尺)\s*(?:高柜|高箱)"


class SalesSortRequest(TypedDict, total=False):
    sort: str
    direction: str
