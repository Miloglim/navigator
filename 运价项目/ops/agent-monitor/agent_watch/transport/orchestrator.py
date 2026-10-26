"""Read orchestration snapshots using the shared store in enforced read-only mode."""
import sys
from pathlib import Path
from agent_watch.services.orchestrator_progress import summarize_queue, business_state


class OrchestratorReader:
    def __init__(self, project):
        package = project / "ops" / "freight-orchestrator"
        if str(package) not in sys.path:
            sys.path.append(str(package))
        from freight_flow.transport.store import Store
        self.store = Store(project / "Skill" / "heartbeat-data" / "orchestrator" / "control.sqlite3", readonly=True)
        self.web = package / "web" / "index.html"

    def runs(self):
        if not Path(self.store.path).is_file():
            return []  # No orchestration run exists yet; never create a database to display an empty view.
        return self.store.runs()

    def health(self):
        from freight_flow.transport.daemon import healthy
        runtime = Path(self.store.path).parent
        enabled = runtime / 'production-enabled.json'
        queue = self.queue()
        return {'available': self.web.is_file(), 'production_cutover': enabled.is_file(),
                'worker_alive': healthy(runtime), 'queue': queue, 'business_state': business_state(queue),
                'groups': self.groups()}

    def groups(self):
        from freight_flow.services.scheduling import group_summary
        if not Path(self.store.path).is_file():
            return []
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("""SELECT r.id,r.lane,r.created,
                json_extract(r.input,'$.group.name') AS name,
                (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='FAILED') AS failed,
                (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='RUNNING') AS running,
                (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='RETRY_WAIT') AS retry,
                (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='HELD') AS held,
                (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='COALESCED') AS coalesced,
                (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status NOT IN ('SUCCEEDED','SKIPPED')) AS unfinished,
                EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child WHERE d.parent=r.id AND t.status='RUNNING') AS child_running,
                EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child WHERE d.parent=r.id AND t.status='FAILED') AS child_failed,
                EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child WHERE d.parent=r.id AND t.status NOT IN ('SUCCEEDED','SKIPPED')) AS child_waiting,
                (SELECT t.stage FROM tasks t WHERE t.run_id=r.id AND t.status NOT IN ('SUCCEEDED','SKIPPED','COALESCED') ORDER BY t.position LIMIT 1) AS stage,
                (SELECT t.error FROM tasks t WHERE t.run_id=r.id AND t.error!='' ORDER BY t.position LIMIT 1) AS error,
                (SELECT max(e.at) FROM events e WHERE e.run_id=r.id) AS last_event_at,
                (SELECT count(DISTINCT m.id) FROM runs m JOIN tasks t ON t.run_id=m.id WHERE m.mode='production-message'
                  AND json_extract(m.input,'$.group.slug')=substr(r.lane,7) AND t.status='FAILED') AS failed_messages,
                (SELECT count(*) FROM runs m JOIN tasks t ON t.run_id=m.id WHERE m.mode='production-message'
                  AND json_extract(m.input,'$.group.slug')=substr(r.lane,7) AND t.status='RUNNING') AS running_messages
                FROM runs r WHERE r.mode='production-batch' AND r.lane GLOB 'group:*' ORDER BY r.rowid""")]
        return group_summary(rows)

    def queue(self):
        if not Path(self.store.path).is_file():
            return summarize_queue([], None)
        with self.store.connect() as db:
            db.execute('BEGIN')  # Counts and successful event timestamp share one read-only snapshot.
            rows = [dict(row) for row in db.execute("""
                SELECT r.id,r.lane,r.mode,
                  (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='FAILED') AS failed,
                  (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='RUNNING') AS running,
                  (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='RETRY_WAIT') AS retry,
                  (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='HELD') AS held,
                  (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status='COALESCED') AS coalesced,
                  (SELECT count(*) FROM tasks t WHERE t.run_id=r.id AND t.status NOT IN ('SUCCEEDED','SKIPPED')) AS unfinished,
                  EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child
                         WHERE d.parent=r.id AND t.status='FAILED') AS child_failed,
                  EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child
                         WHERE d.parent=r.id AND t.status NOT IN ('SUCCEEDED','SKIPPED')) AS child_waiting
                FROM runs r WHERE (r.mode='production-batch' AND r.lane GLOB 'group:*')
                  OR (r.mode='production-message' AND r.lane GLOB 'message:*') ORDER BY r.rowid
            """)]
            progress = db.execute("""SELECT max(e.at) FROM events e JOIN runs r ON r.id=e.run_id
                WHERE ((r.mode='production-batch' AND r.lane GLOB 'group:*')
                  OR (r.mode='production-message' AND r.lane GLOB 'message:*'))
                AND e.message IN ('SUCCEEDED','SKIPPED')""").fetchone()[0]
        return summarize_queue(rows, progress)

    def page(self):
        return (self.web.read_text(encoding="utf-8")
                .replace("fetch('/api/runs')", "fetch('/api/orchestrator/runs')")
                .replace("fetch('/api/health')", "fetch('/api/orchestrator/health')")
                .replace("fetch('/api/run?id='", "fetch('/api/orchestrator/run?id='").encode("utf-8"))
