"""Local operational queue, separate from the freight database."""
import json
import secrets
import sqlite3
import time
from pending_quality.core.contracts import LEASE_SECONDS, MAX_INTERRUPTED_ATTEMPTS, SQLITE_BUSY_TIMEOUT_SECONDS, CLAIM_TOKEN_BYTES

class Store:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.c=sqlite3.connect(path,timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        self.c.row_factory=sqlite3.Row
        self.c.execute('CREATE TABLE IF NOT EXISTS reviews (source_key TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL, priority INTEGER NOT NULL, oldest_id INTEGER NOT NULL, created_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL, claim TEXT, report TEXT, updated_at REAL NOT NULL, PRIMARY KEY(source_key,fingerprint))')
        self.c.commit()
    def close(self):self.c.close()
    def sync(self,items):
        now=time.time()
        with self.c:
            self.c.execute("UPDATE reviews SET status='needs_human', report=?, updated_at=? WHERE status='claimed' AND lease_until<? AND attempts>=?",(json.dumps({'note':'审查重复中断，保留待审并转人工，不继续自动重试'},ensure_ascii=False),now,now,MAX_INTERRUPTED_ATTEMPTS))
            self.c.execute("UPDATE reviews SET status='pending',claim=NULL WHERE status='claimed' AND lease_until<? AND attempts<?",(now,MAX_INTERRUPTED_ATTEMPTS))
            current={(i['key'],i['fingerprint']) for i in items}
            # Old snapshots are history; changed source versions receive separate reviews.
            for row in self.c.execute("SELECT source_key,fingerprint FROM reviews WHERE status='pending'").fetchall():
                if (row['source_key'],row['fingerprint']) not in current:
                    self.c.execute("UPDATE reviews SET status='superseded',updated_at=? WHERE source_key=? AND fingerprint=?",(now,row['source_key'],row['fingerprint']))
            for i in items:
                self.c.execute('INSERT OR IGNORE INTO reviews(source_key,fingerprint,status,snapshot,priority,oldest_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(i['key'],i['fingerprint'],'pending',json.dumps(i,ensure_ascii=False),i['priority'],i['oldest_id'],now,now))
                self.c.execute("UPDATE reviews SET priority=? WHERE source_key=? AND fingerprint=? AND status='pending'",(i['priority'],i['key'],i['fingerprint']))
    def claim(self,limit):
        now=time.time();self.c.execute('BEGIN IMMEDIATE')
        try:
            rows=self.c.execute("SELECT * FROM reviews WHERE status='pending' AND source_key NOT IN (SELECT source_key FROM reviews WHERE status='claimed') ORDER BY priority DESC,oldest_id ASC LIMIT ?",(limit,)).fetchall()
            result=[]
            for row in rows:
                token=secrets.token_hex(CLAIM_TOKEN_BYTES)
                self.c.execute("UPDATE reviews SET status='claimed',claim=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE source_key=? AND fingerprint=?",(token,now+LEASE_SECONDS,now,row['source_key'],row['fingerprint']))
                result.append(dict(json.loads(row['snapshot']),claim=token,lease_until=now+LEASE_SECONDS))
            self.c.commit();return result
        except Exception:
            self.c.rollback();raise
    def get_claim(self,token):
        row=self.c.execute("SELECT * FROM reviews WHERE claim=? AND status='claimed'",(token,)).fetchone()
        if not row or row['lease_until']<time.time():raise ValueError('claim is missing or expired; do not write business data')
        return dict(json.loads(row['snapshot']),claim=token)
    def finish(self,token,after,report):
        self.c.execute('BEGIN IMMEDIATE')
        try:
            original=self.get_claim(token);now=time.time()
            self.c.execute('UPDATE reviews SET status=?,report=?,updated_at=?,lease_until=NULL WHERE claim=?',(report['outcome'],json.dumps(report,ensure_ascii=False),now,token))
            # A repair itself must not cause another automatic round on the same source.
            self.c.execute('INSERT INTO reviews(source_key,fingerprint,status,snapshot,priority,oldest_id,created_at,updated_at,report) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key,fingerprint) DO UPDATE SET status=excluded.status,report=excluded.report,updated_at=excluded.updated_at',(after['key'],after['fingerprint'],report['outcome'],json.dumps(after,ensure_ascii=False),after['priority'],after['oldest_id'],now,now,json.dumps(report,ensure_ascii=False)))
            self.c.commit()
        except Exception:self.c.rollback();raise
    def status(self):
        return {r['status']:r['n'] for r in self.c.execute('SELECT status,COUNT(*) n FROM reviews GROUP BY status')}
