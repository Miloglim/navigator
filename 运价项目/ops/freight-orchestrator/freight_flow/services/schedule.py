"""Deterministic per-shard schedule projection for the local extraction engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
NEXT_SLOT_SEARCH_DAYS = 8


def _clock_seconds(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule clock out of range")
    return hour * 3600 + minute * 60


def validate_schedule(config: dict) -> None:
    windows = config.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError("schedule windows required")
    if int(config.get("tick_seconds") or 0) <= 0:
        raise ValueError("tick_seconds must be positive")
    for window in windows:
        start = _clock_seconds(str(window.get("start") or ""))
        end = _clock_seconds(str(window.get("end") or ""))
        interval = int(window.get("interval_seconds") or 0)
        if start > end or interval <= 0:
            raise ValueError("invalid schedule window")


def due_triggers(now_ts: float, config: dict) -> dict[str, str]:
    """Return the latest active slot per configured shard, deduplicated by trigger key."""
    validate_schedule(config)
    if not config.get("enabled", True):
        return {}
    now = datetime.fromtimestamp(now_ts, CN_TZ)
    weekdays = {int(day) for day in config.get("weekdays", [1, 2, 3, 4, 5])}
    if now.isoweekday() not in weekdays:
        return {}
    seconds_now = now.hour * 3600 + now.minute * 60 + now.second
    result = {}
    for shard, raw_offset in (config.get("shard_offsets_seconds") or {"A": 0}).items():
        offset = int(raw_offset or 0)
        latest = None
        for window in config["windows"]:
            start = _clock_seconds(str(window["start"])) + offset
            end = _clock_seconds(str(window["end"]))
            interval = int(window["interval_seconds"])
            if seconds_now < start or seconds_now > end:
                continue
            slot = start + ((seconds_now - start) // interval) * interval
            if slot <= end and (latest is None or slot > latest):
                latest = slot
        if latest is None:
            continue
        hour, remainder = divmod(latest, 3600)
        minute, second = divmod(remainder, 60)
        slot_text = f"{hour:02d}{minute:02d}{second:02d}"
        result[str(shard)] = f"engine:{now:%Y%m%d}-{slot_text}:{shard}"
    return result


def next_slots(now_ts: float, config: dict) -> dict[str, dict]:
    """Project the next real slot per shard without depending on scheduler memory."""
    validate_schedule(config)
    if not config.get("enabled", True):
        return {}
    now = datetime.fromtimestamp(now_ts, CN_TZ)
    weekdays = {int(day) for day in config.get("weekdays", [1, 2, 3, 4, 5])}
    result = {}
    for shard, raw_offset in (config.get("shard_offsets_seconds") or {"A": 0}).items():
        offset = int(raw_offset or 0)
        candidate = None
        for day_offset in range(NEXT_SLOT_SEARCH_DAYS + 1):
            day = (now + timedelta(days=day_offset)).date()
            if day.isoweekday() not in weekdays:
                continue
            for window in config["windows"]:
                start = _clock_seconds(str(window["start"])) + offset
                end = _clock_seconds(str(window["end"]))
                interval = int(window["interval_seconds"])
                for seconds in range(start, end + 1, interval):
                    slot = datetime.combine(day, datetime.min.time(), CN_TZ) + timedelta(seconds=seconds)
                    if slot > now and (candidate is None or slot < candidate):
                        candidate = slot
                        break
        if candidate is None:
            continue
        slot_text = candidate.strftime("%Y%m%d-%H%M%S")
        result[str(shard)] = {
            "at": candidate.isoformat(),
            "trigger": f"engine:{slot_text}:{shard}",
        }
    return result
