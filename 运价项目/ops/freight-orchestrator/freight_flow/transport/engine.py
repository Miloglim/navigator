"""Long-lived local extraction service: deterministic scheduler plus worker pool."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from freight_flow.core.contracts import CONCURRENCY, PULSE_SECONDS, STOP_FILE
from freight_flow.services.schedule import due_triggers, next_slots
from freight_flow.transport.production import atomic_json
from freight_flow.transport.runtime_control import RuntimeControl
from freight_flow.transport.runtime_settings import read_runtime_settings, save_worker
from freight_flow.transport.worker import work

LOG = logging.getLogger("engine")


class EngineService:
    def __init__(self, runtime: Path, store, production, schedule_path: Path, model: str,
                 concurrency: int = CONCURRENCY, workers_enabled=True):
        self.runtime = Path(runtime)
        self.store = store
        self.production = production
        self.schedule_path = Path(schedule_path)
        self.model = model
        self.concurrency = max(1, int(concurrency))
        if self.store is not None:
            self.store.claim_limit = self.concurrency
        self.worker_gate = threading.Event()
        if workers_enabled:
            self.worker_gate.set()
        self.stop = threading.Event()
        self.scheduler_thread = None
        self.workers = []
        self.http_server = None
        self.started_at = None
        self.last_tick_at = None
        self.last_scheduler_error = ""

    def load_schedule(self) -> dict:
        return json.loads(self.schedule_path.read_text(encoding="utf-8-sig"))

    def set_workers_enabled(self, enabled):
        if enabled:
            self.worker_gate.set()
            (self.runtime / "agent-health.json").unlink(missing_ok=True)
        else:
            self.worker_gate.clear()
        return self.worker_gate.is_set()

    def configure_agent(self, model, codex=None, refresh_health=True):
        self.model = str(model)
        agent = getattr(self.production, "agent", None)
        if agent is not None and hasattr(agent, "configure"):
            agent.configure(model=self.model, codex=codex)
        if refresh_health and self.started_at is not None:
            self._heartbeat()
        return {"model": self.model, "codex": getattr(agent, "codex", None)}

    def _reload_agent_config(self):
        settings_path = self.schedule_path.with_name("runtime.json")
        if not settings_path.is_file():
            return
        settings = read_runtime_settings(settings_path)
        worker = settings["worker"]
        executable = settings["executables"]
        self.configure_agent(worker["model"], executable["codex_exe"] or None,
                             refresh_health=False)

    def _block_on_agent_failure(self, reason, state):
        self.worker_gate.clear()
        settings = read_runtime_settings(self.schedule_path.with_name("runtime.json"))
        worker = settings["worker"]
        executable = settings["executables"]
        save_worker(self.schedule_path.with_name("runtime.json"), {
            "enabled": False,
            "model": worker["model"],
            "concurrency": worker["concurrency"],
            "codex_exe": executable["codex_exe"],
            "dws_exe": executable["dws_exe"],
        })
        atomic_json(self.runtime / "agent-health.json", {
            "state": state,
            "reason": reason,
            "at": time.time(),
        })
        LOG.error("[engine] Agent 系统性失败，已暂停全部 Worker 领取: %s", reason)

    def enqueue_due(self, now_ts: float | None = None) -> dict[str, list[str]]:
        triggers = due_triggers(time.time() if now_ts is None else now_ts, self.load_schedule())
        queued = {}
        for shard, trigger in triggers.items():
            if self.store is not None and self.store.has_trigger_prefix(trigger):
                continue
            queued[shard] = self.production.enqueue(shard, trigger)
            LOG.info("[%s] 到期触发 %s runs=%s", shard, trigger, len(queued[shard]))
        return queued

    def _scheduler_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if (self.runtime / STOP_FILE).is_file():
                    self.stop.set()
                    if self.http_server is not None:
                        self.http_server.shutdown()
                    break
                self.enqueue_due()
                self.last_tick_at = time.time()
                self.last_scheduler_error = ""
                self._heartbeat()
                tick = max(1, int(self.load_schedule().get("tick_seconds") or 30))
            except Exception as exc:
                self.last_scheduler_error = str(exc)
                LOG.exception("[engine] 调度循环异常，保留队列并等待重试")
                self._heartbeat()
                tick = 30
            self.stop.wait(tick)

    def _heartbeat(self) -> None:
        now = time.time()
        workers = [{"name": thread.name, "alive": thread.is_alive()} for thread in self.workers]
        try:
            schedule = self.load_schedule()
            schedule_status = {
                "timezone": "Asia/Shanghai",
                "enabled": bool(schedule.get("enabled", True)),
                "last_tick_at": self.last_tick_at,
                "next_slots": next_slots(now, schedule),
                "error": self.last_scheduler_error,
            }
        except Exception as exc:
            schedule_status = {
                "timezone": "Asia/Shanghai", "enabled": False,
                "last_tick_at": self.last_tick_at, "next_slots": {}, "error": str(exc),
            }
        try:
            queue = self.store.task_summary()
        except Exception:
            LOG.exception("[engine] 队列摘要读取失败，健康心跳继续保留")
            queue = {"error": "queue summary unavailable"}
        try:
            dws = json.loads((self.runtime / "dws-health.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dws = {"status": "unknown", "reason": "尚无 DWS 调用记录"}
        try:
            agent_health = json.loads((self.runtime / "agent-health.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            agent_health = {"state": "ready", "reason": ""}
        atomic_json(self.runtime / "worker-health.json", {
            "mode": "service",
            "pid": os.getpid(),
            "at": now,
            "started_at": self.started_at,
            "model": self.model,
            "workers": self.concurrency,
            "worker_config": {
                "model": self.model,
                "capacity": self.concurrency,
                "agent": "codex-subprocess",
                "claim_names": [item["name"] for item in workers],
            },
            "workers_alive": all(item["alive"] for item in workers) if workers else False,
            "worker_control": {"enabled": self.worker_gate.is_set()},
            "agent_health": agent_health,
            "worker_threads": workers,
            "scheduler_alive": bool(self.scheduler_thread and self.scheduler_thread.is_alive()),
            "schedule": schedule_status,
            "queue": queue,
            "dws": dws,
        })

    def start_background(self) -> None:
        self.started_at = time.time()
        self.workers = []
        for index in range(1, self.concurrency + 1):
            name = f"engine-worker-{index}"
            self.workers.append(threading.Thread(
                target=work,
                args=(self.store, self.production, self.stop),
                kwargs={"owner": name, "gate": self.worker_gate,
                        "on_agent_block": self._block_on_agent_failure,
                        "before_claim": self._reload_agent_config},
                name=name,
                daemon=True,
            ))
        for thread in self.workers:
            thread.start()
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop,
                                                 name="engine-scheduler", daemon=True)
        self.scheduler_thread.start()

    def serve_forever(self, web: Path, port: int) -> None:
        from freight_flow.transport.server import server
        runtime_control = RuntimeControl(
            self.runtime, self.store, self.schedule_path, dws=getattr(self.production, "dws", "dws"),
            model=self.model, concurrency=self.concurrency, start_auth_monitor=True, engine=self)
        http = server(self.store, web, port=port, runtime_control=runtime_control)
        self.http_server = http
        self.start_background()
        try:
            LOG.info("[engine] service ready port=%s workers=%s", port, self.concurrency)
            http.serve_forever()
        finally:
            self.stop.set()
            http.server_close()
            for thread in [*self.workers, self.scheduler_thread]:
                if thread:
                    thread.join(timeout=PULSE_SECONDS * 2)
            for name in ("worker-health.json", STOP_FILE):
                try:
                    (self.runtime / name).unlink(missing_ok=True)
                except OSError:
                    LOG.warning("[engine] 退出清理暂未释放 %s；进程已停止，保留文件供下次覆盖", name)
