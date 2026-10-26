"""Local autonomous review contracts; no business write or approval endpoints."""
from typing import TypedDict
PAGE_SIZE = 500
DEFAULT_BATCH_SIZE = 2
MAX_BATCH_SIZE = 5
LEASE_SECONDS = 1800
MAX_INTERRUPTED_ATTEMPTS = 2
MAX_PAGES = 100
OUTCOMES = frozenset({'reviewed', 'repaired', 'needs_human'})
PENDING = 'pending'
CLAIMED = 'claimed'
MIN_NOTE_LENGTH = 12
class ReviewReport(TypedDict):
    outcome: str
    note: str
    evidence: list[str]

SQLITE_BUSY_TIMEOUT_SECONDS = 30
CLAIM_TOKEN_BYTES = 16
