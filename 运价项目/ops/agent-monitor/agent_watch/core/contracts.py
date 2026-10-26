"""Monitor v1 contract. No external dependencies or IO."""
from typing import TypedDict
VERSION = "1.5.0"
HOST = "127.0.0.1"
PORT = 8796
MAX_BODY_BYTES = 65536
MAX_TEXT = 300
EVENT_LIMIT = 250
RUN_LIMIT = 40
STALE_SECONDS = 45
IDLE_SECONDS = 180
HEARTBEAT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 0.6
POLL_SECONDS = 2
HEALTH_PATH = "/api/health"
REPORT_PATH = "/api/report"
CONTROL_PATH = "/api/control"
ORCHESTRATOR_PAGE_PATH = "/orchestrator"
ORCHESTRATOR_RUNS_PATH = "/api/orchestrator/runs"
ORCHESTRATOR_HEALTH_PATH = "/api/orchestrator/health"
ORCHESTRATOR_DETAIL_PATH = "/api/orchestrator/run"
TERMINAL = {"completed", "failed", "interrupted"}
class Progress(TypedDict, total=False):
    run_id: str
    shard: str
    task_name: str
    source: str
    workflow_stage: str
    stage: str
    activity: str
    group: str
    message_id: str
    file: str
    status: str
    heartbeat: bool
    control_ready: bool
    completed: int
    total: int
    check_failed: int
class OrchestratorQueue(TypedDict):
    batches: dict[str, int]
    messages: dict[str, int]
    running_tasks: int
    lane_blocked_batches: int
    last_progress_at: float | None

STARTUP_ATTEMPTS = 20
STARTUP_POLL_SECONDS = 0.1
DESKTOP_SCAN_SECONDS = 2

class GroupStatus(TypedDict):
    lane: str
    name: str
    run_id: str
    status: str
    stage: str
    error: str
    failed_messages: int
    running_messages: int
    followup: int
    review_batches: int
    merged: int
    last_event_at: float | None
    created: float
