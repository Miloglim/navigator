"""Read-only tail of this project's four Codex automation session logs."""
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from agent_watch.services.session_events import automation_in, project_session, event_time, AUTOMATIONS, TASK_NAMES
from agent_watch.core.contracts import DESKTOP_SCAN_SECONDS
class DesktopObserver:
    def __init__(self, store, sessions, checkout):
        self.store=store
        self.sessions=Path(sessions)
        self.checkout=Path(checkout).resolve()
        self.files={}
    def scan(self):
        for offset in (0,1):
            date=datetime.now()-timedelta(days=offset)
            folder=self.sessions/date.strftime("%Y")/date.strftime("%m")/date.strftime("%d")
            if not folder.exists():
                continue
            for filename in folder.glob("*.jsonl"):
                self.read(filename)
    def read(self, filename):
        state=self.files.setdefault(str(filename),{"offset":0,"id":None,"automation":None,"buffer":[],"started":None})
        with filename.open("rb") as stream:
            stream.seek(state["offset"])
            while True:
                start=stream.tell()
                line=stream.readline()
                if not line or not line.endswith(b"\n"):
                    state["offset"]=start
                    break # Partial JSONL writes are retried next scan, never skipped.
                state["offset"]=stream.tell()
                try:
                    record=json.loads(line)
                except ValueError:
                    logging.warning("[DESKTOP] malformed session line skipped")
                    continue
                if record.get("type")=="session_meta":
                    payload=record.get("payload") or {}
                    source=payload.get("source")
                    internal=isinstance(source,dict) and "subagent" in source
                    if internal or payload.get("thread_source")=="guardian_review":
                        state["excluded"]=True
                        if payload.get("id"):
                            self.store.exclude_internal_session("desktop-"+payload["id"])
                    if Path(payload.get("cwd", "")).resolve()!=self.checkout:
                        state["excluded"]=True
                    state["id"]=payload.get("id")
                    state["started"]=event_time(record)
                if state.get("excluded"):
                    continue
                automation=automation_in(record)
                if automation and state.get("id"):
                    state["automation"]=automation
                    run_id="desktop-"+state["id"]
                    existing=any(r["run_id"]==run_id for r in self.store.runs())
                    if not existing:
                        self.store.report({"run_id":run_id,"source":"desktop","shard":AUTOMATIONS[automation],"task_name":TASK_NAMES[automation],"status":"running","stage":"START","activity":"接入Codex应用内定时采集","control_ready":False},event_at=state["started"])
                    for pending in state.pop("buffer",[]):
                        self.emit(state,pending)
                if state.get("automation") and state.get("id"):
                    self.emit(state,record)
                elif not state.get("excluded"):
                    projection=project_session(record)
                    if projection and state.get("buffer") is not None:
                        state["buffer"].append(record)
    def emit(self,state,record):
        run_id="desktop-"+state["id"]
        projection=project_session(record)
        if projection:
            self.store.report({"run_id":run_id,"source":"desktop","task_name":TASK_NAMES[state["automation"]],**projection},event_at=event_time(record))
        else:
            # Heartbeat only when a new session event actually exists, not on a polling timer.
            self.store.report({"run_id":run_id,"source":"desktop","heartbeat":True},event_at=event_time(record))
    def start(self):
        def loop():
            while True:
                try:
                    self.scan()
                except Exception:
                    logging.exception("[DESKTOP] observer scan failed; monitor API remains available")
                threading.Event().wait(DESKTOP_SCAN_SECONDS)
        threading.Thread(target=loop,name="desktop-observer",daemon=True).start()
