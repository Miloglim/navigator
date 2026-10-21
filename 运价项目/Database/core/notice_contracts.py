"""Announcement board contracts; no runtime dependencies."""
from typing import TypedDict

NOTICES_ROUTE = '/api/notices'
NOTICE_SAVE_ROUTE = '/api/notices/save'
NOTICE_DELETE_ROUTE = '/api/notices/delete'
NOTICE_PIN_ROUTE = '/api/notices/pin'
NOTICE_PAGE_SIZE = 20
NOTICE_PAGE_MAX = 50
NOTICE_TITLE_MAX = 160
NOTICE_TAG_MAX = 24
NOTICE_TAG_COUNT_MAX = 8
NOTICE_HTML_MAX = 16 * 1024 * 1024
NOTICE_IMAGE_MAX = 2 * 1024 * 1024
NOTICE_IMAGE_COUNT_MAX = 8
NOTICE_REQUEST_MAX = 24 * 1024 * 1024
NOTICE_SCOPE_MAX = 100
NOTICE_SCOPE_COLUMNS = ('target_groups_json', 'target_routes_json')
NOTICE_DDL = """CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body_html TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL,
    target_groups_json TEXT NOT NULL DEFAULT '[]', target_routes_json TEXT NOT NULL DEFAULT '[]',
    author_id TEXT NOT NULL, author_name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_on TEXT,
    pin_state TEXT NOT NULL DEFAULT 'none', pin_requested_by TEXT,
    pin_reviewed_by TEXT, pin_reviewed_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
    CHECK (pin_state IN ('none','pending','approved','rejected')))"""


class NoticeInput(TypedDict, total=False):
    id: str
    revision: int
    title: str
    body_html: str
    group_name: str
    target_groups: list[str]
    target_routes: list[str]
    tags: list[str]
    pin: bool
    expires_on: str | None


class NoticeRow(TypedDict, total=False):
    id: str
    title: str
    body_html: str
    group_name: str
    target_groups: list[str]
    target_routes: list[str]
    tags: list[str]
    author_id: str
    author_name: str
    avatar_id: str | None
    created_at: str
    updated_at: str
    expires_on: str | None
    pin_state: str
    revision: int


class NoticePinInput(TypedDict):
    id: str
    revision: int
    action: str
