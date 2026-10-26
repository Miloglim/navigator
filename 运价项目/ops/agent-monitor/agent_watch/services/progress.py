"""Pure validation, safe event projection and derived display state."""
import re
from agent_watch.core.contracts import MAX_TEXT, STALE_SECONDS, IDLE_SECONDS, TERMINAL
FIELDS = {"run_id", "shard", "task_name", "source", "stage", "workflow_stage", "activity", "group", "message_id", "file", "status", "completed", "total", "check_failed", "heartbeat", "control_ready"}
SOURCES = {"runner", "pipeline", "agent", "desktop"}
STATUSES = {"running", "paused", *TERMINAL}
SECRET = re.compile(r"(?i)(bearer\s+\S+|(?:token|password|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+)")
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
def clean(value):
    return SECRET.sub("[已隐藏凭据]", str(value))[:MAX_TEXT]
def validate(payload):
    if not isinstance(payload, dict) or not RUN_ID.fullmatch(str(payload.get("run_id", ""))):
        raise ValueError("invalid run_id")
    result = {}
    for key in FIELDS & payload.keys():
        value = payload[key]
        if key in {"completed", "total", "check_failed"}:
            if type(value) is not int or value < 0:
                raise ValueError("invalid count")
            result[key] = value
        elif key in {"heartbeat", "control_ready"}:
            if type(value) is not bool:
                raise ValueError("invalid heartbeat")
            result[key] = value
        else:
            result[key] = clean(value)
    if result.get("source") not in SOURCES:
        raise ValueError("invalid source")
    if "status" in result and result["status"] not in STATUSES:
        raise ValueError("invalid status")
    return result

def project_cli(event):
    item = event.get("item") or {}
    kind = event.get("type", "")
    if kind in {"error", "turn.failed"}:
        return {"activity": "执行器报告错误，请查任务日志", "stage": "ERROR"}
    if kind not in {"item.started", "item.completed", "item.updated"}:
        return None
    category = item.get("type", "")
    command = str(item.get("command", ""))
    if category == "command_execution":
        label = "执行命令"
        for needle, title in [("pipeline.py", "推进采集流水线"), ("dws", "读取钉钉数据"), ("ocr_cache", "处理识图缓存"), ("check_llm_out", "核对识别来源契约"), ("ledger_client", "调用台账接口"), ("规范.md", "读取群业务规范")]:
            if needle in command:
                label = title
                break
        files = re.findall(r"[^\s\"']+\.(?:py|json|png|jpg|md|ps1)", command)
        filename = files[-1].replace("\\", "/").rsplit("/", 1)[-1] if files else ""
        failed = item.get("exit_code") not in (None, 0) or item.get("status") == "failed"
        return {"activity": label + ("失败" if failed else "完成" if kind == "item.completed" else "开始"), "file": clean(filename), "stage": "TOOL"}
    if category in {"mcp_tool_call", "web_search", "file_change"}:
        return {"activity": {"mcp_tool_call": "调用工具", "web_search": "检索资料", "file_change": "写入工作文件"}[category] + ("完成" if kind == "item.completed" else "开始"), "stage": "TOOL"}
    # Never forward raw messages, tool output, prompts, arguments or reasoning.
    return None

def display(state, now):
    state = dict(state)
    state["signal_age_seconds"] = max(0, int(now - state["seen_at"]))
    state["activity_age_seconds"] = max(0, int(now - state.get("activity_at", state["seen_at"])))
    if state.get("status") in TERMINAL:
        state["health"] = state["status"]
    elif state["signal_age_seconds"] > STALE_SECONDS:
        state["health"] = "disconnected"
    elif state.get("status") == "paused":
        state["health"] = "paused"
    elif state["activity_age_seconds"] > IDLE_SECONDS:
        state["health"] = "quiet"
    else:
        state["health"] = "running"
    return state
