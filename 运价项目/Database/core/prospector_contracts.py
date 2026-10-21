"""Prospector user authorization contract (no IO)."""
from typing import TypedDict
CLIENT_ID = 'prospector'
SCOPES = ('rates:read', 'space:read', 'images:read')
AUTHORIZE_ROUTE = '/api/prospector/authorize'
TOKEN_ROUTE = '/api/prospector/token'
ME_ROUTE = '/api/prospector/me'
OPTIONS_ROUTE = '/api/prospector/options'
WEB_MATCH = 'web'
LOGOUT_ROUTE = '/api/prospector/logout'
POST_ROUTES = frozenset({AUTHORIZE_ROUTE, TOKEN_ROUTE, LOGOUT_ROUTE})
READ_ROUTES = frozenset({'/api/rates','/api/space','/api/rates_grouped','/api/space_grouped',ME_ROUTE,OPTIONS_ROUTE})
ACCESS_TTL_SECONDS = 900
CODE_TTL_SECONDS = 120
REFRESH_TTL_SECONDS = 2592000
RANDOM_BYTES = 32
PKCE_MIN_LENGTH = 43
PKCE_MAX_LENGTH = 128
MAX_STATE_LENGTH = 512
PURGE_BATCH_SIZE = 256
MIN_PORT = 1
MAX_PORT = 65535
PRIVATE_TABLES = frozenset({'prospector_codes','prospector_tokens','prospector_grants'})
DDL = {
'prospector_grants': '''CREATE TABLE IF NOT EXISTS prospector_grants (
 grant_id TEXT PRIMARY KEY,revoked INTEGER NOT NULL DEFAULT 0,expires_at INTEGER NOT NULL)''',
'prospector_codes': '''CREATE TABLE IF NOT EXISTS prospector_codes (
 code_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,security_hash TEXT NOT NULL,
 redirect_uri TEXT NOT NULL,challenge TEXT NOT NULL,expires_at INTEGER NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0)''',
'prospector_tokens': '''CREATE TABLE IF NOT EXISTS prospector_tokens (
 token_hash TEXT PRIMARY KEY,grant_id TEXT NOT NULL,user_id TEXT NOT NULL,
 security_hash TEXT NOT NULL,kind TEXT NOT NULL,expires_at INTEGER NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0)'''
}
class ProspectorUser(TypedDict):
    user_id: str
    name: str
    display_name: str
    role: str
class TokenResponse(TypedDict):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    user: ProspectorUser

# User-approved HTTP scope: literal private LAN and loopback addresses only.
HTTP_PRIVATE_NETWORKS = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '::1/128')
