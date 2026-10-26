"""Read-only runtime projection and the controlled DWS authentication adapter."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from freight_flow.core.contracts import (CONCURRENCY, DWS_TIMEOUT_SECONDS,
                                         PRODUCTION_MESSAGE_STAGES,
                                         RECENT_TRIGGER_LIMIT)
from freight_flow.services.schedule import next_slots
from freight_flow.transport.daemon import service_status
from freight_flow.transport.production import atomic_json, hidden_subprocess_kwargs
from freight_flow.transport.runtime_settings import (effective_settings, read_runtime_settings,
                                                     save_schedule, save_worker)

LOG = logging.getLogger("runtime-control")
DWS_AUTH_CHECK_SECONDS = 30
DWS_AUTH_STALE_SECONDS = 90
PIPELINE = (
    {"stage": "pull", "title": "DWS 拉取", "executor": "来源 IO", "module": "transport/production.py"},
    {"stage": "fanout", "title": "拆分消息", "executor": "机械", "module": "transport/production.py"},
    {"stage": "prepare", "title": "固化原始证据", "executor": "机械", "module": "transport/production.py"},
    {"stage": "classify", "title": "消息分类", "executor": "Agent", "module": "transport/agent.py"},
    {"stage": "vision", "title": "原图识读", "executor": "Agent", "module": "transport/agent.py"},
    {"stage": "extract", "title": "报价抽取", "executor": "Agent", "module": "transport/agent.py"},
    {"stage": "inventory", "title": "独立盘点", "executor": "Agent", "module": "transport/agent.py"},
    {"stage": "coverage", "title": "覆盖检查", "executor": "机械门禁", "module": "services/workflow.py"},
    {"stage": "audit", "title": "独立复核", "executor": "Agent", "module": "transport/agent.py"},
    {"stage": "normalize", "title": "字段归一", "executor": "机械", "module": "transport/production.py"},
    {"stage": "validate", "title": "台账预检", "executor": "台账 IO", "module": "transport/production.py"},
    {"stage": "submit", "title": "写入待审", "executor": "台账 IO", "module": "transport/production.py"},
    {"stage": "readback", "title": "读回核对", "executor": "台账 IO", "module": "transport/production.py"},
    {"stage": "reconcile", "title": "群级对账", "executor": "机械门禁", "module": "transport/production.py"},
    {"stage": "watermark", "title": "提交水位", "executor": "锁定 IO", "module": "transport/production.py"},
)


class DwsAuthManager:
    def __init__(self, runtime, dws="dws"):
        self.runtime = Path(runtime)
        self.dws = str(dws)
        self.path = self.runtime / "dws-auth.json"
        self._lock = threading.Lock()
        self._checking = False
        self._monitor = None
        self._login_process = None
        self._login_thread = None

    def configure(self, dws):
        value = str(dws or "dws")
        if value != self.dws:
            self.dws = value
            self.begin_check(force=True)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, state, authenticated=False, reason=""):
        now = time.time()
        atomic_json(self.path, {
            "state": state,
            "authenticated": bool(authenticated),
            "reason": reason,
            "at": now,
            "checked_at": now,
        })

    def snapshot(self):
        data = self._read()
        process_alive = bool(self._login_process and self._login_process.poll() is None)
        if process_alive:
            data["state"] = "login_pending"
            data["authenticated"] = False
            data["reason"] = "等待浏览器完成授权"
        if not data:
            return {
                "state": "unknown",
                "authenticated": False,
                "reason": "尚未读取 DWS 登录态",
                "fresh": False,
                "login_running": process_alive,
            }
        try:
            checked_at = float(data.get("checked_at") or data.get("at") or 0)
        except (TypeError, ValueError):
            checked_at = 0
        return {
            **data,
            "fresh": time.time() - checked_at < DWS_AUTH_STALE_SECONDS,
            "login_running": process_alive,
        }

    def begin_check(self, force=False):
        if self._login_process and self._login_process.poll() is None:
            return False
        data = self._read()
        try:
            checked_at = float(data.get("checked_at") or data.get("at") or 0)
        except (TypeError, ValueError):
            checked_at = 0
        if not force and time.time() - checked_at < DWS_AUTH_CHECK_SECONDS:
            return False
        with self._lock:
            if self._checking:
                return False
            self._checking = True
        threading.Thread(target=self._check_worker, name="dws-auth-check", daemon=True).start()
        return True

    def start_monitor(self):
        if self._monitor and self._monitor.is_alive():
            return

        def monitor():
            while True:
                self._check_now()
                time.sleep(DWS_AUTH_CHECK_SECONDS)

        self._monitor = threading.Thread(target=monitor, name="dws-auth-monitor", daemon=True)
        self._monitor.start()

    def _check_worker(self):
        try:
            self._check_now()
        finally:
            with self._lock:
                self._checking = False

    def _check_now(self):
        if self._login_process and self._login_process.poll() is None:
            return
        try:
            result = subprocess.run(
                [self.dws, "auth", "status", "--format", "json"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DWS_TIMEOUT_SECONDS,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            self._write("unavailable", reason="DWS 登录态读取超时")
            return
        except OSError:
            LOG.exception("[DWS-AUTH] 无法执行 DWS CLI")
            self._write("unavailable", reason="DWS CLI 不可用")
            return
        if result.returncode:
            self._write("unavailable", reason="DWS 登录态读取失败")
            return
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            self._write("unavailable", reason="DWS 返回了无效的登录态")
            return
        authenticated = bool(payload.get("authenticated"))
        self._write(
            "authenticated" if authenticated else "not_authenticated",
            authenticated=authenticated,
            reason="已登录" if authenticated else "未登录",
        )

    def login(self):
        current = self.snapshot()
        if current.get("authenticated"):
            return {"started": False, "already_authenticated": True, **current}
        if self._login_process and self._login_process.poll() is None:
            return {"started": False, "already_pending": True, **current}
        command = [self.dws, "auth", "login", "--recommend", "--format", "json"]
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if os.name == "nt" else {"start_new_session": True})
        try:
            self._login_process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **options,
            )
        except OSError:
            LOG.exception("[DWS-AUTH] 无法启动浏览器授权")
            self._write("unavailable", reason="无法启动 DWS 浏览器授权")
            return {"started": False, "reason": "无法启动 DWS 浏览器授权", **self.snapshot()}
        self._write("login_pending", reason="等待浏览器完成授权")
        self._login_thread = threading.Thread(
            target=self._wait_for_login, name="dws-auth-login", daemon=True)
        self._login_thread.start()
        return {"started": True, **self.snapshot()}

    def _wait_for_login(self):
        process = self._login_process
        if process is not None:
            process.wait()
        self._check_now()


class RuntimeControl:
    def __init__(self, runtime, store, schedule_path, dws="dws", model="gpt-6-sol",
                 concurrency=CONCURRENCY, start_auth_monitor=False, engine=None):
        self.runtime = Path(runtime)
        self.store = store
        self.schedule_path = Path(schedule_path)
        self.runtime_config_path = self.schedule_path.with_name("runtime.json")
        self.model = model
        self.concurrency = max(1, int(concurrency))
        self.engine = engine
        self.dws_auth = DwsAuthManager(self.runtime, dws)
        if start_auth_monitor:
            self.dws_auth.start_monitor()

    def save_schedule(self, payload):
        saved = save_schedule(self.schedule_path, payload)
        return saved

    def save_worker(self, payload):
        saved = save_worker(self.runtime_config_path, payload)
        if self.engine is not None:
            self.engine.set_workers_enabled(saved["enabled"])
            self.engine.configure_agent(saved["model"], saved["codex_exe"] or None)
            self.model = saved["model"]
        if saved["dws_exe"]:
            self.dws_auth.configure(saved["dws_exe"])
        return saved

    def available_groups(self):
        if self.engine is None:
            return []
        groups = []
        shards = self._schedule().get("shard_offsets_seconds") or {}
        for shard in sorted(shards):
            for group in self.engine.production.groups(shard):
                groups.append({"name": group.get("name") or group["slug"], "slug": group["slug"],
                               "shard": shard})
        return sorted(groups, key=lambda item: (item["shard"], item["name"]))

    def trigger_group(self, slug, message_id=None):
        if self.engine is None:
            raise ValueError("当前工作台不是常驻 service，不能立即触发")
        if not self.engine.worker_gate.is_set():
            raise ValueError("Worker 当前已暂停，请先启用 Worker")
        target = next((item for item in self.available_groups() if item["slug"] == slug), None)
        if target is None:
            raise ValueError("群聊不在已登记配置中")
        trigger_key = "manual:" + time.strftime("%Y%m%d-%H%M%S") + ":" + slug + ":" + uuid.uuid4().hex[:8]
        run_ids = self.engine.production.enqueue(target["shard"], trigger_key, only=slug,
                                                 message_id=message_id, priority=1)
        return {"group": target, "trigger_key": trigger_key, "run_ids": run_ids,
                "message_id": message_id or ""}

    def _schedule(self):
        try:
            config = json.loads(self.schedule_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return {"state": "error", "enabled": False, "error": str(exc),
                    "windows": [], "next_slots": {}, "timezone": "Asia/Shanghai"}
        now = time.time()
        try:
            projected = next_slots(now, config)
            error = ""
        except ValueError as exc:
            projected = {}
            error = str(exc)
        try:
            tick_seconds = int(config.get("tick_seconds") or 0)
        except (TypeError, ValueError):
            tick_seconds = 0
        return {
            "state": "error" if error else "configured" if config.get("enabled", True) else "disabled",
            "enabled": bool(config.get("enabled", True)),
            "timezone": "Asia/Shanghai",
            "tick_seconds": tick_seconds,
            "weekdays": list(config.get("weekdays") or []),
            "windows": list(config.get("windows") or []),
            "shard_offsets_seconds": dict(config.get("shard_offsets_seconds") or {}),
            "next_slots": projected,
            "error": error,
        }

    def snapshot(self, trigger_limit=RECENT_TRIGGER_LIMIT):
        status = service_status(self.runtime)
        heartbeat = status.get("data") or {}
        try:
            saved = read_runtime_settings(self.runtime_config_path)
            effective = effective_settings(
                self.runtime_config_path, model=self.model, concurrency=self.concurrency,
                dws_exe=self.dws_auth.dws)
        except ValueError:
            LOG.exception("[RUNTIME] 保存的 Worker 配置无效，回退启动参数")
            saved = {"worker": {"model": self.model, "concurrency": self.concurrency},
                     "executables": {"codex_exe": "", "dws_exe": self.dws_auth.dws}}
            effective = {"model": self.model, "concurrency": self.concurrency,
                         "codex_exe": "", "dws_exe": self.dws_auth.dws}
        schedule = self._schedule()
        if status["state"] == "running":
            schedule["state"] = "running" if schedule["enabled"] else "paused"
        schedule["last_tick_at"] = (heartbeat.get("schedule") or {}).get("last_tick_at")
        schedule["scheduler_alive"] = bool(heartbeat.get("scheduler_alive"))

        running_tasks = self.store.running_tasks()
        by_owner = {task["owner"]: task for task in running_tasks}
        config = heartbeat.get("worker_config") or {}
        worker_control = bool((heartbeat.get("worker_control") or {}).get(
            "enabled", saved["worker"].get("enabled", True)))
        capacity = int(config.get("capacity") or heartbeat.get("workers") or effective["concurrency"])
        threads = heartbeat.get("worker_threads") or []
        configured = []
        if threads:
            for thread in threads:
                task = by_owner.get(thread.get("name"))
                configured.append({
                    "id": thread.get("name"),
                    "state": "busy" if task else "ready" if thread.get("alive") and worker_control
                    else "paused" if thread.get("alive") else "offline",
                    "current": self._task_projection(task) if task else None,
                })
        else:
            configured = [{"id": f"engine-worker-{index}", "state": "configured", "current": None}
                          for index in range(1, capacity + 1)]
        try:
            queue = self.store.task_summary()
        except Exception:
            LOG.exception("[RUNTIME] 队列摘要读取失败")
            queue = {"error": "queue summary unavailable"}
        workers = {
            "model": config.get("model") or heartbeat.get("model") or effective["model"],
            "capacity": capacity,
            "agent": config.get("agent") or "codex-subprocess",
            "stages": list(PRODUCTION_MESSAGE_STAGES),
            "service_ready": status["state"] == "running",
            "control_enabled": worker_control,
            "configured": configured,
            "running_tasks": [self._task_projection(task) for task in running_tasks],
            "queue": queue,
        }
        trigger_rows = []
        try:
            trigger_rows = self.store.recent_triggers(trigger_limit)
        except Exception:
            LOG.exception("[RUNTIME] 最近触发读取失败")
        service = {
            "state": status["state"],
            "healthy": status["healthy"],
            "reason": status.get("reason", ""),
            "age_seconds": status.get("age_seconds"),
            "mode": heartbeat.get("mode") or "readonly-monitor",
            "pid": heartbeat.get("pid"),
            "started_at": heartbeat.get("started_at"),
        }
        agent = heartbeat.get("agent_health")
        if not agent:
            try:
                agent = json.loads((self.runtime / "agent-health.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                agent = {"state": "ready", "reason": ""}
        return {
            "generated_at": time.time(),
            "service": service,
            "scheduler": schedule,
            "triggers": trigger_rows,
            "workers": workers,
            "dws": self.dws_auth.snapshot(),
            "agent": agent,
            "settings": {
                "worker": {**saved["worker"], "codex_exe": saved["executables"]["codex_exe"],
                           "dws_exe": saved["executables"]["dws_exe"]},
                "effective": effective,
            },
            "pipeline": list(PIPELINE),
            "groups": self.available_groups(),
        }

    @staticmethod
    def _task_projection(task):
        return {
            "task_id": task["id"],
            "run_id": task["run_id"],
            "trigger_key": task["trigger_key"],
            "lane": task["lane"],
            "stage": task["stage"],
            "owner": task["owner"],
            "attempt": task["attempt"],
            "generation": task["generation"],
            "lease_until": task["lease_until"],
        }
