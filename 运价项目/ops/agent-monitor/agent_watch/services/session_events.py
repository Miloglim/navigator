"""Pure projection of local Codex desktop execution events. Never forward raw output."""
import json
import re
from datetime import datetime
from agent_watch.services.progress import project_cli
AUTOMATIONS = {"a-codex":"A", "a-codex-2":"A", "b-codex":"B", "b-codex-2":"B"}
TASK_NAMES = {"a-codex":"运价采集 上午 A（Codex）","a-codex-2":"运价采集 下午 A（Codex）","b-codex":"运价采集 上午 B（Codex）","b-codex-2":"运价采集 下午 B（Codex）"}
def automation_in(record):
    payload=record.get("payload") or {}
    if record.get("type")!="response_item" or payload.get("type")!="message" or payload.get("role")!="user":
        return None
    for part in payload.get("content",[]):
        text=part.get("text", "") if isinstance(part,dict) else ""
        match=re.search(r"(?m)^Automation ID: ([a-z0-9-]+)\s*$",text)
        if match and match.group(1) in AUTOMATIONS:
            return match.group(1)
    return None

def project_session(record):
    payload=record.get("payload") or {}
    kind=payload.get("type")
    if record.get("type")=="event_msg":
        if kind=="task_started":
            return {"status":"running","stage":"START","activity":"Codex应用内采集任务开始"}
        if kind in {"task_complete","task_completed"}:
            return {"status":"completed","stage":"END","activity":"Codex应用内任务执行结束，业务结果以回执为准"}
        if kind in {"task_aborted","turn_aborted"}:
            return {"status":"interrupted","stage":"END","activity":"Codex应用内任务已中断"}
        if kind=="item_completed":
            item=payload.get("item") or {}
            if item.get("type")=="CommandExecution":
                return project_cli({"type":"item.completed","item":{"type":"command_execution","command":item.get("command",""),"exit_code":item.get("exit_code")}})
            if item.get("type") in {"McpToolCall","WebSearch","ImageView","FileChange"}:
                return {"stage":"TOOL","activity":{"McpToolCall":"工具调用完成","WebSearch":"资料检索完成","ImageView":"查看图片完成","FileChange":"工作文件修改完成"}[item["type"]]}
    if record.get("type")=="response_item" and kind=="function_call":
        try:
            args=json.loads(payload.get("arguments") or "{}")
        except (ValueError,TypeError):
            return None # Unparseable tool arguments are not interpreted as commands.
        calls=args.get("tool_uses",[]) if isinstance(args,dict) else []
        commands=[args.get("cmd","")] if isinstance(args,dict) else []
        commands += [x.get("parameters",{}).get("cmd", "") for x in calls if isinstance(x,dict)]
        command=next((c for c in commands if c),"")
        if command:
            return project_cli({"type":"item.started","item":{"type":"command_execution","command":command}})
        return {"stage":"TOOL","activity":"调用工具："+str(payload.get("name", ""))[:100]}
    return None

def event_time(record):
    try:
        return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError,ValueError,TypeError):
        return None # Missing source timestamp remains unknown; store uses receive time.
