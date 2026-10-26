"""Monitor-only SQLite persistence; never opens the freight database."""
import json
import sqlite3
import threading
import time
from agent_watch.core.contracts import EVENT_LIMIT, RUN_LIMIT, TERMINAL
from agent_watch.services.progress import display, validate
class Store:
    def __init__(self, filename):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(filename, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, state TEXT NOT NULL, control TEXT NOT NULL DEFAULT 'resume'); CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at REAL NOT NULL, payload TEXT NOT NULL); CREATE INDEX IF NOT EXISTS event_run ON events(run_id,seq);")
    def report(self, payload, event_at=None):
        payload = validate(payload)
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM runs WHERE id=?", (payload["run_id"],)).fetchone()
            now = time.time() if event_at is None else event_at
            state = json.loads(row[0]) if row else {"run_id": payload["run_id"], "started_at": now, "status": "running"}
            # Late child heartbeats must not reopen a completed run.
            if state.get("status") in TERMINAL and payload.get("status", "running") not in TERMINAL:
                return state
            state["seen_at"] = max(now,state.get("seen_at",0))
            if not payload.get("heartbeat"):
                if now >= state.get("activity_at",0):
                    state.update({k:v for k,v in payload.items() if k != "heartbeat"})
                    state["activity_at"] = now
                encoded=json.dumps(payload, ensure_ascii=False,sort_keys=True)
                duplicate=event_at is not None and self.db.execute("SELECT 1 FROM events WHERE run_id=? AND at=? AND payload=?",(payload["run_id"],now,encoded)).fetchone()
                if not duplicate:
                    self.db.execute("INSERT INTO events(run_id,at,payload) VALUES(?,?,?)", (payload["run_id"], now, encoded))
                self.db.execute("DELETE FROM events WHERE run_id=? AND seq NOT IN (SELECT seq FROM events WHERE run_id=? ORDER BY seq DESC LIMIT ?)", (payload["run_id"], payload["run_id"], EVENT_LIMIT))
            self.db.execute("INSERT INTO runs(id,state) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state", (payload["run_id"], json.dumps(state, ensure_ascii=False)))
            return state
    def runs(self):
        with self.lock:
            rows = [display(json.loads(r[0]), time.time()) for r in self.db.execute("SELECT state FROM runs ORDER BY json_extract(state,'$.started_at') DESC LIMIT ?", (RUN_LIMIT,))]
            return rows
    def events(self, run_id):
        with self.lock:
            return [{"seq":r[0],"at":r[1],**json.loads(r[2])} for r in self.db.execute("SELECT seq,at,payload FROM events WHERE run_id=? ORDER BY at DESC,seq DESC LIMIT ?", (run_id, EVENT_LIMIT))]
    def control(self, run_id, action=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT control,state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("run not found")
            if action is not None:
                if action not in {"pause", "resume"}:
                    raise ValueError("invalid action")
                if json.loads(row[1]).get("status") in TERMINAL:
                    raise ValueError("run already finished")
                self.db.execute("UPDATE runs SET control=? WHERE id=?", (action, run_id))
            return action or row[0]

    def exclude_internal_session(self, run_id):
        with self.lock, self.db:
            row=self.db.execute("SELECT state FROM runs WHERE id=?",(run_id,)).fetchone()
            state=json.loads(row[0]) if row else {}
            if state.get("source")=="desktop" and state.get("control_ready") is False:
                self.db.execute("DELETE FROM events WHERE run_id=?",(run_id,))
                self.db.execute("DELETE FROM runs WHERE id=?",(run_id,))
