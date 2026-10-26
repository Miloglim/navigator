"""Validation for runtime-editable scheduler and worker settings."""
from __future__ import annotations

import re

from freight_flow.services.schedule import _clock_seconds, validate_schedule

WORKER_LIMIT_MIN = 1
WORKER_LIMIT_MAX = 8
TICK_SECONDS_MIN = 1
TICK_SECONDS_MAX = 3600
INTERVAL_SECONDS_MIN = 60
INTERVAL_SECONDS_MAX = 86400
WINDOW_LIMIT = 8
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def schedule_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError("调度配置必须是对象")
    expected = {"enabled", "tick_seconds", "weekdays", "shard_offsets_seconds", "windows"}
    if set(payload) != expected:
        raise ValueError("调度配置字段不完整或包含未知字段")
    if not isinstance(payload["enabled"], bool):
        raise ValueError("enabled 必须是布尔值")
    try:
        tick_seconds = int(payload["tick_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValueError("tick_seconds 必须是整数") from exc
    if not TICK_SECONDS_MIN <= tick_seconds <= TICK_SECONDS_MAX:
        raise ValueError("tick_seconds 超出范围")
    weekdays = payload["weekdays"]
    if not isinstance(weekdays, list) or not weekdays:
        raise ValueError("至少选择一个工作日")
    try:
        weekdays = sorted({int(day) for day in weekdays})
    except (TypeError, ValueError) as exc:
        raise ValueError("weekdays 必须是 1 到 7") from exc
    if any(day < 1 or day > 7 for day in weekdays):
        raise ValueError("weekdays 必须是 1 到 7")
    offsets = payload["shard_offsets_seconds"]
    if not isinstance(offsets, dict) or not offsets:
        raise ValueError("至少配置一个分片偏移")
    normalized_offsets = {}
    for shard, value in offsets.items():
        name = str(shard).strip()
        if not name or len(name) > 16:
            raise ValueError("分片名称无效")
        try:
            normalized_offsets[name] = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("分片偏移必须是整数秒") from exc
    windows = payload["windows"]
    if not isinstance(windows, list) or not windows or len(windows) > WINDOW_LIMIT:
        raise ValueError("时间窗数量必须是 1 到 8")
    normalized_windows = []
    for window in windows:
        if not isinstance(window, dict) or set(window) != {"start", "end", "interval_seconds"}:
            raise ValueError("时间窗字段无效")
        start, end = str(window["start"]), str(window["end"])
        try:
            start_seconds, end_seconds = _clock_seconds(start), _clock_seconds(end)
            interval = int(window["interval_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("时间窗时间或间隔无效") from exc
        if start_seconds > end_seconds:
            raise ValueError("时间窗开始不能晚于结束")
        if not INTERVAL_SECONDS_MIN <= interval <= INTERVAL_SECONDS_MAX:
            raise ValueError("时间窗间隔超出范围")
        normalized_windows.append({"start": start, "end": end, "interval_seconds": interval})
    result = {
        "enabled": payload["enabled"],
        "tick_seconds": tick_seconds,
        "weekdays": weekdays,
        "shard_offsets_seconds": normalized_offsets,
        "windows": normalized_windows,
    }
    validate_schedule(result)
    return result


def worker_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError("Worker 配置必须是对象")
    expected = {"enabled", "model", "concurrency", "codex_exe", "dws_exe"}
    if set(payload) != expected:
        raise ValueError("Worker 配置字段不完整或包含未知字段")
    model = str(payload["model"]).strip()
    if not MODEL_PATTERN.fullmatch(model):
        raise ValueError("模型名称格式无效")
    try:
        concurrency = int(payload["concurrency"])
    except (TypeError, ValueError) as exc:
        raise ValueError("concurrency 必须是整数") from exc
    if not WORKER_LIMIT_MIN <= concurrency <= WORKER_LIMIT_MAX:
        raise ValueError("concurrency 超出范围")
    codex_exe = str(payload["codex_exe"] or "").strip()
    dws_exe = str(payload["dws_exe"] or "").strip()
    if len(codex_exe) > 1024 or len(dws_exe) > 1024:
        raise ValueError("可执行文件路径过长")
    if not isinstance(payload["enabled"], bool):
        raise ValueError("enabled 必须是布尔值")
    return {"enabled": payload["enabled"], "model": model, "concurrency": concurrency,
            "codex_exe": codex_exe, "dws_exe": dws_exe}
