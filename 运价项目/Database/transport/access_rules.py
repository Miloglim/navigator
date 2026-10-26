"""Explicit HTTP capability map. Unlisted routes fail closed."""
from core.contracts import ACCOUNT_RECOVERY_ROUTE, ACCOUNT_PROFILE_ROUTE
from core.master_resolution_contracts import MASTER_RESOLVE_ROUTE
from core.agent_intake_contracts import AGENT_INTAKE_ROUTE
from core.contracts import AGENT_REVIEW_ISSUES_SETTLE_ROUTE, AGENT_REVIEW_ISSUES_NOISE_ROUTE
from core.growth_contracts import GROWTH_ROUTE, GROWTH_SEARCH_ROUTE
from core.admin_contracts import ADMIN_GET_ROUTES, ADMIN_POST_ROUTES
from core.notice_contracts import NOTICES_ROUTE, NOTICE_SAVE_ROUTE, NOTICE_DELETE_ROUTE, NOTICE_PIN_ROUTE
from core.message_board_contracts import MESSAGE_BOARD_ROUTE, MESSAGE_BOARD_CREATE_ROUTE
from core.notification_contracts import NOTIFICATION_GET_ROUTES, NOTIFICATION_POST_ROUTES
from core.feedback_contracts import FEEDBACK_ROUTE, FEEDBACK_DISMISS_ROUTE
from core.activity_contracts import (ACTIVITY_EVENTS_ROUTE, ACTIVITY_PAGE_OPEN_ROUTE,
                                     ACTIVITY_QUOTE_USAGE_ROUTE, ACTIVITY_SUMMARY_ROUTE,
                                     ACTIVITY_TIMELINE_ROUTE)
from core.api_monitor_contracts import API_EVENTS_ROUTE, API_HEALTH_ROUTE, API_SUMMARY_ROUTE
from core.download_contracts import DOWNLOAD_MANIFEST_ROUTE
from core.company_rates_contracts import (COMPANY_RATE_QUERY_ROUTE, COMPANY_RATE_TASK_ROUTE,
                                          COMPANY_REALTIME_QUERY_ROUTE, COMPANY_MAPPING_ROUTE,
                                          COMPANY_MAPPING_SAVE_ROUTE, COMPANY_MAPPING_PROBE_ROUTE)
from core.company_credentials_contracts import (COMPANY_CREDENTIAL_ROUTE, COMPANY_TOKEN_REFRESH_ROUTE,
    COMPANY_TOKEN_REFRESH_NOW_ROUTE, COMPANY_TOKEN_REFRESH_ENABLE_ROUTE, COMPANY_TOKEN_REFRESH_DISABLE_ROUTE,
    COMPANY_TOKEN_REFRESH_CONNECT_ROUTE)
NOTICE_POST = frozenset({NOTICE_SAVE_ROUTE, NOTICE_DELETE_ROUTE, NOTICE_PIN_ROUTE})
from core.contracts import (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_OPERATOR, ROLE_SALES, ADMIN_ROLES, AUTHENTICATED_ROLES, AUTH_STATUS_ROUTE,
                            LIVENESS_ROUTE, PUBLISHER_ACCOUNTS_ROUTE, AGENT_MASTER_ENSURE_ROUTE, AGENT_PENDING_REPAIR_ROUTE, AGENT_PENDING_SUPPRESS_ROUTE, SYSTEM_HEALTH_ROUTE, SYSTEM_READINESS_ROUTE,
                            MONITORING_SUMMARY_ROUTE, MONITORING_GROUPS_ROUTE,
                            MONITORING_RUNS_ROUTE, RELEASE_DECISION_ROUTE, REVIEW_ISSUES_ROUTE, REVIEW_ISSUES_RESOLVE_ROUTE, SALES_GROUP_SUMMARY_ROUTE,
                            SALES_GROUP_VISIT_ROUTE, ok, fail)

SALES_GET = frozenset({"/api/rates", "/api/space", "/api/rates_grouped", "/api/space_grouped",
                       SALES_GROUP_SUMMARY_ROUTE, FEEDBACK_ROUTE, COMPANY_RATE_TASK_ROUTE,
                       "/api/master", "/api/options", "/api/bootstrap", "/api/stats"})
OPS_GET = frozenset({"/api/pending", REVIEW_ISSUES_ROUTE, "/api/audit", "/api/audit-hints", "/api/group-stats",
                     "/api/queue-stats"})
ADMIN_GET = ADMIN_GET_ROUTES | frozenset({ACCOUNT_RECOVERY_ROUTE, PUBLISHER_ACCOUNTS_ROUTE, "/api/operators", "/api/chat-groups", "/api/export", "/api/alias",
                       "/api/shadow", "/api/candidates", SYSTEM_READINESS_ROUTE,
                       MONITORING_SUMMARY_ROUTE, MONITORING_GROUPS_ROUTE, MONITORING_RUNS_ROUTE,
                       ACTIVITY_SUMMARY_ROUTE, ACTIVITY_EVENTS_ROUTE, ACTIVITY_TIMELINE_ROUTE})
ADMIN_GET = ADMIN_GET | frozenset({API_EVENTS_ROUTE, API_HEALTH_ROUTE, API_SUMMARY_ROUTE})
ADMIN_GET = ADMIN_GET | frozenset({COMPANY_CREDENTIAL_ROUTE, COMPANY_TOKEN_REFRESH_ROUTE})
ADMIN_GET = ADMIN_GET | frozenset({"/api/memory-lookup"})
OPS_POST = frozenset({"/api/push", "/api/validate", "/api/record/update", "/api/record/revise",
                      "/api/record/delete", "/api/pending", "/api/pending/approve",
                      "/api/pending/approve_all", "/api/pending/update", "/api/pending/sync",
                      "/api/pending/reject", "/api/pending/rework", "/api/pending/restore",
                      "/api/pending/delete", "/api/pending/update_group",
                      "/api/pending/approve_group", "/api/pending/reject_group", REVIEW_ISSUES_RESOLVE_ROUTE})
ADMIN_POST = ADMIN_POST_ROUTES | frozenset({PUBLISHER_ACCOUNTS_ROUTE, "/api/import", "/api/master/save", "/api/master/delete", "/api/alias/save",
                        "/api/alias/delete", "/api/shadow/replay", "/api/shadow/clear",
                        "/api/candidates/approve", "/api/candidates/reject", "/api/candidates/scan",
                        "/api/operator/save", "/api/operator/delete", "/api/chat-group/save",
                        "/api/chat-group/delete", "/api/chat-groups/import", "/api/sync",
                        "/api/pending/regroup", AGENT_MASTER_ENSURE_ROUTE, AGENT_PENDING_REPAIR_ROUTE, AGENT_PENDING_SUPPRESS_ROUTE, RELEASE_DECISION_ROUTE})
ADMIN_POST = ADMIN_POST | frozenset({COMPANY_CREDENTIAL_ROUTE, COMPANY_TOKEN_REFRESH_ROUTE,
    COMPANY_TOKEN_REFRESH_NOW_ROUTE, COMPANY_TOKEN_REFRESH_ENABLE_ROUTE, COMPANY_TOKEN_REFRESH_DISABLE_ROUTE,
    COMPANY_TOKEN_REFRESH_CONNECT_ROUTE})
ADMIN_POST = ADMIN_POST | frozenset({REVIEW_ISSUES_ROUTE, AGENT_REVIEW_ISSUES_SETTLE_ROUTE, "/api/agent/review-issues/noise"})
AGENT_GET = SALES_GET | frozenset({"/api/pending", "/api/memory-lookup"})
AGENT_POST = frozenset({MASTER_RESOLVE_ROUTE, AGENT_INTAKE_ROUTE, "/api/validate", "/api/pending", REVIEW_ISSUES_ROUTE, AGENT_REVIEW_ISSUES_SETTLE_ROUTE, AGENT_REVIEW_ISSUES_NOISE_ROUTE, AGENT_MASTER_ENSURE_ROUTE, AGENT_PENDING_REPAIR_ROUTE, AGENT_PENDING_SUPPRESS_ROUTE})
PUBLIC_GET = frozenset({AUTH_STATUS_ROUTE, LIVENESS_ROUTE, DOWNLOAD_MANIFEST_ROUTE})


def authorize(method, path, role=None, agent=False):
    if path in (COMPANY_MAPPING_ROUTE, COMPANY_MAPPING_SAVE_ROUTE, COMPANY_MAPPING_PROBE_ROUTE):
        if role in ADMIN_ROLES and ((method == 'GET' and path == COMPANY_MAPPING_ROUTE) or
                                   (method == 'POST' and path in (COMPANY_MAPPING_SAVE_ROUTE, COMPANY_MAPPING_PROBE_ROUTE))):
            return ok({'principal':role})
        return fail('仅管理员可维护公司接口映射', 'forbidden' if role or agent else 'unauthenticated')
    if role in AUTHENTICATED_ROLES and ((method == 'GET' and path in NOTIFICATION_GET_ROUTES) or
                                                (method == 'POST' and path in NOTIFICATION_POST_ROUTES)):
        return ok({'principal': role})
    if role in AUTHENTICATED_ROLES and ((method == 'GET' and path == NOTICES_ROUTE) or (method == 'POST' and path in NOTICE_POST)):
        return ok({'principal': role})
    if role in AUTHENTICATED_ROLES and ((method == 'GET' and path == MESSAGE_BOARD_ROUTE) or
                                                (method == 'POST' and path == MESSAGE_BOARD_CREATE_ROUTE)):
        return ok({'principal': role})
    if method == "GET" and path in PUBLIC_GET:
        return ok({"principal": "public"})
    if role:
        shared = (method == "GET" and (path in SALES_GET or path in (SYSTEM_HEALTH_ROUTE, ACCOUNT_PROFILE_ROUTE, GROWTH_ROUTE) or path.startswith("/images/"))) or (method == "POST" and path in (MASTER_RESOLVE_ROUTE, GROWTH_SEARCH_ROUTE, SALES_GROUP_VISIT_ROUTE, FEEDBACK_ROUTE, FEEDBACK_DISMISS_ROUTE, ACTIVITY_PAGE_OPEN_ROUTE, ACTIVITY_QUOTE_USAGE_ROUTE, COMPANY_RATE_QUERY_ROUTE, COMPANY_REALTIME_QUERY_ROUTE))
        if role in AUTHENTICATED_ROLES and shared:
            return ok({"principal": role})
        if role in (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_OPERATOR) and ((method == "GET" and path in OPS_GET) or
                                                   (method == "POST" and path in OPS_POST)):
            return ok({"principal": role})
        if role in ADMIN_ROLES and ((method == "GET" and path in ADMIN_GET) or
                                   (method == "POST" and path in ADMIN_POST)):
            return ok({"principal": role})
        return fail("account permission required", "forbidden")
    if agent:
        if (method == "GET" and path in AGENT_GET) or (method == "POST" and path in AGENT_POST):
            return ok({"principal": "agent"})
        return fail("agent permission required", "forbidden")
    return fail("login required", "unauthenticated")
