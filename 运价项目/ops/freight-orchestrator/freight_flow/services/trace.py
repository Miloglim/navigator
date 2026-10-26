"""Pure review projections for the extraction trace workbench."""
from __future__ import annotations

import json
import re
from collections import Counter

STAGE_LABELS = {
    "pull": "拉取", "fanout": "拆消息", "reconcile": "群对账", "watermark": "推进水位",
    "history_cursor": "推进历史游标",
    "prepare": "固化证据", "classify": "消息分类", "vision": "原图识读", "extract": "报价抽取",
    "inventory": "独立盘点", "coverage": "覆盖检查", "audit": "独立复核", "normalize": "字段归一",
    "validate": "台账预检", "submit": "写入待审", "readback": "待审读回",
}
REVIEW_STATUS = {
    "FAILED": "runtime", "HELD": "runtime", "RUNNING": "runtime", "QUEUED": "runtime",
    "RETRY_WAIT": "runtime", "BLOCKED": "runtime", "WAITING_CHILDREN": "runtime",
    "DONE": "soft", "SUCCEEDED": "soft", "SKIPPED": "soft", "COALESCED": "soft",
    "PARTIAL_REVIEW": "soft", "PARTIAL_REVIEW_COMPLETE": "soft",
}

CLOSED_TASK_STATUSES = frozenset({"SUCCEEDED", "SKIPPED", "PARTIAL_REVIEW", "COALESCED"})
ACTIVE_TASK_STATUSES = frozenset({"RUNNING", "QUEUED", "RETRY_WAIT", "WAITING", "WAITING_CHILDREN"})


def _result(task):
    raw = task.get("result")
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {"raw": str(raw)}


def result_summary(stage, result):
    if not isinstance(result, dict):
        return ""
    if result.get('status') == 'PARTIAL_REVIEW_COMPLETE':
        counts = result.get('records') or {}
        return '部分解析已送审并读回 %s 条 · 待核回执已确认' % sum(counts.values())
    if stage == "pull":
        return f"消息 {len(result.get('messages') or [])} 条 · {result.get('before', '')} → {result.get('after', '')}"
    if stage == "fanout":
        return f"子任务 {result.get('messages', result.get('children', 0))} 条"
    if stage == "classify":
        return f"{result.get('decision', '')} · {result.get('reason', '')}"
    if stage == "vision":
        return f"原图事实 {len(result.get('facts') or [])} 项"
    if stage == "extract":
        records = result.get("records") or []
        return f"候选记录 {len(records)} 条"
    if stage == "inventory":
        cells = result.get("cells") or []
        return f"独立价格单元 {len(cells)} 格"
    if stage == "coverage":
        return f"覆盖 {result.get('priced_cells', 0)} 格 · 记录 {result.get('records', 0)} 条"
    if stage == "audit":
        issues = result.get("issues") or []
        return "复核通过" if result.get("ok") else f"复核问题 {len(issues)} 项"
    if stage == "normalize":
        records = result.get("records") if isinstance(result.get("records"), dict) else result
        counts = {kind: len(rows or []) for kind, rows in records.items() if kind in ("rates", "space")}
        return " · ".join(f"{k} {v}" for k, v in counts.items()) or "字段归一完成"
    if stage == "validate":
        records = (result.get("records") or {}) if isinstance(result.get("records"), dict) else {}
        return "台账预检 " + " · ".join(f"{kind} {len(rows or [])}" for kind, rows in records.items())
    if stage == "submit":
        return f"应有 {result.get('expected', 0)} · 已覆盖 {result.get('already_covered', 0)}"
    if stage == "readback":
        return f"读回 {len(result.get('covered_ids') or [])}/{result.get('expected', 0)}"
    if stage == "prepare":
        return f"原文已固化 · 图片 {len(result.get('image_files') or [])} 张"
    return ""


def _message_preview(text, limit=52):
    value = re.sub(r"\[图片消息\]\(mediaId=[^)]+\)", "[图片] ", str(text or ""))
    value = re.sub(r"——引用[\s\S]*", "", value)
    value = re.sub(r"\s+", " ", value).strip(" @\n\t")
    if not value:
        return "未命名消息"
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _child_summary(child, pull_time=None):
    statuses = {item for item in str(child.get("task_statuses") or "").split(",") if item}
    failed = bool(child.get("failed"))
    needs_review = bool(child.get("needs_review"))
    partial_review = bool(child.get("partial_review"))
    running = bool(child.get("running"))
    waiting = bool(child.get("waiting")) and not running
    submitted = bool(child.get("submitted"))
    readback_succeeded = bool(child.get("readback_succeeded"))
    no_quote = str(child.get("classify_decision") or "") in {"non_quote", "noise"} or bool(child.get("noise"))
    closed_statuses = bool(statuses) and statuses <= CLOSED_TASK_STATUSES
    terminal = not child.get("waiting") and (closed_statuses or failed or needs_review or partial_review
                                              or submitted or readback_succeeded or no_quote)
    complete = terminal and not failed and not needs_review and not partial_review and (readback_succeeded or no_quote)
    ingested = terminal and not complete and (submitted or readback_succeeded or partial_review)
    current_stage = str(child.get("current_stage") or "")
    stopped_stage = str(child.get("stopped_stage") or "")
    if not terminal:
        display_status = "WORKING" if current_stage else "QUEUED"
        display_label = (STAGE_LABELS.get(current_stage, current_stage) + "中") if current_stage else "等待执行"
    elif complete:
        display_status, display_label = "COMPLETED", "已完成"
    elif ingested:
        display_status, display_label = "INGESTED", "已入库"
    elif failed:
        display_status, display_label = "FAILED", "处理失败"
    elif needs_review:
        display_status, display_label = "NEEDS_REVIEW", "待人工"
    elif partial_review:
        display_status, display_label = "PARTIAL_REVIEW", "部分待核"
    else:
        display_status, display_label = "BLOCKED", "已阻塞"
    return {
        **child,
        "message_id": str(child.get("message_id") or ""),
        "title": _message_preview(child.get("message_text")),
        "pull_time": pull_time if pull_time is not None else child.get("created"),
        "message_time": str(child.get("message_time") or ""),
        "status": display_status,
        "display_label": display_label,
        "working": not terminal,
        "current_stage": current_stage,
        "current_stage_label": STAGE_LABELS.get(current_stage, current_stage),
        "stopped_stage": stopped_stage,
        "complete": complete,
        "ingested": ingested,
        "readback_succeeded": readback_succeeded,
        "submitted": submitted,
    }


def _task_node(task, events):
    stage = task.get("stage", "")
    relevant = [event for event in events if event.get("stage") == stage]
    start = min((float(event["at"]) for event in relevant if event.get("at")), default=None)
    end = max((float(event["at"]) for event in relevant if event.get("at")), default=None)
    result = _result(task)
    return {
        "id": task.get("id"), "kind": "stage", "stage": stage, "title": STAGE_LABELS.get(stage, stage),
        "status": task.get("status"), "attempt": task.get("attempt", 0), "generation": task.get("generation", 0),
        "error": task.get("error") or "", "failure_kind": task.get("failure_kind") or "",
        "started_at": start, "ended_at": end, "duration_seconds": round(end - start, 3) if start and end else None,
        "summary": result_summary(stage, result), "result": result,
    }


def _message_display(run):
    tasks = run.get("tasks") or []
    by_stage = {task.get("stage"): task for task in tasks}
    classify = _result(by_stage.get("classify") or {}) or {}
    readback = _result(by_stage.get("readback") or {}) or {}
    covered = readback.get("covered_ids") or []
    readback_ok = bool((by_stage.get("readback") or {}).get("status") == "SUCCEEDED"
                       and readback.get("ok") is not False and len(covered) >= int(readback.get("expected") or 0))
    statuses = [str(task.get("status") or "") for task in tasks]
    child = {
        "task_statuses": ",".join(statuses),
        "failed": any(status == "FAILED" for status in statuses),
        "needs_review": any(status in ("HELD", "NEEDS_REVIEW", "BLOCKED") for status in statuses),
        "partial_review": any(status == "PARTIAL_REVIEW" for status in statuses),
        "running": any(status == "RUNNING" for status in statuses),
        "waiting": any(status in ACTIVE_TASK_STATUSES for status in statuses),
        "current_stage": next((str(task.get("stage") or "") for task in tasks if task.get("status") == "RUNNING"), ""),
        "classify_decision": classify.get("decision") or "",
        "submitted": bool((by_stage.get("submit") or {}).get("status") == "SUCCEEDED"),
        "readback_succeeded": readback_ok,
        "noise": bool(run.get("noise_resolution")),
    }
    return _child_summary(child, run.get("created"))


def run_summary(run):
    source = run.get("source") or {}
    group = source.get("group") or {}
    tasks = run.get("tasks") or []
    children = [_child_summary(child) for child in run.get("children") or []]
    counts = Counter(task.get("status") for task in tasks)
    title = group.get("name") or run.get("lane") or run.get("id")
    if run.get("mode") == "production-message":
        title = _message_preview(source.get("text")) or source.get("message_id") or title
    events = run.get("events") or []
    pull_task = next((task for task in tasks if task.get("stage") == "pull"), None)
    pull_events = [event.get("at") for event in events if event.get("stage") == "pull" and event.get("at")]
    pull_time = run.get("pull_finished_at") or max(pull_events) if pull_task and pull_task.get("status") in CLOSED_TASK_STATUSES and (run.get("pull_finished_at") or pull_events) else None
    pull_status = pull_task.get("status") if pull_task else "WAITING"
    running_task = next((task for task in tasks if task.get("status") == "RUNNING"), None)
    waiting_task = next((task for task in tasks if task.get("status") in ACTIVE_TASK_STATUSES), None)
    running_child = next((child for child in children if child.get("working") and child.get("current_stage")), None)
    waiting_child = next((child for child in children if child.get("working") and not child.get("current_stage")), None)
    work_stage = ((running_child or {}).get("current_stage") or (running_task or {}).get("stage")
                  or (waiting_task or {}).get("stage") or "")
    working = bool(run.get("status") in ACTIVE_TASK_STATUSES or running_child or waiting_child or running_task or waiting_task)
    if running_task or running_child:
        work_label = (STAGE_LABELS.get(work_stage, work_stage) + "中") if work_stage else "处理中"
    elif waiting_child:
        work_label = "等待消息执行"
    elif waiting_task:
        work_label = "等待" + STAGE_LABELS.get(work_stage, work_stage)
    else:
        work_label = ""
    child_counts = {
        "total": len(children),
        "completed": sum(bool(child.get("complete")) for child in children),
        "ingested": sum(bool(child.get("ingested")) for child in children),
        "working": sum(bool(child.get("working")) for child in children),
    }
    has_issue = bool(
        counts.get("FAILED") or counts.get("HELD") or counts.get("PARTIAL_REVIEW")
        or any(child.get("failed") or child.get("needs_review") or child.get("partial_review") for child in children))
    reviewed = bool(run.get("reviewed"))
    review_needed = has_issue and not reviewed
    display_status = run.get("status")
    if reviewed:
        display_status = "REVIEWED"
    elif review_needed and not working:
        display_status = "NEEDS_REVIEW" if any(child.get("failed") or child.get("needs_review") for child in children) else "PARTIAL_REVIEW"
    return {
        "id": run.get("id"), "kind": "group" if run.get("mode") == "production-batch" else "message",
        "title": title, "lane": run.get("lane"), "mode": run.get("mode"), "status": run.get("status"),
        "display_status": display_status,
        "created": run.get("created"), "message_id": source.get("message_id", ""),
        "group_name": group.get("name", ""), "group_slug": group.get("slug", ""),
        "source": source, "counts": dict(counts), "task_count": len(tasks),
        "pull_time": pull_time, "pull_status": pull_status,
        "failed_count": counts.get("FAILED", 0), "review_needed": review_needed,
        "needs_review": review_needed, "has_issue": has_issue, "reviewed": reviewed,
        "review": run.get("review") or None,
        "children": children, "child_counts": child_counts, "working": working,
        "work_stage": work_stage, "work_label": work_label,
        "last_event_at": max((e.get("at") or 0 for e in events), default=run.get("created")),
    }


def trace_detail(run):
    result = run_summary(run)
    events = run.get("events") or []
    result["tasks"] = [_task_node(task, events) for task in run.get("tasks") or []]
    result["events"] = events
    if run.get("mode") == "production-message":
        display = _message_display(run)
        for key in ("status", "display_label", "working", "current_stage", "current_stage_label",
                    "stopped_stage", "complete", "ingested", "readback_succeeded", "submitted"):
            if key in display:
                result["display_" + key if key == "status" else key] = display[key]
    pull_task = next((task for task in result["tasks"] if task.get("stage") == "pull"), None)
    pull_time = result.get("pull_time") or ((pull_task.get("ended_at") or pull_task.get("started_at")) if pull_task else None)
    pull_time = pull_time or run.get("created")
    for child in result["children"]:
        child["pull_time"] = pull_time
    result["child_nodes"] = result["children"]
    pull_result = (pull_task or {}).get("result") or {}
    messages = pull_result.get("messages") or []
    message_times = sorted(str(message.get("createTime") or message.get("create_time") or "")
                           for message in messages if message.get("createTime") or message.get("create_time"))
    watermark_task = next((task for task in result["tasks"] if task.get("stage") == "watermark"), None)
    watermark_result = (watermark_task or {}).get("result") or {}
    watermark_status = (watermark_task or {}).get("status") or "WAITING"
    watermark_ok = bool(watermark_status == "SUCCEEDED" and watermark_result.get("ok"))
    result["watermark_timeline"] = {
        "before": pull_result.get("before") or watermark_result.get("before") or "",
        "target": pull_result.get("after") or watermark_result.get("after") or "",
        "current": (watermark_result.get("after") or pull_result.get("after") or "") if watermark_ok else "",
        "pulled_at": pull_time,
        "message_count": len(messages),
        "message_from": message_times[0] if message_times else "",
        "message_to": message_times[-1] if message_times else "",
        "status": watermark_status,
        "ok": watermark_ok,
        "label": "已推进" if watermark_ok else "推进中" if watermark_status == "RUNNING"
                 else "等待群对账" if watermark_status in ("QUEUED", "WAITING", "RETRY_WAIT")
                 else "未推进",
    }
    return result


def trace_index(runs):
    grouped = {}
    for run in runs:
        lane = run.get("lane")
        grouped.setdefault(lane, []).append(run_summary(run))
    result = []
    for items in grouped.values():
        priority = {"FAILED": 0, "BLOCKED": 0, "NEEDS_REVIEW": 0, "PARTIAL_REVIEW_COMPLETE": 1, "HELD": 1,
                    "RUNNING": 2, "RETRY_WAIT": 3, "WAITING_CHILDREN": 4,
                    "QUEUED": 5, "COALESCED": 6}
        current = min(items, key=lambda candidate: priority.get(candidate["status"], 7))
        reviews = [candidate for candidate in items if candidate["review_needed"]] if not current["review_needed"] else []
        current["history_count"] = len(items)
        current["review_root"] = max(reviews, key=lambda candidate: candidate.get("created") or 0) if reviews else None
        current["selection_role"] = "current"
        result.append(current)
    return result
