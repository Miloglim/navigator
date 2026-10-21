"""Administrator terminal contracts; business ownership remains unchanged."""
from typing import Literal, TypedDict, Dict, Any

ADMIN_ACCOUNTS = '/api/admin/accounts'
ADMIN_ACCOUNT = '/api/admin/account'
ADMIN_PROFILE = '/api/admin/account/profile'
ADMIN_SECURITY = '/api/admin/account/security'
ADMIN_EVENTS = '/api/admin/account/events'
ADMIN_RECORDS = '/api/admin/account/records'
from core.publisher_contracts import PUBLISHER_BIND_ROUTE
ADMIN_GET_ROUTES = frozenset({ADMIN_ACCOUNTS, ADMIN_ACCOUNT, ADMIN_EVENTS, ADMIN_RECORDS, PUBLISHER_BIND_ROUTE})
ADMIN_POST_ROUTES = frozenset({ADMIN_ACCOUNTS, ADMIN_PROFILE, ADMIN_SECURITY, PUBLISHER_BIND_ROUTE})
ADMIN_TABLES = ('account_profiles', 'account_security', 'account_events')
ADMIN_PAGE_SIZE = 20
ADMIN_PAGE_LIMIT = 100
ADMIN_TEXT_LIMIT = 120
ADMIN_NOTE_LIMIT = 2000
ADMIN_LOGIN_FAILURE_WINDOW_SECONDS = 86400
MIN_CAREER_YEAR = 1900
PROFILE_FIELDS = frozenset({'display_name','employee_no','department','position','work_email','work_phone','joined_on','employment'})
EXPERIENCE_FIELDS = frozenset({'career_start_year','expertise_routes','expertise_carriers','skills','notes'})
SECURITY_ACTIONS = frozenset({'activate','disable','enable','role','rename_login','reset_password','require_password_change','revoke_sessions'})
PASSWORD_CHANGE_REQUIRED = 'password_change_required'
ADMIN_SERVER_FIELDS = frozenset({'_actor','_actor_id','_role','_actor_display'})

class AdminProfileRequest(TypedDict):
    user_id: str
    section: Literal['profile','experience']
    fields: Dict[str, Any]
    expected_signature: str

class AdminSecurityRequest(TypedDict, total=False):
    user_id: str
    action: str
    expected_signature: str
    role: str
    new_name: str
    new_password: str
    confirm_password: str

ADMIN_DDL = {
 'account_profiles': """CREATE TABLE IF NOT EXISTS account_profiles (
    user_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL DEFAULT '{}',
    experience_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT, updated_by TEXT)""",
 'account_security': """CREATE TABLE IF NOT EXISTS account_security (
    user_id TEXT PRIMARY KEY, must_change_password INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
    CHECK(must_change_password IN (0,1)))""",
 'account_events': """CREATE TABLE IF NOT EXISTS account_events (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, actor_id TEXT NOT NULL,
    actor_name TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL)"""
}
