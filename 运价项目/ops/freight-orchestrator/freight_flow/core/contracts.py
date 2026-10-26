"""Contracts are defined before adapters. Standard-library types only; no IO."""
from typing import TypedDict


class TaskLease(TypedDict):
    id: str
    run_id: str
    stage: str
    attempt: int
    generation: int
    owner: str
    lease_until: float


class Reply(TypedDict, total=False):
    ok: bool
    data: object
    error: str

VERSION = "0.11.1"
HOST = "127.0.0.1"
PORT = 8797
RUNS_PATH = "/api/runs"
HEALTH_PATH = "/api/health"
DETAIL_PATH = "/api/run"
TRACE_ROOTS_PATH = "/api/trace/roots"
TRACE_RUN_PATH = "/api/trace/run"
TRACE_STREAM_PATH = "/api/trace/stream"
TRACE_ACTION_PATH = "/api/trace/action"
TRACE_IMAGE_PATH = "/api/trace/image"
RUNTIME_OVERVIEW_PATH = "/api/runtime"
DWS_LOGIN_PATH = "/api/dws/login"
RUNTIME_SCHEDULE_PATH = "/api/runtime/schedule"
RUNTIME_WORKER_PATH = "/api/runtime/worker"
RUNTIME_TRIGGER_PATH = "/api/runtime/trigger"
STUDIO_PATH = "/studio"
CONCURRENCY = 2
LEASE_SECONDS = 120
PULSE_SECONDS = 10
POLL_SECONDS = 2
AGENT_TIMEOUT_SECONDS = 180
MAX_ATTEMPTS = 3
RETRY_SECONDS = 30
LIST_LIMIT = 100
INFRA_RECOVERY_SECONDS = 900
INFRA_RECOVERY_MAX_SECONDS = 3600
RECOVERY_BACKOFF_MAX_POWER = 2
COALESCED = "COALESCED"
HELD = "HELD"
NEEDS_REVIEW = "NEEDS_REVIEW"
PARTIAL_REVIEW = "PARTIAL_REVIEW"
PARTIAL_REVIEW_COMPLETE = "PARTIAL_REVIEW_COMPLETE"
SCHEDULING_CLOSED = ("SUCCEEDED", "SKIPPED", PARTIAL_REVIEW, COALESCED, HELD, NEEDS_REVIEW)
STOP_FILE = "worker-stop.request"
ERROR_SAMPLE_LIMIT = 3
STAGES = ("classify", "vision", "extract", "inventory", "coverage", "audit")
PRODUCTION_BATCH_STAGES = ("pull", "fanout", "reconcile", "watermark")
HISTORY_BATCH_STAGES = ("pull", "fanout", "reconcile", "history_cursor")
SCHEDULED_SHARDS = ("A", "B", "C", "D")
PRODUCTION_MESSAGE_STAGES = ("prepare", *STAGES, "normalize", "validate", "submit", "readback")
DWS_TIMEOUT_SECONDS = 120
DWS_PAGE_LIMIT = 500
READBACK_PAGE_SIZE = 500
RECENT_TRIGGER_LIMIT = 12
NAME_RESOLVE_BATCH_LIMIT = 200  # Database/core/master_resolution_contracts.py
AI_VALIDITY_DAYS = 2
YEAR_ROLLOVER_MONTHS = 6
LABELS = {"classify": "消息分类", "vision": "原图识读", "extract": "报价解析",
          "inventory": "独立来源盘点", "coverage": "机械覆盖检查", "audit": "独立复核"}
TERMINAL = ("SUCCEEDED", "FAILED", "SKIPPED", PARTIAL_REVIEW, NEEDS_REVIEW)
# API: {ok: bool, data?: object, error?: str}.
# Task lease: {id, run_id, stage, attempt, generation, owner, lease_until}.
# Run input is an immutable fixture snapshot; artifacts are keyed by task generation.
