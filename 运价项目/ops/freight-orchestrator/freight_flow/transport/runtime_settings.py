"""Atomic persistence for runtime-editable engine settings."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from freight_flow.core.contracts import CONCURRENCY
from freight_flow.services.runtime_settings import schedule_settings, worker_settings

DEFAULT_MODEL = "gpt-6-sol"
RUNTIME_SETTINGS_VERSION = 1


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_runtime_settings(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        payload = {}
    except ValueError as exc:
        raise ValueError("运行配置文件不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("运行配置必须是对象")
    worker = payload.get("worker") or {}
    executables = payload.get("executables") or {}
    return {
        "schema_version": int(payload.get("schema_version") or RUNTIME_SETTINGS_VERSION),
        "worker": {
            "enabled": bool(worker.get("enabled", True)),
            "model": str(worker.get("model") or DEFAULT_MODEL),
            "concurrency": int(worker.get("concurrency") or CONCURRENCY),
        },
        "executables": {
            "codex_exe": str(executables.get("codex_exe") or ""),
            "dws_exe": str(executables.get("dws_exe") or ""),
        },
    }


def save_schedule(path, payload):
    normalized = schedule_settings(payload)
    _atomic_json(path, normalized)
    return normalized


def save_worker(path, payload):
    normalized = worker_settings(payload)
    value = {
        "schema_version": RUNTIME_SETTINGS_VERSION,
        "worker": {
            "enabled": normalized["enabled"],
            "model": normalized["model"],
            "concurrency": normalized["concurrency"],
        },
        "executables": {
            "codex_exe": normalized["codex_exe"],
            "dws_exe": normalized["dws_exe"],
        },
    }
    _atomic_json(path, value)
    return normalized


def effective_settings(path, model=None, concurrency=None, codex_exe=None, dws_exe=None):
    saved = read_runtime_settings(path)
    return {
        "workers_enabled": bool(saved["worker"]["enabled"]),
        "model": model or saved["worker"]["model"] or DEFAULT_MODEL,
        "concurrency": int(concurrency or saved["worker"]["concurrency"] or CONCURRENCY),
        "codex_exe": codex_exe or saved["executables"]["codex_exe"],
        "dws_exe": dws_exe or saved["executables"]["dws_exe"] or "dws",
    }
