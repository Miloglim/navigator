# -*- coding: utf-8 -*-
"""Contract and HTTP tests for the external-Agent intake endpoint."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from core.agent_intake_contracts import AGENT_INTAKE_ROUTE, normalize_agent_intake
from transport.agent_intake_routes import build_agent_intake_route

HERE = Path(__file__).resolve().parent
TOKEN = "agent-intake-test-token-0123456789abcdef"


def payload() -> dict:
    return {
        "schema_version": "1.0",
        "kind": "rates",
        "agent": {"name": "contract-test", "version": "1.0.0"},
        "records": [{
            "来源群": "测试群", "消息ID": "message-1", "消息原文": "上海到 Santos 40HQ 2850 USD",
            "起运港": "Shanghai", "目的港": "Santos", "船司": "MAERSK",
            "柜型": "40HQ/HC", "海运费USD": 2850,
            "有效期起": "2026-10-21", "有效期止": "2026-10-31",
        }],
    }


class AgentIntakeContractTests(unittest.TestCase):
    def test_normalizes_to_pending_with_forced_agent_origin(self):
        request, agent = normalize_agent_intake(payload())
        self.assertEqual(request["origin"], "agent")
        self.assertTrue(request["preserve_other_sources"])
        self.assertEqual(agent["name"], "contract-test")

    def test_rejects_missing_trace_fields_and_unknown_top_level_fields(self):
        missing = payload()
        missing["records"][0].pop("消息ID")
        with self.assertRaisesRegex(ValueError, "消息ID"):
            normalize_agent_intake(missing)
        extra = payload()
        extra["origin"] = "manual"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalize_agent_intake(extra)
        no_agent = payload()
        no_agent.pop("agent")
        with self.assertRaisesRegex(ValueError, "agent object"):
            normalize_agent_intake(no_agent)

    def test_route_wraps_pending_result_in_data_envelope(self):
        captured = {}

        def submit(request):
            captured.update(request)
            return {"ok": True, "staged": 1, "ids": [7], "entry_ids": ["entry-7"],
                    "coverage": {"ok": True, "missing": []}}

        result = build_agent_intake_route(submit)[AGENT_INTAKE_ROUTE](payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["ids"], [7])
        self.assertEqual(captured["origin"], "agent")


class AgentIntakeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.temp.name) / "agent-intake.db")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        env = dict(os.environ, NAVIGATOR_AGENT_API_TOKEN=TOKEN)
        cls.proc = subprocess.Popen(
            [sys.executable, str(HERE / "manual_server.py"), "--host", "127.0.0.1",
             "--port", str(cls.port), "--db", cls.db],
            cwd=str(HERE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", cls.port)) == 0:
                    return
            if cls.proc.poll() is not None:
                raise RuntimeError("manual_server exited before Agent intake test")
            time.sleep(0.2)
        raise RuntimeError("manual_server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)
        cls.temp.cleanup()

    @classmethod
    def call(cls, body, token=TOKEN):
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{AGENT_INTAKE_ROUTE}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_valid_bearer_token_stages_record(self):
        status, result = self.call(payload())
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["staged"], 1)

    def test_external_skill_helper_uses_documented_url_and_token(self):
        helper = HERE.parent / "Skill" / "navigator-agent-intake" / "scripts" / "submit_intake.py"
        env = dict(os.environ,
                   NAVIGATOR_AGENT_API_URL=f"http://127.0.0.1:{self.port}{AGENT_INTAKE_ROUTE}",
                   NAVIGATOR_AGENT_API_TOKEN=TOKEN)
        completed = subprocess.run(
            [sys.executable, str(helper), "--file", "-"],
            input=json.dumps(payload(), ensure_ascii=False), text=True,
            capture_output=True, env=env, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertIn("staged", result["data"])

    def test_invalid_token_and_schema_use_structured_errors(self):
        status, result = self.call(payload(), "wrong-token-value-0123456789abcdef")
        self.assertEqual(status, 401)
        self.assertEqual(result["error"]["code"], "unauthorized")
        invalid = payload()
        invalid["schema_version"] = "2.0"
        status, result = self.call(invalid)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
