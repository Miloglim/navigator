"""Stable contracts for optional company-rate lookups."""
from typing import Literal, TypedDict


class CompanyMappingForm(TypedDict):
    kind: Literal['port', 'carrier']
    local_name: str
    company_id: int
    company_code: str
    company_name: str
    country: str
    status: Literal['pending', 'confirmed', 'disabled']
    note: str


COMPANY_MAPPING_FIELD_MAX_LENGTH = 1000
COMPANY_PORT_CODE_LENGTH = 5

COMPANY_RATE_QUERY_ROUTE = "/api/company-rates/query"
COMPANY_RATE_TASK_ROUTE = "/api/company-rates/tasks"
COMPANY_REALTIME_QUERY_ROUTE = "/api/company-rates/realtime"
COMPANY_MAPPING_ROUTE = "/api/admin/company-mappings"
COMPANY_MAPPING_SAVE_ROUTE = COMPANY_MAPPING_ROUTE + "/save"
COMPANY_MAPPING_PROBE_ROUTE = COMPANY_MAPPING_ROUTE + "/probe"

COMPANY_RATE_MAX_PORT_PAIRS = 20
COMPANY_RATE_CACHE_SECONDS = 15 * 60
COMPANY_REALTIME_CACHE_SECONDS = 5 * 60
COMPANY_RATE_HTTP_TIMEOUT_SECONDS = 15
COMPANY_RATE_MAX_WORKERS = 4
COMPANY_RATE_MAX_DETAIL_QUERIES = 20
COMPANY_RATE_MAX_PAGES = 20
COMPANY_RATE_DAY_MILLISECONDS = 24 * 60 * 60 * 1000
COMPANY_MAPPING_CACHE_SECONDS = 24 * 60 * 60
COMPANY_MAPPING_CACHE_MAX_ENTRIES = 512
COMPANY_REALTIME_POLL_SECONDS = 3
COMPANY_REALTIME_BUDGET_SECONDS = 120
COMPANY_REALTIME_CONTAINER_TYPES = {1: "20GP", 11: "40GP", 12: "40HQ/HC", 20: "40NOR"}

COMPANY_GATEWAY_URL = "https://knowledge.iyunquna.com/api/http-dispatch"
COMPANY_RATE_TARGET_URL = "http://20045.pro.yqn.corp:20045/freight_charge_api/get_product_list"
COMPANY_PORT_MATCH_TARGET_URL = "http://37003.pro.yqn.corp:37003/gis/portAndDistrictSearch"
COMPANY_CITY_PORTS_TARGET_URL = "http://9870.pro.yqn.corp:9870/api/call/gis/common/hierarchical_query"
COMPANY_CARRIER_MATCH_TARGET_URL = "http://22017.pro.yqn.corp:22017/match/carrier"
COMPANY_REALTIME_CREATE_TARGET_URL = "http://20045.pro.yqn.corp:20045/api/call/yqn_pms/online/inquiry/v2/createIds"
COMPANY_REALTIME_POLL_TARGET_URL = "http://20045.pro.yqn.corp:20045/api/call/yqn_pms/online/inquiry/v2/get_online_list"

COMPANY_RATE_KINDS = frozenset({"company", "realtime"})
COMPANY_RATE_STATUSES = frozenset({"queued", "running", "complete", "partial", "failed", "timeout"})

COMPANY_RATE_DDL = {
    "company_mappings": """CREATE TABLE IF NOT EXISTS company_mappings (
        business_user_id BIGINT NOT NULL, kind TEXT NOT NULL, local_name TEXT NOT NULL,
        company_id BIGINT NOT NULL, company_code TEXT, company_name TEXT, country TEXT,
        status TEXT NOT NULL DEFAULT 'pending', note TEXT, updated_by TEXT,
        updated_at INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(business_user_id,kind,local_name),
        CHECK(kind IN ('port','carrier')), CHECK(status IN ('pending','confirmed','disabled')),
        CHECK(company_id>0))""",
    "company_rate_tasks": """CREATE TABLE IF NOT EXISTS company_rate_tasks (
        task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
        query_signature TEXT NOT NULL, query_json TEXT NOT NULL,
        status TEXT NOT NULL, error TEXT, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK (kind IN ('company','realtime')),
        CHECK (status IN ('queued','running','complete','partial','failed','timeout')))""",
    "company_rate_results": """CREATE TABLE IF NOT EXISTS company_rate_results (
        task_id TEXT NOT NULL, pair_key TEXT NOT NULL, items_json TEXT NOT NULL,
        error TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        PRIMARY KEY (task_id,pair_key))""",
}

