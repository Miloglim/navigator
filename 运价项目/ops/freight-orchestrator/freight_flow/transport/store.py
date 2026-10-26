import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from freight_flow.core.contracts import CONCURRENCY, LEASE_SECONDS, MAX_ATTEMPTS, RETRY_SECONDS, LIST_LIMIT
from freight_flow.core.contracts import INFRA_RECOVERY_SECONDS, INFRA_RECOVERY_MAX_SECONDS, RECOVERY_BACKOFF_MAX_POWER
from freight_flow.core.contracts import PARTIAL_REVIEW, PARTIAL_REVIEW_COMPLETE


def review_signature(tasks, children=()):
    task_state = sorted((t.get("stage"), t.get("status"), t.get("error") or "") for t in tasks)
    child_state = sorted((c.get("id"), c.get("task_statuses") or "", c.get("current_stage") or "",
                          c.get("stopped_stage") or "", c.get("error_state") or "") for c in children)
    payload = json.dumps([task_state, child_state], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path, readonly=False, claim_limit=CONCURRENCY):
        self.path = str(path)
        self.readonly = readonly
        self.claim_limit = max(1, int(claim_limit))
        if readonly:
            return  # Monitor reader must not create/migrate/modify the control database.
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, trigger_key TEXT UNIQUE NOT NULL,
              lane TEXT NOT NULL, mode TEXT NOT NULL, created REAL NOT NULL, input TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
              position INTEGER NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
              generation INTEGER NOT NULL DEFAULT 0, owner TEXT, lease_until REAL, available REAL NOT NULL DEFAULT 0,
              result TEXT, error TEXT NOT NULL DEFAULT '', UNIQUE(run_id,stage));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, run_id TEXT, at REAL, stage TEXT, message TEXT);
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,available);
            CREATE TABLE IF NOT EXISTS repairs(run_id TEXT PRIMARY KEY, at REAL NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dependencies(parent TEXT NOT NULL, child TEXT NOT NULL,
              PRIMARY KEY(parent,child));
            CREATE TABLE IF NOT EXISTS run_guidance(run_id TEXT PRIMARY KEY, instruction TEXT NOT NULL,
              updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_noise(run_id TEXT PRIMARY KEY, reason TEXT NOT NULL,
              at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_reviews(run_id TEXT PRIMARY KEY, signature TEXT NOT NULL,
              note TEXT NOT NULL, reviewed_by TEXT NOT NULL, reviewed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS trigger_aliases(key TEXT PRIMARY KEY, run_id TEXT NOT NULL);
            """)
            columns = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
            for name, definition in [('failure_kind', "TEXT NOT NULL DEFAULT ''"), ('recoveries', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute('ALTER TABLE tasks ADD COLUMN ' + name + ' ' + definition)
            run_columns = {r['name'] for r in db.execute('PRAGMA table_info(runs)')}
            if 'priority' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        target = Path(self.path).resolve().as_uri() + "?mode=ro" if self.readonly else self.path
        db = sqlite3.connect(target, timeout=LEASE_SECONDS, uri=self.readonly)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, run, stage, message, now):
        db.execute("INSERT INTO events(run_id,at,stage,message) VALUES(?,?,?,?)", (run, now, stage, message))

    def enqueue(self, key, lane, source, stages, run_id=None, mode="replay", priority=0):
        now = time.time()
        priority = max(0, int(priority))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM runs WHERE trigger_key=?", (key,)).fetchone()
            if existing:
                db.execute("UPDATE runs SET priority=MAX(priority,?) WHERE id=?", (priority, existing["id"]))
                return existing["id"]
            alias = db.execute('SELECT run_id FROM trigger_aliases WHERE key=?', (key,)).fetchone()
            if alias:
                db.execute("UPDATE runs SET priority=MAX(priority,?) WHERE id=?", (priority, alias['run_id']))
                return alias['run_id']
            if mode == 'production-batch' and lane.startswith('group:'):
                self._coalesce(db, now)
                pending = db.execute("""SELECT r.id FROM runs r WHERE r.lane=? AND r.mode='production-batch'
                    AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND (t.status!='QUEUED' OR t.attempt!=0 OR t.result IS NOT NULL))
                    AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.parent=r.id) ORDER BY r.rowid LIMIT 1""", (lane,)).fetchone()
                if pending:
                    db.execute('INSERT INTO trigger_aliases VALUES(?,?)', (key, pending['id']))
                    db.execute("UPDATE runs SET priority=MAX(priority,?) WHERE id=?", (priority, pending['id']))
                    self.event(db, pending['id'], 'COALESCE', '重复群触发已合并，保留一次后续检查' + ('并置顶' if priority else ''), now)
                    return pending['id']
            run_id = run_id or uuid.uuid4().hex
            db.execute("INSERT INTO runs(id,trigger_key,lane,mode,created,input,priority) VALUES(?,?,?,?,?,?,?)",
                       (run_id, key, lane, mode, now, json.dumps(source, ensure_ascii=False), priority))
            for position, stage in enumerate(stages):
                db.execute("INSERT INTO tasks(id,run_id,stage,position,status) VALUES(?,?,?,?,?)",
                           (uuid.uuid4().hex, run_id, stage, position, "QUEUED"))
            self.event(db, run_id, "QUEUE", "触发已持久化，等待 worker", now)
            return run_id

    def _coalesce(self, db, now):
        """Merge only untouched pulls; never alter evidence, a started run or message tasks."""
        pending = db.execute("""SELECT r.id,r.lane,r.priority FROM runs r WHERE r.mode='production-batch' AND r.lane GLOB 'group:*'
            AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND (t.status!='QUEUED' OR t.attempt!=0 OR t.result IS NOT NULL))
            AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.parent=r.id) ORDER BY r.rowid""").fetchall()
        first = {}
        for row in pending:
            if row['lane'] not in first:
                first[row['lane']] = {'id': row['id'], 'priority': row['priority']}
                continue
            target = first[row['lane']]
            db.execute("UPDATE tasks SET status='COALESCED',error=? WHERE run_id=?", ('触发合并到 ' + target['id'], row['id']))
            if row['priority'] > target['priority']:
                db.execute("UPDATE runs SET priority=? WHERE id=?", (row['priority'], target['id']))
                target['priority'] = row['priority']
            self.event(db, row['id'], 'COALESCE', '未执行的重复检查已合并到 ' + target['id'], now)

    def _recover(self, db, now):
        """Reset infrastructure only; business failures keep their evidence and repair budget."""
        for task in db.execute("""SELECT t.* FROM tasks t JOIN runs r ON r.id=t.run_id
            WHERE t.status='FAILED' AND t.failure_kind='infrastructure' AND t.available<=?
            AND r.mode IN ('production-batch','production-message')""", (now,)).fetchall():
            db.execute("UPDATE tasks SET status='QUEUED',attempt=0,recoveries=recoveries+1,owner=NULL,lease_until=NULL WHERE id=?", (task['id'],))
            # A parent held for this message must reconcile again after recovery.
            db.execute("UPDATE tasks SET status='QUEUED',error='' WHERE status='HELD' AND run_id IN (SELECT parent FROM dependencies WHERE child=?)", (task['run_id'],))
            self.event(db, task['run_id'], 'RECOVERY', '基础设施冷却后自动恢复，保留成功阶段和原始证据', now)
        db.execute("""UPDATE tasks SET status='QUEUED',error='' WHERE status='HELD'
            AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks child ON child.run_id=d.child
                WHERE d.parent=tasks.run_id AND child.status IN ('FAILED','NEEDS_REVIEW'))""")
        self._coalesce(db, now)
        # Business failures settle scheduling, not delivery: preserve cursor and failed children.
        parents = db.execute("""SELECT r.id FROM runs r WHERE r.mode='production-batch'
            AND EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.stage='fanout' AND t.status='SUCCEEDED')
            AND EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child WHERE d.parent=r.id AND t.status IN ('FAILED','NEEDS_REVIEW'))
            AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.parent=r.id
                AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=d.child AND t.status IN ('FAILED','NEEDS_REVIEW'))
                AND EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=d.child AND t.status NOT IN ('SUCCEEDED','SKIPPED','PARTIAL_REVIEW')))
            AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks t ON t.run_id=d.child WHERE d.parent=r.id AND t.status='RUNNING')
            AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.status='RUNNING')""").fetchall()
        for parent in parents:
            changed = db.execute("UPDATE tasks SET status='HELD',error='消息证据待核；水位保留，允许本群继续检查新消息' WHERE run_id=? AND stage IN ('reconcile','watermark') AND status='QUEUED'", (parent['id'],)).rowcount
            if changed:
                self.event(db, parent['id'], 'HOLD', '异常消息已隔离，未推进水位；后续检查可继续', now)

    def maintain(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._recover(db, time.time())

    def backup(self, destination):
        """SQLite's online backup includes WAL transactions without copying live files."""
        with self.connect() as db:
            target = sqlite3.connect(str(destination))
            try:
                db.backup(target)
            finally:
                target.close()

    def claim(self, owner, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db, now)
            expired = db.execute("SELECT * FROM tasks WHERE status='RUNNING' AND lease_until<=?", (now,)).fetchall()
            for t in expired:
                status = "FAILED" if t["attempt"] >= MAX_ATTEMPTS else "RETRY_WAIT"
                db.execute("UPDATE tasks SET status=?,owner=NULL,error=?,available=? WHERE id=?",
                           (status, "worker 租约过期；未执行完成，不计 DONE", now + (INFRA_RECOVERY_SECONDS if status == 'FAILED' else 0), t["id"]))
                db.execute("UPDATE tasks SET failure_kind='infrastructure' WHERE id=?", (t['id'],))
                self.event(db, t["run_id"], t["stage"], "租约过期，保留原任务恢复", now)
            if db.execute("SELECT count(*) FROM tasks WHERE status='RUNNING'").fetchone()[0] >= self.claim_limit:
                return None
            row = db.execute("""SELECT t.*,r.input,r.lane FROM tasks t JOIN runs r ON r.id=t.run_id
              WHERE t.status IN ('QUEUED','RETRY_WAIT') AND t.available<=?
              AND NOT EXISTS(SELECT 1 FROM tasks prev WHERE prev.run_id=t.run_id AND prev.position<t.position
                             AND prev.status NOT IN ('SUCCEEDED','SKIPPED','PARTIAL_REVIEW'))
              AND NOT EXISTS(SELECT 1 FROM runs same_lane JOIN tasks active ON active.run_id=same_lane.id
                WHERE same_lane.lane=r.lane AND active.status='RUNNING')
              AND NOT EXISTS(SELECT 1 FROM runs same_group JOIN dependencies d ON d.parent=same_group.id
                JOIN tasks active ON active.run_id=d.child WHERE r.mode='production-batch'
                AND same_group.lane=r.lane AND active.status='RUNNING')
              AND NOT EXISTS(SELECT 1 FROM runs older JOIN tasks ot ON ot.run_id=older.id
                WHERE older.lane=r.lane AND older.rowid<r.rowid AND ot.status NOT IN ('SUCCEEDED','SKIPPED','PARTIAL_REVIEW','COALESCED','HELD')
                  AND NOT (older.mode='production-batch' AND r.mode='production-batch'
                    AND EXISTS(SELECT 1 FROM tasks failed WHERE failed.run_id=older.id AND failed.status='FAILED')
                    AND NOT EXISTS(SELECT 1 FROM tasks active WHERE active.run_id=older.id AND active.status='RUNNING')
                    AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks active ON active.run_id=d.child WHERE d.parent=older.id AND active.status='RUNNING')
                    AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.parent=older.id
                        AND NOT EXISTS(SELECT 1 FROM tasks failed WHERE failed.run_id=d.child AND failed.status='FAILED')
                        AND EXISTS(SELECT 1 FROM tasks unfinished WHERE unfinished.run_id=d.child AND unfinished.status NOT IN ('SUCCEEDED','SKIPPED','PARTIAL_REVIEW')))))
              AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks child ON child.run_id=d.child
                WHERE d.parent=r.id AND t.stage='reconcile' AND child.status NOT IN ('SUCCEEDED','SKIPPED','PARTIAL_REVIEW'))
              ORDER BY r.priority DESC,r.rowid,t.position LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET status='RUNNING',attempt=attempt+1,generation=generation+1,owner=?,lease_until=? WHERE id=?",
                       (owner, now + LEASE_SECONDS, row["id"]))
            self.event(db, row["run_id"], row["stage"], "worker 开始执行", now)
            return dict(db.execute("SELECT t.*,r.input,r.lane,r.mode, EXISTS(SELECT 1 FROM repairs WHERE run_id=r.id) AS repair_used,(SELECT reason FROM repairs WHERE run_id=r.id) AS repair_reason,COALESCE((SELECT instruction FROM run_guidance WHERE run_id=r.id),'') AS guidance FROM tasks t JOIN runs r ON r.id=t.run_id WHERE t.id=?", (row["id"],)).fetchone())

    def pulse(self, task):
        now = time.time()
        with self.connect() as db:
            return db.execute("UPDATE tasks SET lease_until=? WHERE id=? AND generation=? AND owner=? AND status='RUNNING' AND lease_until>?",
                              (now + LEASE_SECONDS, task["id"], task["generation"], task["owner"], now)).rowcount == 1

    def active(self, task):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM tasks WHERE id=? AND generation=? AND owner=? AND status='RUNNING' AND lease_until>?",
                              (task['id'], task['generation'], task['owner'], time.time())).fetchone() is not None

    def finish(self, task, result=None, error=None, retryable=True, terminal_status=None):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=? AND generation=? AND owner=? AND status='RUNNING' AND lease_until>?",
                             (task["id"], task["generation"], task["owner"], now)).fetchone()
            if not row:
                raise ValueError("拒绝过期或其他代次的 worker 结果")
            status = "SUCCEEDED" if error is None else terminal_status if terminal_status else "RETRY_WAIT" if retryable and row["attempt"] < MAX_ATTEMPTS else "FAILED"
            delay = RETRY_SECONDS
            if status == 'FAILED' and retryable:
                delay = min(INFRA_RECOVERY_MAX_SECONDS, INFRA_RECOVERY_SECONDS * (2 ** min(row['recoveries'], RECOVERY_BACKOFF_MAX_POWER)))
            db.execute("UPDATE tasks SET status=?,result=?,error=?,owner=NULL,lease_until=NULL,available=? WHERE id=?",
                       (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error or "", now + delay if error else now, task["id"]))
            db.execute('UPDATE tasks SET failure_kind=? WHERE id=?', ('infrastructure' if error and retryable else 'business' if error else '', task['id']))
            if status == 'NEEDS_REVIEW':
                db.execute("UPDATE tasks SET status='BLOCKED',error='等待前序待核' WHERE run_id=? AND position>? AND status='QUEUED'",
                           (task['run_id'], task['position']))
            source = json.loads(task["input"])
            if error is None and task["stage"] == "classify" and result.get("decision") in ("noise", "followup"):
                if result["decision"] == "followup":
                    reason = '引用跟进只关联被引用报价，不重复生成产品'
                elif not source.get("image_path") and not source.get('media_ids'):
                    reason = '已确认噪声，有逐条分类依据'
                else:
                    reason = ''
                if reason:
                    db.execute("UPDATE tasks SET status='SKIPPED',error=? WHERE run_id=? AND status='QUEUED'",
                               (reason, task["run_id"]))
            self.event(db, task["run_id"], task["stage"], status + (": " + error if error else ""), now)

    def finish_partial(self, task, result, error):
        """Close a blocked source after its partial delivery is visibly acknowledged."""
        if not isinstance(result, dict) or result.get('status') != PARTIAL_REVIEW_COMPLETE or not result.get('ok'):
            raise ValueError('部分交付回执不完整')
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM tasks WHERE id=? AND generation=? AND owner=? AND status='RUNNING' AND lease_until>?",
                             (task['id'], task['generation'], task['owner'], now)).fetchone()
            if not row:
                raise ValueError('拒绝过期的部分交付结果')
            failure_kind = str(result.get('failure_kind') or 'business')
            db.execute("UPDATE tasks SET status=?,result=?,error=?,owner=NULL,lease_until=NULL,available=?,failure_kind=? WHERE id=?",
                       (PARTIAL_REVIEW, json.dumps(result, ensure_ascii=False), str(error or ''), now,
                        failure_kind, task['id']))
            skipped = db.execute("UPDATE tasks SET status='SKIPPED',error='部分解析已送待核并读回，未闭合阶段停止自动推进' "
                                 "WHERE run_id=? AND position>? AND status IN ('QUEUED','RETRY_WAIT','BLOCKED')",
                                 (task['run_id'], task['position'])).rowcount
            released = db.execute("UPDATE tasks SET status='QUEUED',error='' WHERE status='HELD' "
                                  "AND run_id IN (SELECT parent FROM dependencies WHERE child=?)",
                                  (task['run_id'],)).rowcount
            self.event(db, task['run_id'], task['stage'], PARTIAL_REVIEW_COMPLETE + ': ' + str(error or ''), now)
            return {'status': PARTIAL_REVIEW_COMPLETE, 'skipped_downstream': skipped,
                    'released_parents': released}

    def detail(self, run_id, include_events=True):
        with self.connect() as db:
            r = db.execute("SELECT id,trigger_key,lane,mode,created,input FROM runs WHERE id=?", (run_id,)).fetchone()
            if not r:
                raise ValueError("轮次不存在")
            tasks = [dict(t) for t in db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY position", (run_id,))]
            closed = ('SUCCEEDED', 'SKIPPED', PARTIAL_REVIEW)
            state = "COALESCED" if all(t['status'] == 'COALESCED' for t in tasks) else "NEEDS_REVIEW" if any(t['status'] in ('HELD','NEEDS_REVIEW') for t in tasks) else "BLOCKED" if any(t['status'] == 'BLOCKED' for t in tasks) else "FAILED" if any(t["status"] == "FAILED" for t in tasks) else "RUNNING" if any(t["status"] == "RUNNING" for t in tasks) else "RETRY_WAIT" if any(t["status"] == "RETRY_WAIT" for t in tasks) else PARTIAL_REVIEW_COMPLETE if any(t['status'] == PARTIAL_REVIEW for t in tasks) and all(t['status'] in closed for t in tasks) else "DONE" if all(t["status"] in closed for t in tasks) else "QUEUED"
            children = [dict(c) for c in db.execute("""
                SELECT r.id,r.lane,r.created,
                    COALESCE(json_extract(r.input,'$.message_id'),'') AS message_id,
                    COALESCE(json_extract(r.input,'$.message_time'),'') AS message_time,
                    COALESCE(json_extract(r.input,'$.text'),'') AS message_text,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.status='FAILED') AS failed,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.status='NEEDS_REVIEW') AS needs_review,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.status='PARTIAL_REVIEW') AS partial_review,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.status='RUNNING') AS running,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.status IN ('QUEUED','RETRY_WAIT','WAITING','WAITING_CHILDREN','RUNNING')) AS waiting,
                    COALESCE((SELECT GROUP_CONCAT(status) FROM tasks WHERE run_id=r.id),'') AS task_statuses,
                    COALESCE((SELECT GROUP_CONCAT(error,'||') FROM tasks WHERE run_id=r.id AND error!=''),'') AS error_state,
                    COALESCE((SELECT stage FROM tasks WHERE run_id=r.id AND status='RUNNING' ORDER BY position LIMIT 1),'') AS current_stage,
                    COALESCE((SELECT stage FROM tasks WHERE run_id=r.id AND status IN ('FAILED','HELD','NEEDS_REVIEW','BLOCKED') ORDER BY position DESC LIMIT 1),'') AS stopped_stage,
                    COALESCE((SELECT json_extract(result,'$.decision') FROM tasks WHERE run_id=r.id AND stage='classify' ORDER BY position DESC LIMIT 1),'') AS classify_decision,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.stage='submit' AND t.status='SUCCEEDED') AS submitted,
                    EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id AND t.stage='readback' AND t.status='SUCCEEDED'
                        AND COALESCE(json_extract(t.result,'$.ok'),1)=1
                        AND COALESCE(json_array_length(json_extract(t.result,'$.covered_ids')),0)>=COALESCE(json_extract(t.result,'$.expected'),0)) AS readback_succeeded,
                    EXISTS(SELECT 1 FROM run_noise n WHERE n.run_id=r.id) AS noise
                FROM dependencies d JOIN runs r ON r.id=d.child
                WHERE d.parent=? ORDER BY r.created,r.id
            """, (run_id,))]
            if state == 'QUEUED' and any(c['failed'] for c in children):
                state = 'BLOCKED'
            elif state == 'QUEUED' and any(c['needs_review'] for c in children):
                state = 'NEEDS_REVIEW'
            elif state == 'QUEUED' and any(c['waiting'] for c in children):
                state = 'WAITING_CHILDREN'
            data = dict(r)
            source = json.loads(data.pop('input'))
            guidance = db.execute('SELECT instruction FROM run_guidance WHERE run_id=?', (run_id,)).fetchone()
            noise = db.execute('SELECT reason,at FROM run_noise WHERE run_id=?', (run_id,)).fetchone()
            review = db.execute('SELECT signature,note,reviewed_by,reviewed_at FROM run_reviews WHERE run_id=?', (run_id,)).fetchone()
            pull_finished_at = db.execute("""SELECT MAX(at) FROM events WHERE run_id=? AND stage='pull'""",
                                           (run_id,)).fetchone()[0]
            events = ([dict(e) for e in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
                      if include_events else [])
            signature = review_signature(tasks, children)
            review_data = dict(review) if review else None
            reviewed = bool(review_data and review_data.get("signature") == signature)
            if review_data:
                review_data["active"] = reviewed
            return {**data, 'source': source, 'message_id': source.get('message_id', ''), 'children': children,
                    "status": state, "tasks": tasks, "pull_finished_at": pull_finished_at,
                    "repair_guidance": guidance['instruction'] if guidance else '',
                    "noise_resolution": dict(noise) if noise else None,
                    "review": review_data, "reviewed": reviewed,
                    "events": events}

    def runs(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT ?", (LIST_LIMIT,))]
        return [self.detail(i) for i in ids]

    def task_summary(self):
        with self.connect() as db:
            by_status = {row['status']: int(row['count']) for row in db.execute(
                """SELECT t.status,count(*) AS count FROM tasks t JOIN runs r ON r.id=t.run_id
                   WHERE r.mode!='history-batch'
                     AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN runs p ON p.id=d.parent
                                    WHERE d.child=r.id AND p.mode='history-batch')
                   GROUP BY t.status""")}
            historical = {row['status']: int(row['count']) for row in db.execute(
                """SELECT t.status,count(*) AS count FROM tasks t JOIN runs r ON r.id=t.run_id
                   WHERE r.mode='history-batch'
                      OR EXISTS(SELECT 1 FROM dependencies d JOIN runs p ON p.id=d.parent
                                WHERE d.child=r.id AND p.mode='history-batch')
                   GROUP BY t.status""")}
            oldest = db.execute("""SELECT MIN(t.available) FROM tasks t JOIN runs r ON r.id=t.run_id
                                   WHERE r.mode!='history-batch'
                                     AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN runs p ON p.id=d.parent
                                                    WHERE d.child=r.id AND p.mode='history-batch')
                                     AND t.status IN ('QUEUED','RETRY_WAIT')""").fetchone()[0]
        return {
            'by_status': by_status,
            'scope': 'live',
            'historical_by_status': historical,
            'active': sum(by_status.get(name, 0) for name in ('QUEUED', 'RETRY_WAIT', 'RUNNING')),
            'failed': by_status.get('FAILED', 0),
            'needs_review': by_status.get('HELD', 0) + by_status.get('NEEDS_REVIEW', 0),
            'partial_review': by_status.get(PARTIAL_REVIEW, 0),
            'oldest_available_at': oldest,
        }

    def recent_triggers(self, limit=12):
        with self.connect() as db:
            ids = [row[0] for row in db.execute(
                'SELECT id FROM runs ORDER BY created DESC LIMIT ?', (int(limit),))]
        result = []
        for run_id in ids:
            run = self.detail(run_id, include_events=False)
            tasks = run['tasks']
            running = next((task for task in tasks if task['status'] == 'RUNNING'), None)
            group = run['source'].get('group') or {}
            result.append({
                'id': run['id'],
                'trigger_key': run['trigger_key'],
                'lane': run['lane'],
                'mode': run['mode'],
                'created': run['created'],
                'status': run['status'],
                'title': group.get('name') or run['source'].get('message_id') or run['lane'],
                'task_count': len(tasks),
                'completed': sum(task['status'] in ('SUCCEEDED', 'SKIPPED', PARTIAL_REVIEW) for task in tasks),
                'current_stage': running['stage'] if running else '',
                'message_count': len(run.get('children') or []),
            })
        return result

    def running_tasks(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT t.id,t.run_id,t.stage,t.owner,t.attempt,
                t.generation,t.lease_until,r.trigger_key,r.lane,r.mode
                FROM tasks t JOIN runs r ON r.id=t.run_id
                WHERE t.status='RUNNING' ORDER BY t.lease_until""")]

    def trace_roots(self, limit=LIST_LIMIT):
        with self.connect() as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM runs WHERE mode='production-batch' ORDER BY created DESC LIMIT ?", (limit,))]
        return [self.detail(i, include_events=False) for i in ids]

    def events_after(self, seq, limit=100):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT seq,run_id,at,stage,message FROM events WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit))]

    def latest_event_seq(self):
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()
            return int(row[0] or 0)

    def results(self, run_id):
        return {t["stage"]: json.loads(t["result"]) for t in self.detail(run_id)["tasks"] if t["result"] is not None}

    def trigger(self, key):
        with self.connect() as db:
            row = db.execute("SELECT id FROM runs WHERE trigger_key=?", (key,)).fetchone()
            alias = db.execute('SELECT run_id FROM trigger_aliases WHERE key=?', (key,)).fetchone()
            return row['id'] if row else alias['run_id'] if alias else None

    def has_trigger_prefix(self, prefix):
        with self.connect() as db:
            pattern = str(prefix) + ':%'
            return bool(db.execute(
                "SELECT 1 FROM runs WHERE trigger_key LIKE ? "
                "UNION ALL SELECT 1 FROM trigger_aliases WHERE key LIKE ? LIMIT 1",
                (pattern, pattern)).fetchone())

    def depend(self, parent, child):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO dependencies VALUES(?,?)', (parent, child))

    def children(self, parent):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT child FROM dependencies WHERE parent=?', (parent,))]
        return [self.detail(i, include_events=False) for i in ids]

    def cancel_history(self, reason):
        """Cancel unstarted historical roots and their dependent children only."""
        reason = str(reason or '用户取消历史回补').strip()[:500]
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            roots = [r['id'] for r in db.execute("SELECT id FROM runs WHERE mode='history-batch'")]
            if not roots:
                return {'roots': 0, 'runs': 0, 'cancelled_tasks': 0}
            cte = """WITH RECURSIVE tree(id) AS (
                SELECT id FROM runs WHERE mode='history-batch'
                UNION SELECT d.child FROM dependencies d JOIN tree ON d.parent=tree.id
            )"""
            running = db.execute(cte + " SELECT COUNT(*) FROM tasks WHERE run_id IN (SELECT id FROM tree) AND status='RUNNING'").fetchone()[0]
            if running:
                raise ValueError('历史回补仍有 RUNNING 任务，请先暂停 Worker 并等待收尾')
            run_count = db.execute(cte + " SELECT COUNT(*) FROM tree").fetchone()[0]
            pending_statuses = "('QUEUED','RETRY_WAIT','FAILED','BLOCKED','HELD')"
            pending_count = db.execute(cte + " SELECT COUNT(*) FROM tasks WHERE run_id IN (SELECT id FROM tree)"
                                       " AND status IN " + pending_statuses).fetchone()[0]
            changed = db.execute(cte + """ UPDATE tasks SET status='SKIPPED',error=?,owner=NULL,
                lease_until=NULL,available=0 WHERE run_id IN (SELECT id FROM tree)
                AND status IN ('QUEUED','RETRY_WAIT','FAILED','BLOCKED','HELD')""", (reason,)).rowcount
            if changed < 0:
                changed = pending_count
            for run_id in roots:
                self.event(db, run_id, 'CANCEL', reason, now)
            return {'roots': len(roots), 'runs': run_count, 'cancelled_tasks': changed}

    def resume(self, run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM tasks WHERE run_id=? AND status='RUNNING'", (run_id,)).fetchone():
                raise ValueError('任务正在运行，不能重置')
            count = db.execute("UPDATE tasks SET status='QUEUED',attempt=0,error='',available=0 WHERE run_id=? AND status IN ('FAILED','NEEDS_REVIEW','BLOCKED')", (run_id,)).rowcount
            db.execute("UPDATE tasks SET status='QUEUED',error='' WHERE status='HELD' AND run_id IN (SELECT parent FROM dependencies WHERE child=?)", (run_id,))
            self.event(db, run_id, 'RESUME', '修复执行原因后继续未完成阶段，不清除成功产物', time.time())
            return count

    def recheck_inventory(self, run_id):
        """Code/rule fix recovery, not an automatic extra business repair budget."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            failed = db.execute("SELECT 1 FROM tasks WHERE run_id=? AND stage='coverage' AND status IN ('FAILED','NEEDS_REVIEW')", (run_id,)).fetchone()
            if not failed:
                raise ValueError('仅适用于覆盖阶段已失败的轮次')
            db.execute("UPDATE tasks SET status='QUEUED',attempt=0,result=NULL,error='',available=0 WHERE run_id=? AND stage IN ('inventory','coverage')", (run_id,))
            self.event(db, run_id, 'RESUME', '执行规则修复后重核独立盘点，保留来源与解析，修正预算不重置', time.time())

    def recheck_source(self, run_id, instruction=None):
        """Explicit code-fix maintenance only; preserve evidence and consumed repair budget."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM tasks WHERE run_id=? AND (status='RUNNING' OR (stage IN ('submit','readback') AND status='SUCCEEDED'))", (run_id,)).fetchone():
                raise ValueError('已送审或正在运行的轮次不能重新解析')
            if not db.execute("SELECT 1 FROM tasks WHERE run_id=? AND status IN ('FAILED','NEEDS_REVIEW') AND stage IN ('coverage','audit','normalize')", (run_id,)).fetchone():
                raise ValueError('仅适用于代码修复后未送审的业务失败轮次')
            if instruction is not None:
                text = str(instruction).strip()
                if len(text) > 2000:
                    raise ValueError('探针指令最多 2000 字')
                db.execute("INSERT INTO run_guidance(run_id,instruction,updated_at) VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET instruction=excluded.instruction,updated_at=excluded.updated_at",
                           (run_id, text, time.time()))
            db.execute("UPDATE tasks SET status='QUEUED',attempt=0,result=NULL,error='',owner=NULL,lease_until=NULL,available=0 WHERE run_id=? AND stage IN ('extract','inventory','coverage','audit','normalize','validate','submit','readback')", (run_id,))
            self.event(db, run_id, 'RESUME', '代码与契约修复后重新解析原始证据；保留修正预算和已下载原图', time.time())

    def amend_result(self, run_id, stage, result, instruction=None):
        """Accept a reviewed structured override, then reopen only downstream stages."""
        if stage not in ('classify', 'extract', 'inventory'):
            raise ValueError('只能修改分类、抽取或盘点结果')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            target = db.execute('SELECT position,status FROM tasks WHERE run_id=? AND stage=?', (run_id, stage)).fetchone()
            if not target:
                raise ValueError('目标阶段不存在')
            if target['status'] == 'RUNNING':
                raise ValueError('目标阶段正在运行，不能修改')
            db.execute("UPDATE tasks SET status='SUCCEEDED',result=?,error='',failure_kind='',available=0 WHERE id IN (SELECT id FROM tasks WHERE run_id=? AND stage=?)",
                       (json.dumps(result, ensure_ascii=False), run_id, stage))
            db.execute("UPDATE tasks SET status='QUEUED',attempt=0,generation=generation+1,result=NULL,error='',failure_kind='',owner=NULL,lease_until=NULL,available=0 WHERE run_id=? AND position>?",
                       (run_id, target['position']))
            if instruction is not None:
                text = str(instruction).strip()
                if len(text) > 2000:
                    raise ValueError('探针指令最多 2000 字')
                db.execute("INSERT INTO run_guidance(run_id,instruction,updated_at) VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET instruction=excluded.instruction,updated_at=excluded.updated_at",
                           (run_id, text, time.time()))
            db.execute("UPDATE tasks SET status='QUEUED',error='' WHERE status='HELD' AND run_id IN (SELECT parent FROM dependencies WHERE child=?)", (run_id,))
            self.event(db, run_id, stage, '人工修正结构化结果，重开后续阶段', time.time())
            return {'stage': stage, 'downstream_reopened': True}

    def mark_noise(self, run_id, reason):
        """Soft-skip this source after an operator decision; preserve all evidence."""
        text = str(reason or '').strip()
        if len(text) < 2 or len(text) > 1000:
            raise ValueError('判噪原因需为 2-1000 字')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise ValueError('轮次不存在')
            db.execute("INSERT INTO run_noise(run_id,reason,at) VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET reason=excluded.reason,at=excluded.at",
                       (run_id, text, time.time()))
            changed = db.execute("UPDATE tasks SET status='SKIPPED',error=?,owner=NULL,lease_until=NULL WHERE run_id=? AND status IN ('FAILED','NEEDS_REVIEW','BLOCKED','QUEUED','RETRY_WAIT')",
                                 ('人工判噪: ' + text, run_id)).rowcount
            self.event(db, run_id, 'noise', '人工确认噪声，保留原文与原图: ' + text, time.time())
            return {'skipped_tasks': changed}

    def acknowledge_review(self, run_id, note, reviewed_by='operator'):
        """Acknowledge the exact current issue state without changing task outcomes."""
        text = str(note or '').strip()
        actor = str(reviewed_by or 'operator').strip()[:100]
        if len(text) < 2 or len(text) > 1000:
            raise ValueError('处置说明需为 2-1000 字')
        detail = self.detail(run_id, include_events=False)
        if detail['status'] in ('RUNNING', 'QUEUED', 'RETRY_WAIT', 'WAITING_CHILDREN') or any(
                child.get('waiting') for child in detail.get('children') or []):
            raise ValueError('轮次仍在执行，不能标记已处置')
        issue = detail['status'] in ('FAILED', 'BLOCKED', 'NEEDS_REVIEW', 'HELD', 'PARTIAL_REVIEW_COMPLETE') or any(
            child.get('failed') or child.get('needs_review') or child.get('partial_review')
            for child in detail.get('children') or [])
        if not issue:
            raise ValueError('当前轮次没有需要处置的失败或待核状态')
        signature = review_signature(detail.get('tasks') or [], detail.get('children') or [])
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO run_reviews(run_id,signature,note,reviewed_by,reviewed_at)
                VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET signature=excluded.signature,
                note=excluded.note,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at""",
                       (run_id, signature, text, actor, now))
            self.event(db, run_id, 'review', '人工确认已处置: ' + text, now)
        return {'reviewed': True, 'signature': signature, 'reviewed_by': actor, 'reviewed_at': now}

    def repair(self, task, reason):
        """Exactly one independent source reread; never loops until business pass."""
        if task["stage"] not in ("coverage", "audit"):
            return False
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT 1 FROM tasks WHERE id=? AND generation=? AND owner=? AND status='RUNNING' AND lease_until>?",
                                (task["id"], task["generation"], task["owner"], now)).fetchone()
            if not active or db.execute("SELECT 1 FROM repairs WHERE run_id=?", (task["run_id"],)).fetchone():
                return False
            db.execute("INSERT INTO repairs VALUES(?,?,?)", (task["run_id"], now, reason))
            db.execute("UPDATE tasks SET status='QUEUED',result=NULL,error='',owner=NULL,lease_until=NULL,available=0 WHERE run_id=? AND stage IN ('extract','inventory','coverage','audit')", (task["run_id"],))
            self.event(db, task["run_id"], "REPAIR", "一次独立重读来源修正: " + reason, now)
            return True
