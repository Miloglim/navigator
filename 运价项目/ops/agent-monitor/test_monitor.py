import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from agent_watch.services.progress import project_cli, validate, display
from agent_watch.transport.store import Store
from agent_watch.transport import client
from agent_watch.core.contracts import REPORT_PATH, CONTROL_PATH
ROOT = Path(__file__).parent
class CoreTests(unittest.TestCase):
    def test_actual_tools_do_not_leak_command_or_output(self):
        event = {"type":"item.completed","item":{"type":"command_execution","command":"python pipeline.py --token=secret-value","aggregated_output":"private body","exit_code":0}}
        output = project_cli(event)
        self.assertIn("完成", output["activity"])
        self.assertNotIn("secret-value", json.dumps(output))
        self.assertNotIn("private body", json.dumps(output))
        self.assertIsNone(project_cli({"type":"item.completed","item":{"type":"reasoning","text":"private reasoning"}}))
    def test_validation_and_redaction(self):
        self.assertNotIn("xyz", validate({"run_id":"test","source":"agent","activity":"token=xyz"})["activity"])
        for payload in [{"run_id":"../x","source":"agent"},{"run_id":"test","source":"fake"},{"run_id":"test","source":"agent","completed":-1}]:
            with self.assertRaises(ValueError): validate(payload)
    def test_health_distinguishes_alive_from_progress(self):
        state={"seen_at":190,"activity_at":0,"status":"running"}
        self.assertEqual(display(state,200)["health"],"quiet")
        self.assertEqual(display(state,300)["health"],"disconnected")
        state["status"]="completed"
        self.assertEqual(display(state,300)["health"],"completed")
class StoreTests(unittest.TestCase):
    def test_isolation_concurrency_heartbeat_and_late_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"monitor.sqlite3")
            with concurrent.futures.ThreadPoolExecutor() as pool:
                list(pool.map(lambda i:store.report({"run_id":"A" if i%2 else "B","source":"runner","activity":str(i)}),range(60)))
            self.assertEqual(len(store.runs()),2)
            a=next(r for r in store.runs() if r["run_id"]=="A")
            store.control("A","pause")
            store.report({"run_id":"A","source":"runner","heartbeat":True})
            self.assertEqual(store.control("A"),"pause")
            self.assertEqual(next(r for r in store.runs() if r["run_id"]=="A")["activity_at"],a["activity_at"])
            store.report({"run_id":"A","source":"runner","status":"completed"})
            store.report({"run_id":"A","source":"pipeline","status":"running"})
            self.assertEqual(next(r for r in store.runs() if r["run_id"]=="A")["status"],"completed")
            self.assertEqual(len(store.events("A")),31)
            store.db.close()
class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.runtime=Path(cls.tmp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0)); cls.port=sock.getsockname()[1]
        cls.url="http://127.0.0.1:"+str(cls.port)
        script="from pathlib import Path; from agent_watch.transport.server import serve; serve(Path(%r),Path(%r),%d,observe_desktop=False)"%(str(cls.runtime),str(ROOT/"web"),cls.port)
        cls.process=subprocess.Popen([sys.executable,"-c",script],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with urlopen(cls.url+"/api/health",timeout=.2) as r: json.load(r)
                break
            except OSError: time.sleep(.05)
        cls.token=(cls.runtime/"monitor-token.txt").read_text().strip()
    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=5);cls.tmp.cleanup()
    def request(self,path,payload=None,auth=True,headers=None):
        headers=dict(headers or {})
        if auth:headers["Authorization"]="Bearer "+self.token
        if payload is not None:headers["Content-Type"]="application/json"
        with urlopen(Request(self.url+path,data=None if payload is None else json.dumps(payload).encode(),headers=headers),timeout=3) as r:
            return json.load(r)
    def test_security_and_browser_control(self):
        with self.assertRaises(HTTPError) as err:self.request(REPORT_PATH,{"run_id":"unsafe","source":"runner"},auth=False)
        self.assertEqual(err.exception.code,403)
        self.request(REPORT_PATH,{"run_id":"web","source":"runner"})
        with urlopen(self.url+"/") as r:
            cookie=r.headers["Set-Cookie"].split(";")[0]
            page=r.read().decode()
        self.assertNotIn(self.token,page)
        with self.assertRaises(HTTPError):self.request(CONTROL_PATH,{"run_id":"web","action":"pause"},auth=False,headers={"Cookie":cookie,"Origin":"https://evil.invalid"})
        self.assertEqual(self.request(CONTROL_PATH,{"run_id":"web","action":"pause"},auth=False,headers={"Cookie":cookie,"Origin":self.url})["data"]["action"],"pause")

    def test_orchestrator_view_same_origin_and_readonly_api(self):
        with urlopen(self.url + "/") as r:
            page = r.read().decode()
        self.assertIn('id="flow-view"', page)
        self.assertIn('src="/orchestrator"', page)
        with urlopen(self.url + "/orchestrator") as r:
            embedded = r.read().decode()
            self.assertIn("frame-ancestors 'self'", r.headers["Content-Security-Policy"])
        self.assertIn("fetch('/api/orchestrator/runs')", embedded)
        self.assertNotIn("fetch('/api/runs')", embedded)
        self.assertIsInstance(self.request("/api/orchestrator/runs")["data"], list)
        with self.assertRaises(HTTPError):
            self.request("/api/orchestrator/runs", {"action":"retry"})
    def test_checkpoint_pause_and_resume(self):
        self.request(REPORT_PATH,{"run_id":"pause","source":"runner"})
        self.request(CONTROL_PATH,{"run_id":"pause","action":"pause"})
        with patch.object(client,"URL",self.url),patch.object(client,"RUNTIME",self.runtime),patch.dict(os.environ,{"FRT_MONITOR_RUN_ID":"pause"}),patch.object(client,"POLL_SECONDS",.02):
            done=threading.Event()
            thread=threading.Thread(target=lambda:(client.checkpoint(),done.set()))
            thread.start()
            for _ in range(50):
                rows=self.request("/api/runs")["data"]
                if any(r["run_id"]=="pause" and r["status"]=="paused" for r in rows):break
                time.sleep(.02)
            self.assertFalse(done.is_set())
            self.assertTrue(any(r["run_id"]=="pause" and r["status"]=="paused" for r in rows))
            self.request(CONTROL_PATH,{"run_id":"pause","action":"resume"})
            thread.join(timeout=3)
            self.assertTrue(done.is_set())
    def test_outage_does_not_block_business(self):
        with patch.object(client,"URL","http://127.0.0.1:1"),patch.dict(os.environ,{"FRT_MONITOR_RUN_ID":"no-service"}):
            self.assertFalse(client.report(source="pipeline",stage="BUILD"))
            client.checkpoint()
    def test_pipeline_save_reports_real_checkpoint_in_isolated_work(self):
        scripts=ROOT.parents[1]/"Skill"/"freight-rate-heartbeat"/"scripts"
        script="import pipeline; pipeline.save_state('fixture',{'stage':'SUBMIT','records_submitted':7,'records_check_failed':2})"
        env=dict(os.environ, FRT_MONITOR_RUN_ID="pipeline-fixture", FRT_MONITOR_URL=self.url, FRT_RUNTIME_DIR=str(self.runtime/"pipeline"))
        # Client credential path is overridden in the child without touching production credentials.
        script="from pathlib import Path; import sys; sys.path.insert(0,%r); from agent_watch.transport import client; client.RUNTIME=Path(%r); "%(str(ROOT),str(self.runtime))+script
        result=subprocess.run([sys.executable,"-c",script],cwd=scripts,env=env,capture_output=True,text=True,encoding="utf-8")
        self.assertEqual(result.returncode,0,result.stderr)
        row=next(r for r in self.request("/api/runs")["data"] if r["run_id"]=="pipeline-fixture")
        self.assertEqual(row["workflow_stage"],"SUBMIT")
        self.assertEqual(row["completed"],7)
        self.assertEqual(row["check_failed"],2)

    def test_runner_preserves_failure_exit_code(self):
        import run_agent
        prompt=self.runtime/"failed-prompt.md"; prompt.write_text("test",encoding="utf-8")
        final=self.runtime/"never-written.md"
        real_popen=subprocess.Popen
        def launch(command,**kwargs):
            return real_popen([sys.executable,"-c","import sys; sys.stdin.read(); raise SystemExit(7)"],**kwargs)
        args=["run_agent.py","--codex","fake","--prompt",str(prompt),"--last-message",str(final),"--project",str(ROOT),"--dws-state",str(self.runtime),"--shard","B","--model","original-model"]
        with patch.object(client,"URL",self.url),patch.object(client,"RUNTIME",self.runtime),patch.object(run_agent,"ensure_server",return_value=True),patch.object(run_agent.subprocess,"Popen",side_effect=launch),patch.object(sys,"argv",args),patch.dict(os.environ,{},clear=False):
            self.assertEqual(run_agent.main(),7)
        row=next(r for r in self.request("/api/runs")["data"] if r.get("shard")=="B")
        self.assertEqual(row["status"],"failed")

    def test_runner_uses_events_inherits_context_and_preserves_exit(self):
        import run_agent
        prompt=self.runtime/"prompt.md";prompt.write_text("test",encoding="utf-8")
        final=self.runtime/"final.md"
        fake=self.runtime/"fake_cli.py"
        fake.write_text('import json,os,sys;from pathlib import Path; assert os.environ.get("FRT_MONITOR_RUN_ID"); sys.stdin.read(); print(json.dumps({"type":"item.started","item":{"type":"command_execution","command":"python pipeline.py next"}}),flush=True);Path(sys.argv[1]).write_text("DONE: test",encoding="utf-8");print(json.dumps({"type":"item.completed","item":{"type":"command_execution","command":"python pipeline.py next","exit_code":0}}),flush=True)',encoding="utf-8")
        real_popen=subprocess.Popen
        seen=[]
        def launch(command,**kwargs):
            seen.extend(command)
            return real_popen([sys.executable,str(fake),str(final)],**kwargs)
        args=["run_agent.py","--codex","fake","--prompt",str(prompt),"--last-message",str(final),"--project",str(ROOT),"--dws-state",str(self.runtime),"--shard","A","--model","original-model"]
        with patch.object(client,"URL",self.url),patch.object(client,"RUNTIME",self.runtime),patch.object(run_agent,"ensure_server",return_value=True),patch.object(run_agent.subprocess,"Popen",side_effect=launch),patch.object(sys,"argv",args),patch.dict(os.environ,{},clear=False):
            self.assertEqual(run_agent.main(),0)
        self.assertIn("--json",seen)
        self.assertIn("original-model",seen)
        rows=self.request("/api/runs")["data"]
        row=next(r for r in rows if r.get("shard")=="A")
        self.assertEqual(row["status"],"completed")
        self.assertEqual(len(self.request("/api/events?run_id="+row["run_id"])["data"]),4)
if __name__ == "__main__":unittest.main()
