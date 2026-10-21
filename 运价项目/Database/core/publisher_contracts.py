"""Stable quote ownership contracts. Names are labels, never external identity keys."""
from typing import List, TypedDict

PUBLISHER_BIND_ROUTE = '/api/admin/account/publisher'
PUBLISHER_TABLES = ('quote_publishers', 'publisher_sources', 'publisher_legacy_names', 'publisher_dingtalk_ids')
SOURCE_FIELDS = {'来源群ID': 'source_group_id', '发送人ID': 'source_sender_id',
                 '发送人OpenID': 'source_sender_open_id'}
PUBLISHER_DDL = {
    'publisher_dingtalk_ids': '''CREATE TABLE IF NOT EXISTS publisher_dingtalk_ids (
        identity_key TEXT PRIMARY KEY, publisher_id TEXT NOT NULL, created_at TEXT NOT NULL)''',
    'quote_publishers': '''CREATE TABLE IF NOT EXISTS quote_publishers (
        publisher_id TEXT PRIMARY KEY, publisher_name TEXT NOT NULL, created_at TEXT NOT NULL,
        merged_into TEXT)''',
    'publisher_sources': '''CREATE TABLE IF NOT EXISTS publisher_sources (
        source_key TEXT PRIMARY KEY, publisher_id TEXT NOT NULL, source_group TEXT,
        sender_name TEXT, identity_kind TEXT NOT NULL, created_at TEXT NOT NULL)''',
    'publisher_legacy_names': '''CREATE TABLE IF NOT EXISTS publisher_legacy_names (
        sender_name TEXT PRIMARY KEY, publisher_id TEXT NOT NULL)''',
}

class PublisherBindingRequest(TypedDict):
    user_id: str
    publisher_id: str
    expected_signature: str


class PublisherDecisionRequest(TypedDict, total=False):
    candidate_id: str
    action: str
    expected_revision: int
    note: str


class PublisherDecisionResult(TypedDict, total=False):
    revision: int
    action: str
    unchanged: bool
    identity_keys: List[str]
    account_created: int
    account_removed: int
    account_sync_pending: bool
    account_cleanup_pending: bool
