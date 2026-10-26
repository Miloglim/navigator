import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from agent_watch.transport.store import Store
from agent_watch.transport.desktop_observer import DesktopObserver
from agent_watch.services.session_events import project_session
class DesktopTests(unittest.TestCase):
    def test_attach_existing_task_timestamp_incremental_tail_and_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); checkout=root/"checkout"; checkout.mkdir()
            sessions=root/"sessions"; folder=sessions/datetime.now().strftime("%Y/%m/%d");folder.mkdir(parents=True)
            file=folder/"task.jsonl"
            rows=[{"type":"session_meta","timestamp":"2026-09-29T05:02:24Z","payload":{"id":"test-thread","cwd":str(checkout)}},{"type":"event_msg","timestamp":"2026-09-29T05:02:25Z","payload":{"type":"task_started"}},{"type":"response_item","timestamp":"2026-09-29T05:02:26Z","payload":{"type":"message","role":"user","content":[{"text":"Automation ID: b-codex\nprivate prompt"}]}},{"type":"response_item","timestamp":"2026-09-29T05:04:00Z","payload":{"type":"function_call","name":"functions.exec_command","arguments":json.dumps({"cmd":"python pipeline.py --token=secret"})}}]
            file.write_text("".join(json.dumps(r)+"\n" for r in rows),encoding="utf-8")
            store=Store(root/"monitor.sqlite3"); observer=DesktopObserver(store,sessions,checkout); observer.scan()
            row=store.runs()[0]; self.assertEqual(row["run_id"],"desktop-test-thread");self.assertEqual(row["shard"],"B")
            self.assertEqual(row["source"],"desktop");self.assertFalse(row["control_ready"])
            self.assertEqual(row["seen_at"],datetime.fromisoformat("2026-09-29T05:04:00+00:00").timestamp())
            self.assertNotIn("secret",json.dumps(store.events(row["run_id"])))
            before=len(store.events(row["run_id"]));observer.scan();self.assertEqual(len(store.events(row["run_id"])),before)
            DesktopObserver(store,sessions,checkout).scan();self.assertEqual(len(store.events(row["run_id"])),before)
            end=json.dumps({"type":"event_msg","timestamp":"2026-09-29T05:05:00Z","payload":{"type":"task_complete"}})
            with file.open("a",encoding="utf-8") as f:f.write(end)
            observer.scan();self.assertEqual(store.runs()[0]["status"],"running")
            with file.open("a",encoding="utf-8") as f:f.write("\n")
            observer.scan();self.assertEqual(store.runs()[0]["status"],"completed")
            store.db.close()
    def test_other_project_and_other_automation_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/"monitor.sqlite3");observer=DesktopObserver(store,root,root/"checkout")
            for index,(cwd,automation) in enumerate([(root/"other","b-codex"),(root/"checkout","unrelated"),(root/"checkout","b-codex")]):
                file=root/(str(index)+".jsonl")
                rows=[{"type":"session_meta","payload":{"id":str(index),"cwd":str(cwd),"source":{"subagent":{"other":"guardian"}} if index==2 else "vscode"}},{"type":"response_item","payload":{"type":"message","role":"user","content":[{"text":"Automation ID: "+automation}]} }]
                file.write_text("".join(json.dumps(r)+"\n" for r in rows),encoding="utf-8");observer.read(file)
            self.assertEqual(store.runs(),[]);store.db.close()
    def test_reasoning_and_tool_output_not_projected(self):
        self.assertIsNone(project_session({"type":"response_item","payload":{"type":"reasoning","text":"secret"}}))
        self.assertIsNone(project_session({"type":"response_item","payload":{"type":"function_call_output","output":"secret"}}))
if __name__=="__main__":unittest.main()
