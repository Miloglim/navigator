import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.error import URLError
from types import SimpleNamespace
from freight_flow.core.contracts import LEASE_SECONDS, MAX_ATTEMPTS
from freight_flow.core.errors import AgentAuthenticationError
from freight_flow.services.workflow import coverage, plan, validate_schema
from freight_flow.transport.store import Store
from freight_flow.transport.server import server
from freight_flow.transport.agent import SCHEMAS
from freight_flow.transport.worker import work
from freight_flow.transport.production import Production
from freight_flow.services.trace import trace_detail, trace_index
from orchestrator import enqueue_fixture

SOURCE = {"message_id": "test", "text": "Santos 5600/5900", "group_knowledge": "仅使用原文"}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "control.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def queue(self, key="a", lane="A", stages=("classify", "extract")):
        return self.store.enqueue(key, lane, SOURCE, stages)

    def test_duplicate_trigger_and_restart(self):
        first = self.queue()
        self.assertEqual(first, self.queue())
        reopened = Store(self.root / "control.sqlite3")
        self.assertEqual(reopened.detail(first)["status"], "QUEUED")
        task = reopened.claim("worker")
        self.assertEqual(task["run_id"], first)

    def test_monitor_store_cannot_write(self):
        self.queue()
        reader = Store(self.root / "control.sqlite3", readonly=True)
        self.assertEqual(len(reader.runs()), 1)
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            reader.enqueue("forbidden", "A", SOURCE, ("classify",))

    def test_new_trigger_preserves_old_and_lane_fifo(self):
        old = self.queue("old")
        task = self.store.claim("one")
        new = self.queue("new")
        self.assertIsNone(self.store.claim("two"))
        self.assertEqual(self.store.detail(old)["tasks"][0]["generation"], task["generation"])
        self.assertEqual(self.store.detail(new)["status"], "QUEUED")

    def test_manual_trigger_promotes_coalesced_group_run(self):
        source = {**SOURCE, 'group': {'slug': 'manual_group'}}
        automatic = self.store.enqueue('engine:manual_group', 'group:manual_group', source,
                                       ('pull', 'fanout'), mode='production-batch')
        other = self.store.enqueue('engine:other_group', 'group:other_group', source,
                                   ('pull', 'fanout'), mode='production-batch')
        manual = self.store.enqueue('manual:manual_group', 'group:manual_group', source,
                                    ('pull', 'fanout'), mode='production-batch', priority=1)
        self.assertEqual(manual, automatic, 'manual click reuses an untouched queued pull')
        with self.store.connect() as db:
            priority = db.execute('SELECT priority FROM runs WHERE id=?', (automatic,)).fetchone()['priority']
        self.assertEqual(priority, 1)
        self.assertEqual(self.store.claim('worker')['run_id'], automatic,
                         'manual group pull is claimed before older automatic work in other lanes')
        self.assertEqual(self.store.detail(other)['status'], 'QUEUED')

    def test_global_cap_across_process_connections(self):
        for lane in ("A", "B", "C"):
            self.queue(lane, lane)
        stores = [Store(self.root / "control.sqlite3") for _ in range(3)]
        tasks = []
        threads = [threading.Thread(target=lambda s=s: tasks.append(s.claim("owner"))) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(t is not None for t in tasks), 2)

    def test_business_failure_not_retried_as_infrastructure(self):
        run = self.queue(stages=("coverage", "audit"))
        task = self.store.claim("worker")
        self.store.finish(task, error="有价格未覆盖", retryable=False)
        self.assertEqual(self.store.detail(run)["status"], "FAILED")
        self.assertEqual(self.store.detail(run)["tasks"][0]["attempt"], 1)
        self.assertIsNone(self.store.claim("worker"))

    def test_review_issue_network_failure_does_not_kill_worker_or_block_next_lane(self):
        source = {**SOURCE, 'group': {'slug': 'isolated'}}
        failed = self.store.enqueue('failed', 'message:first', source, ('prepare',), mode='production-message')
        following = self.store.enqueue('following', 'message:next', source, ('prepare',), mode='production-message')
        stop = threading.Event()
        progressed = threading.Event()
        escaped = []
        reported = []

        def report_issues(issues):
            reported.extend(issues)
            raise URLError('isolated ledger unavailable')

        production = object.__new__(Production)
        production.ledger = SimpleNamespace(report_review_issues=report_issues)

        def execute(task, results):
            if task['run_id'] == failed:
                raise ValueError('original source validation failed')
            progressed.set()
            stop.set()
            return {'ok': True}

        adapter = SimpleNamespace(execute=execute, report_failure=production.report_failure)

        def run_worker():
            try:
                work(self.store, adapter, stop)
            except Exception as exc:
                escaped.append(exc)

        with self.assertLogs('worker', level='ERROR') as logs:
            thread = threading.Thread(target=run_worker)
            thread.start()
            try:
                continued = progressed.wait(1)
            finally:
                stop.set()
                thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(escaped, [], 'notification network errors must not escape worker loop')
        self.assertTrue(continued, 'same worker executes subsequent independent lane')
        first_task = self.store.detail(failed)['tasks'][0]
        self.assertEqual(first_task['status'], 'NEEDS_REVIEW')
        self.assertEqual(first_task['attempt'], 1)
        self.assertEqual(first_task['error'], 'original source validation failed')
        self.assertIsNone(first_task['lease_until'])
        self.assertEqual(self.store.detail(following)['status'], 'DONE')
        self.assertEqual(reported[0]['message_id'], source['message_id'])
        self.assertTrue(any('isolated ledger unavailable' in line for line in logs.output), 'notification failure must be logged')

    def test_worker_gate_pauses_new_claims_until_enabled(self):
        run = self.queue(stages=("coverage",))
        gate = threading.Event()
        stop = threading.Event()
        progressed = threading.Event()

        class Adapter:
            def execute(self, task, results):
                progressed.set()
                stop.set()
                return {"ok": True}

        thread = threading.Thread(target=work, args=(self.store, Adapter(), stop),
                                  kwargs={"owner": "engine-worker-test", "gate": gate})
        thread.start()
        try:
            time.sleep(0.1)
            self.assertFalse(progressed.is_set())
            self.assertEqual(self.store.detail(run)["tasks"][0]["status"], "QUEUED")
            gate.set()
            self.assertTrue(progressed.wait(3))
        finally:
            stop.set()
            thread.join(1)
        self.assertEqual(self.store.detail(run)["status"], "DONE")

    def test_agent_auth_failure_stops_further_claims_without_retry(self):
        first = self.queue("auth-first", stages=("classify",))
        second = self.queue("auth-second", stages=("classify",))
        gate = threading.Event()
        gate.set()
        stop = threading.Event()
        blocked = threading.Event()
        reasons = []

        class Adapter:
            def execute(self, task, results):
                raise AgentAuthenticationError("Codex 模型通道认证失败: HTTP 401/403 Unauthorized")

        def block(reason, state):
            reasons.append(reason)
            self.assertEqual(state, "authentication_required")
            gate.clear()
            blocked.set()

        thread = threading.Thread(target=work, args=(self.store, Adapter(), stop),
                                  kwargs={"owner": "engine-worker-auth", "gate": gate,
                                          "on_agent_block": block})
        thread.start()
        try:
            self.assertTrue(blocked.wait(1))
            time.sleep(0.1)
            self.assertFalse(gate.is_set())
            self.assertEqual(self.store.detail(first)["tasks"][0]["status"], "FAILED")
            self.assertEqual(self.store.detail(second)["tasks"][0]["status"], "QUEUED")
        finally:
            stop.set()
            thread.join(1)
        self.assertIn("认证失败", reasons[0])

    def test_exactly_one_repair_rejects_old_results(self):
        run = self.queue(stages=("extract", "inventory", "coverage", "audit"))
        for stage in ("extract", "inventory"):
            task = self.store.claim("w")
            self.store.finish(task, result={})
        first = self.store.claim("w")
        self.assertTrue(self.store.repair(first, "错配"))
        with self.assertRaises(ValueError):
            self.store.finish(first, result={})
        for stage in ("extract", "inventory"):
            task = self.store.claim("w")
            self.assertEqual(task["stage"], stage)
            self.assertTrue(task["repair_used"])
            self.store.finish(task, result={})
        task = self.store.claim("w")
        self.assertFalse(self.store.repair(task, "仍错配"))
        self.store.finish(task, error="仍错配", retryable=False)
        self.assertEqual(self.store.detail(run)["status"], "FAILED")

    def test_expired_generation_cannot_deliver(self):
        run = self.queue()
        first = self.store.claim("old")
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, first["id"]))
        second = self.store.claim("new")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["generation"], first["generation"] + 1)
        with self.assertRaisesRegex(ValueError, "过期"):
            self.store.finish(first, result={"decision": "quote"})
        self.store.finish(second, result={"decision": "quote"})
        self.assertEqual(self.store.detail(run)["tasks"][0]["status"], "SUCCEEDED")

    def test_failed_image_never_done_or_extract(self):
        run = self.queue(stages=("vision", "extract"))
        for attempt in range(MAX_ATTEMPTS):
            task = self.store.claim("worker")
            self.assertEqual(task["stage"], "vision")
            self.store.finish(task, error="没有实际图像结果")
            with self.store.connect() as db:
                db.execute("UPDATE tasks SET available=0 WHERE id=?", (task["id"],))
        self.assertEqual(self.store.detail(run)["status"], "FAILED")
        self.assertIsNone(self.store.claim("worker"))
        self.assertIsNone(self.store.detail(run)["tasks"][1]["result"])

    def test_image_noise_still_requires_visual_work(self):
        run = self.store.enqueue("image-noise", "A", {**SOURCE, "image_path": "evidence.png"}, ("classify", "vision", "audit"))
        task = self.store.claim("worker")
        self.store.finish(task, result={"decision": "noise", "reason": "text empty"})
        self.assertEqual(self.store.claim("worker")["stage"], "vision")
        self.assertNotEqual(self.store.detail(run)["status"], "DONE")

    def test_quoted_followup_skips_downstream_even_with_image(self):
        run = self.store.enqueue("followup", "A", {**SOURCE, "image_path": "evidence.png"},
                                 ("classify", "vision", "audit"))
        task = self.store.claim("worker")
        self.store.finish(task, result={"decision": "followup", "reason": "引用补充",
                                        "review_issue_acknowledged": True})
        detail = self.store.detail(run)
        self.assertEqual(detail["status"], "DONE")
        self.assertEqual([item["status"] for item in detail["tasks"]],
                         ["SUCCEEDED", "SKIPPED", "SKIPPED"])

    def test_same_amount_different_ports_cannot_mask_missing(self):
        record = {"目的港": "Santos", "柜型": "20GP", "海运费USD": 5600}
        inventory = [{"pod": "Santos", "container": "20GP", "price": 5600, "location": "r1"},
                     {"pod": "Paranagua", "container": "20GP", "price": 5600, "location": "r2"}]
        with self.assertRaisesRegex(ValueError, "Paranagua"):
            coverage([record], inventory)

    def test_non_usd_schema_and_coverage(self):
        row = {"_table": "rate", "section_role": "product", "起运港": "南沙", "目的港": "Lattakia", "船司": "", "柜型": "20GP",
               "海运费USD": "见原图", "证据": "r2c3", "备注": "EUR 1000",
               "ETD":"", "有效期开始":"", "有效期截止":"", "service":"", "tag":""}
        validate_schema({"records": [row]}, SCHEMAS["extract"])
        self.assertTrue(coverage([row], [{"pod": "Lattakia", "container": "20GP", "price": "见原图", "location": "r2c3", "起运港":"南沙"}])["ok"])

    def test_container_alias_and_voyage_dimensions(self):
        row = {"目的港":"Charleston", "柜型":"40HQ/HC", "海运费USD":7500, "ETD":"10/14"}
        cell = {"pod":"Charleston", "container":"40HQ", "price":7500, "ETD":"10/14"}
        self.assertTrue(coverage([row],[cell])["ok"])
        with self.assertRaisesRegex(ValueError, "10/21"):
            coverage([row], [cell, {**cell, "ETD":"10/21"}])

    def test_immutable_image_snapshot_and_unique_artifacts(self):
        image = self.root / "image.png"
        image.write_bytes(b"original evidence")
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({**SOURCE, "image_path": str(image)}), encoding="utf-8")
        first = enqueue_fixture(self.store, self.root, fixture, "first", "A")
        image.write_bytes(b"changed")
        second = enqueue_fixture(self.store, self.root, fixture, "second", "A")
        t = self.store.claim("w")
        self.assertEqual(Path(json.loads(t["input"])["image_path"]).read_bytes(), b"original evidence")
        self.assertNotEqual(first, second)
        self.assertIn("vision", plan(json.loads(t["input"])))

    def test_quoted_image_does_not_start_vision_stage(self):
        fixture = self.root / "quoted-image.json"
        fixture.write_text(json.dumps({
            **SOURCE, "message_time": "2026-10-23 10:00:00",
            "group": {"name": "g", "cid": "c", "slug": "g"},
            "media_ids": ["@quoted"], "display_media_ids": ["@quoted"],
            "parse_media_ids": [], "quoted_media_ids": ["@quoted"],
        }, ensure_ascii=False), encoding="utf-8")
        run = enqueue_fixture(self.store, self.root, fixture, "quoted-image", "A", mode="production-message")
        stages = [task["stage"] for task in self.store.detail(run)["tasks"]]
        self.assertNotIn("vision", stages)

    def test_own_image_starts_vision_stage(self):
        fixture = self.root / "own-image.json"
        fixture.write_text(json.dumps({
            **SOURCE, "message_time": "2026-10-23 10:00:00",
            "group": {"name": "g", "cid": "c", "slug": "g"},
            "media_ids": ["@own"], "display_media_ids": ["@own"],
            "parse_media_ids": ["@own"], "quoted_media_ids": [],
        }, ensure_ascii=False), encoding="utf-8")
        run = enqueue_fixture(self.store, self.root, fixture, "own-image", "A", mode="production-message")
        stages = [task["stage"] for task in self.store.detail(run)["tasks"]]
        self.assertIn("vision", stages)

    def test_readonly_http_host_and_actual_queue(self):
        run = self.queue()
        http = server(self.store, Path(__file__).parent / "web/index.html", port=0)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{http.server_port}"
            with urlopen(base + "/api/runs") as r:
                response = json.load(r)
            self.assertTrue(response["ok"])
            self.assertEqual(response["data"][0]["id"], run)
            with urlopen(base) as r:
                self.assertIn("运价采集编排器", r.read().decode())
            with self.assertRaises(HTTPError):
                urlopen(Request(base + "/api/runs", headers={"Host": "external.invalid"}))
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_trace_studio_routes_and_review_projection(self):
        source = {"group": {"name": "测试群", "slug": "test", "cid": "cid"}}
        run = self.store.enqueue("trace", "group:test", source, ("pull", "fanout"), mode="production-batch")
        detail = trace_detail(self.store.detail(run))
        self.assertEqual(detail["title"], "测试群")
        self.assertEqual([task["title"] for task in detail["tasks"]], ["拉取", "拆消息"])
        self.assertEqual(trace_index(self.store.trace_roots())[0]["id"], run)
        http = server(self.store, Path(__file__).parent / "web/index.html", port=0)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{http.server_port}"
            with urlopen(base + "/studio") as response:
                studio = response.read().decode("utf-8")
            self.assertIn("Trace Studio", studio)
            self.assertIn("单群立即解析", studio)
            self.assertIn("runtimeGroupSlug", studio)
            self.assertIn("select.onchange", studio)
            self.assertIn("已加入并置顶解析队列", studio)
            self.assertIn("正在入队", studio)
            self.assertIn("runningNodePulse", studio)
            self.assertIn("aria-current", studio)
            self.assertIn("prefers-reduced-motion:reduce", studio)
            self.assertIn("filter-count", studio)
            self.assertIn("定位运行中", studio)
            self.assertIn("focusRunningNode", studio)
            self.assertIn("GRAPH_NODE_WIDTH", studio)
            self.assertIn("navigator-theme", studio)
            self.assertIn("Milogin's", studio)
            self.assertIn("Navigator.", studio)
            self.assertIn("STAGE_DESCRIPTION", studio)
            self.assertIn("hover-description", studio)
            self.assertIn("nodeDescription", studio)
            self.assertIn("node-color-contrast", studio)
            self.assertIn("dock-toggle-polish", studio)
            self.assertIn("展开节点检查器", studio)
            self.assertIn("run-head-layout", studio)
            self.assertIn("touch-action:none!important", studio)
            self.assertIn("box.addEventListener('dragstart'", studio)
            self.assertIn("box.addEventListener('pointercancel'", studio)
            with urlopen(base + "/api/trace/roots") as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["runs"][0]["id"], run)
            with urlopen(base + "/api/trace/run?id=" + run) as response:
                payload = json.load(response)
            self.assertEqual(payload["data"]["tasks"][0]["stage"], "pull")
            action = Request(base + "/api/trace/action", method="POST",
                             data=json.dumps({"action": "resume", "run_id": run}).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
            with urlopen(action) as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_trace_watermark_timeline_and_child_states(self):
        run = {
            "id": "group-run", "lane": "group:test", "mode": "production-batch", "created": 1000,
            "status": "WAITING_CHILDREN", "source": {"group": {"name": "测试群", "slug": "test"}},
            "tasks": [
                {"id": "pull", "stage": "pull", "status": "SUCCEEDED", "result": json.dumps({
                    "before": "2026-10-23 09:00:00", "after": "2026-10-23 10:00:00",
                    "messages": [{"createTime": "2026-10-23 09:20:00"}, {"createTime": "2026-10-23 09:50:00"}],
                })},
                {"id": "reconcile", "stage": "reconcile", "status": "QUEUED", "result": None},
                {"id": "watermark", "stage": "watermark", "status": "QUEUED", "result": None},
            ],
            "events": [{"stage": "pull", "at": 1009}, {"stage": "pull", "at": 1010}],
            "children": [
                {"id": "running", "lane": "message:test:m1", "created": 1001,
                 "message_id": "m1", "message_time": "2026-10-23 09:20:00", "message_text": "HPL 宁波 DAMIETTA 4050/40HQ",
                 "task_statuses": "SUCCEEDED,RUNNING", "running": 1, "waiting": 1, "current_stage": "classify"},
                {"id": "done", "lane": "message:test:m2", "created": 1002,
                 "message_id": "m2", "message_time": "2026-10-23 09:50:00", "message_text": "完成消息",
                 "task_statuses": "SUCCEEDED,SKIPPED", "submitted": 1, "readback_succeeded": 1,
                 "classify_decision": "quote"},
                {"id": "partial", "lane": "message:test:m3", "created": 1003,
                 "message_id": "m3", "message_time": "2026-10-23 09:55:00", "message_text": "部分消息",
                 "task_statuses": "SUCCEEDED,PARTIAL_REVIEW", "submitted": 1, "partial_review": 1,
                 "classify_decision": "quote"},
            ],
        }
        detail = trace_detail(run)
        self.assertTrue(detail["working"])
        self.assertEqual(detail["pull_time"], 1010)
        self.assertEqual(detail["pull_status"], "SUCCEEDED")
        lightweight = trace_index([{**run, "events": [], "pull_finished_at": 1010}])[0]
        self.assertEqual(lightweight["pull_time"], 1010, "tree summary keeps pull time without loading full events")
        self.assertEqual(detail["work_label"], "消息分类中")
        self.assertEqual(detail["child_counts"], {"total": 3, "completed": 1, "ingested": 1, "working": 1})
        self.assertEqual([child["status"] for child in detail["child_nodes"]], ["WORKING", "COMPLETED", "INGESTED"])
        self.assertEqual(detail["child_nodes"][1]["title"], "完成消息")
        self.assertEqual({child["pull_time"] for child in detail["child_nodes"]}, {1010})
        self.assertEqual(detail["watermark_timeline"]["before"], "2026-10-23 09:00:00")
        self.assertEqual(detail["watermark_timeline"]["target"], "2026-10-23 10:00:00")
        self.assertEqual(detail["watermark_timeline"]["message_from"], "2026-10-23 09:20:00")
        self.assertEqual(detail["watermark_timeline"]["message_to"], "2026-10-23 09:50:00")
        self.assertEqual(detail["watermark_timeline"]["label"], "等待群对账")

    def test_review_acknowledgement_is_audited_and_stales_when_task_state_changes(self):
        source = {"group": {"name": "处置测试群", "slug": "review-test"}}
        parent = self.store.enqueue("review-parent", "group:review-test", source,
                                    ("pull", "fanout", "reconcile", "watermark"), mode="production-batch")
        child = self.store.enqueue("review-child", "message:review-test:m1",
                                   {**source, "message_id": "m1", "message_time": "2026-10-23 10:00:00"},
                                   ("prepare", "classify"), mode="production-message")
        self.store.depend(parent, child)
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET status='FAILED',error='business mismatch' WHERE run_id=?", (child,))

        before = trace_detail(self.store.detail(parent))
        self.assertTrue(before["needs_review"])
        acknowledged = self.store.acknowledge_review(parent, "已核对当前失败原因", "tester")
        self.assertTrue(acknowledged["reviewed"])
        after = trace_detail(self.store.detail(parent))
        self.assertTrue(after["reviewed"])
        self.assertFalse(after["needs_review"])
        self.assertEqual(after["display_status"], "REVIEWED")

        with self.store.connect() as db:
            db.execute("UPDATE tasks SET error='new business mismatch' WHERE run_id=?", (child,))
        changed = trace_detail(self.store.detail(parent))
        self.assertFalse(changed["reviewed"])
        self.assertTrue(changed["needs_review"])

        http = server(self.store, Path(__file__).parent / "web/index.html", port=0)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{http.server_port}"
            request = Request(
                base + "/api/trace/action", method="POST",
                data=json.dumps({
                    "action": "acknowledge_review", "run_id": parent,
                    "note": "HTTP 处置审计", "reviewer": "test-reviewer",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])
            with urlopen(base + "/api/trace/run?id=" + parent) as response:
                payload = json.load(response)
            self.assertTrue(payload["data"]["reviewed"])
            self.assertFalse(payload["data"]["needs_review"])
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

        queued_message = trace_detail({
            "id": "message-queued", "lane": "message:test:m4", "mode": "production-message",
            "status": "QUEUED", "created": 1004, "source": {"message_id": "m4"},
            "tasks": [{"id": "prepare", "stage": "prepare", "status": "QUEUED", "result": None}],
            "events": [], "children": [],
        })
        self.assertEqual(queued_message["display_status"], "QUEUED")
        self.assertEqual(queued_message["display_label"], "等待执行")

        completed_message = trace_detail({
            "id": "message-done", "lane": "message:test:m5", "mode": "production-message",
            "status": "DONE", "created": 1005, "source": {"message_id": "m5"},
            "tasks": [
                {"id": "prepare", "stage": "prepare", "status": "SUCCEEDED", "result": "{}"},
                {"id": "submit", "stage": "submit", "status": "SUCCEEDED", "result": "{}"},
                {"id": "readback", "stage": "readback", "status": "SUCCEEDED",
                 "result": json.dumps({"ok": True, "expected": 2, "covered_ids": ["a", "b"]})},
            ],
            "events": [], "children": [],
        })
        self.assertEqual(completed_message["display_status"], "COMPLETED")
        self.assertEqual(completed_message["display_label"], "已完成")

    def test_runtime_control_and_dws_login_routes(self):
        class FakeAuth:
            def __init__(self):
                self.checks = 0
                self.logins = 0
            def begin_check(self):
                self.checks += 1
            def login(self):
                self.logins += 1
                return {"started": True, "state": "login_pending"}
        class FakeControl:
            def __init__(self):
                self.dws_auth = FakeAuth()
                self.saved_schedule = None
                self.saved_worker = None
                self.triggered_group = None
            def save_schedule(self, payload):
                self.saved_schedule = payload
                return payload
            def save_worker(self, payload):
                self.saved_worker = payload
                return payload
            def trigger_group(self, slug, message_id=None):
                self.triggered_group = slug
                self.triggered_message = message_id
                return {"group": {"slug": slug}, "trigger_key": "manual:test", "run_ids": ["run"]}
            def snapshot(self):
                return {"service": {"state": "stopped"},
                        "scheduler": {"next_slots": {}},
                        "triggers": [], "workers": {"configured": []},
                        "dws": {"state": "not_authenticated"}}
        control = FakeControl()
        http = server(self.store, Path(__file__).parent / "web/index.html", port=0,
                      runtime_control=control)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{http.server_port}"
            with urlopen(base + "/studio") as response:
                page = response.read().decode("utf-8")
            self.assertIn('data-tab="runtime"', page)
            self.assertIn("授权 DWS", page)
            with urlopen(base + "/api/runtime") as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["dws"]["state"], "not_authenticated")
            self.assertEqual(control.dws_auth.checks, 1)
            login = Request(base + "/api/dws/login", method="POST", data=b"{}",
                            headers={"Content-Type": "application/json"})
            with urlopen(login) as response:
                payload = json.load(response)
            self.assertTrue(payload["data"]["started"])
            self.assertEqual(control.dws_auth.logins, 1)
            schedule_request = Request(
                base + "/api/runtime/schedule", method="POST", data=b'{"enabled":false}',
                headers={"Content-Type": "application/json"})
            with urlopen(schedule_request) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(control.saved_schedule, {"enabled": False})
            worker_request = Request(
                base + "/api/runtime/worker", method="POST", data=b'{"concurrency":3}',
                headers={"Content-Type": "application/json"})
            with urlopen(worker_request) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(control.saved_worker, {"concurrency": 3})
            trigger_request = Request(
                base + "/api/runtime/trigger", method="POST",
                data=b'{"group_slug":"test_group","message_id":"msg-old"}',
                headers={"Content-Type": "application/json"})
            with urlopen(trigger_request) as response:
                payload = json.load(response)
            self.assertEqual(payload["data"]["trigger_key"], "manual:test")
            self.assertEqual(control.triggered_group, "test_group")
            self.assertEqual(control.triggered_message, "msg-old")
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_trace_root_prefers_actionable_failure_over_newer_queue(self):
        source = {"group": {"name": "优先级群", "slug": "priority", "cid": "cid"}}
        failed = self.store.enqueue("failed", "group:priority", source, ("pull",), mode="production-batch")
        task = self.store.claim("worker")
        self.store.finish(task, error="source blocked", retryable=False)
        queued = self.store.enqueue("queued", "group:priority", source, ("pull",), mode="production-batch")
        roots = trace_index(self.store.trace_roots())
        self.assertEqual(roots[0]["id"], failed)
        self.assertEqual(roots[0]["history_count"], 2)
        self.assertNotEqual(roots[0]["id"], queued)

    def test_trace_root_marks_partial_child_for_review(self):
        run = {
            "id": "parent", "lane": "group:partial", "mode": "production-batch", "status": "DONE",
            "source": {"group": {"name": "部分解析群", "slug": "partial"}}, "tasks": [], "events": [],
            "children": [{"id": "child", "lane": "message:partial:msg", "failed": 0,
                          "needs_review": 0, "partial_review": 1, "waiting": 0}],
        }
        summary = trace_index([run])[0]
        self.assertTrue(summary["review_needed"])
        self.assertEqual(summary["display_status"], "PARTIAL_REVIEW")
        queued = {
            "id": "newer-queued", "lane": "group:partial", "mode": "production-batch", "status": "QUEUED",
            "created": 2000, "source": {"group": {"name": "部分解析群", "slug": "partial"}},
            "tasks": [{"id": "pull", "stage": "pull", "status": "QUEUED", "result": None}],
            "events": [], "children": [],
        }
        indexed = trace_index([queued, run])
        self.assertEqual(len(indexed), 1, 'one production group must stay one top-level tree row')
        self.assertEqual(indexed[0]["id"], "newer-queued")
        self.assertEqual(indexed[0]["selection_role"], "current")
        self.assertEqual(indexed[0]["review_root"]["id"], "parent")
        self.assertEqual(indexed[0]["review_root"]["display_status"], "PARTIAL_REVIEW")

    def test_task_summary_excludes_cancelled_history_from_live_queue(self):
        self.store.enqueue('live', 'group:live', SOURCE, ('pull',), mode='production-batch')
        history = self.store.enqueue('history', 'history:test', SOURCE, ('pull',), mode='history-batch')
        history_child = self.store.enqueue('history-child', 'message:test:h1', SOURCE, ('prepare',), mode='production-message')
        with self.store.connect() as db:
            db.execute('INSERT INTO dependencies(parent,child) VALUES(?,?)', (history, history_child))
        summary = self.store.task_summary()
        self.assertEqual(summary['scope'], 'live')
        self.assertEqual(summary['by_status'], {'QUEUED': 1})
        self.assertEqual(summary['historical_by_status'], {'QUEUED': 2})


if __name__ == "__main__":
    unittest.main()
