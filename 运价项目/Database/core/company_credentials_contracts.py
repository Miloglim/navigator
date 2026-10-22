"""Contracts for local-only company gateway credential management."""

COMPANY_CREDENTIAL_ROUTE = "/api/admin/company-credentials"
COMPANY_TOKEN_REFRESH_ROUTE = "/api/admin/company-token-refresh"
COMPANY_TOKEN_REFRESH_NOW_ROUTE = "/api/admin/company-token-refresh/now"
COMPANY_TOKEN_REFRESH_ENABLE_ROUTE = "/api/admin/company-token-refresh/enable"
COMPANY_TOKEN_REFRESH_DISABLE_ROUTE = "/api/admin/company-token-refresh/disable"
COMPANY_TOKEN_REFRESH_CONNECT_ROUTE = "/api/admin/company-token-refresh/connect"
KB_BROWSER_LOGIN_URL = "https://knowledge.iyunquna.com/me"
KB_AUTH_REFRESH_URL = "https://knowledge.iyunquna.com/api/auth/refresh"
KB_TEST_TOKEN_URL = "https://knowledge.iyunquna.com/api/me/test-token"
KB_DINGTALK_START_URL = "https://knowledge.iyunquna.com/api/auth/dingtalk/start"
KB_DINGTALK_STATUS_URL = "https://knowledge.iyunquna.com/api/auth/dingtalk/status"
KB_TOKEN_LIFETIME_SECONDS = 24 * 60 * 60
KB_REFRESH_EARLY_SECONDS = 3 * 60 * 60
KB_REFRESH_CHECK_SECONDS = 60 * 60
KB_HTTP_TIMEOUT_SECONDS = 20
KB_BROWSER_LOGIN_TIMEOUT_SECONDS = 300
KB_BROWSER_POLL_SECONDS = 2
KB_BROWSER_START_TIMEOUT_SECONDS = 15
KB_BROWSER_PROFILE_DIR = "kb-login-browser"
COMPANY_TOKEN_REFRESH_STORE_FILE = "company-token-refresh.bin"
COMPANY_CREDENTIAL_TOKEN_MAX_LENGTH = 8192
COMPANY_CREDENTIAL_USER_ID_MAX_LENGTH = 32
COMPANY_CREDENTIAL_STORE_DIR = "MiloginNavigator"
COMPANY_CREDENTIAL_STORE_FILE = "company-rate-credentials.bin"
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
