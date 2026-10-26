"""Best-effort telemetry; business operation never depends on monitor uptime."""
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from agent_watch.core.contracts import HOST, PORT, REPORT_PATH, CONTROL_PATH, HEALTH_PATH, REQUEST_TIMEOUT_SECONDS, POLL_SECONDS, STARTUP_ATTEMPTS, STARTUP_POLL_SECONDS
ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
URL = os.environ.get("FRT_MONITOR_URL", "http://%s:%s" % (HOST, PORT))
def current_run_id():
    explicit=os.environ.get("FRT_MONITOR_RUN_ID")
    thread=os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    return explicit or ("desktop-"+thread if thread else None)

def request(route, payload=None):
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + (RUNTIME / "monitor-token.txt").read_text(encoding="utf-8").strip()
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with urlopen(Request(URL + route, data=data, headers=headers), timeout=REQUEST_TIMEOUT_SECONDS) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise ValueError("monitor rejected request")
    return result.get("data")
def report(**fields):
    run_id = fields.pop("run_id", None) or current_run_id()
    if not run_id:
        return False
    try:
        request(REPORT_PATH, {"run_id":run_id,"source":"agent",**fields})
        return True
    except Exception as e:
        logging.warning("[MONITOR] progress delivery failed (%s); collection continues", type(e).__name__)
        return False

def ensure_server():
    try:
        health = request(HEALTH_PATH)
        return health.get("service") == "freight-agent-monitor"
    except Exception:
        # Service absent is an expected auto-wake condition; startup is logged below.
        pass
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "server.log").open("a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, str(ROOT / "monitor.py"), "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0), close_fds=True)
    for _ in range(STARTUP_ATTEMPTS):
        time.sleep(STARTUP_POLL_SECONDS)
        try:
            return request(HEALTH_PATH).get("service") == "freight-agent-monitor"
        except Exception:
            pass # Bounded startup poll; caller emits failure if all polls fail.
    logging.warning("[MONITOR] automatic wake failed; collection continues")
    return False

def checkpoint():
    run_id = current_run_id()
    if not run_id:
        return
    paused = False
    while True:
        try:
            action = request(CONTROL_PATH + "?run_id=" + run_id)["action"]
        except Exception as e:
            logging.warning("[MONITOR] control unavailable (%s); collection continues", type(e).__name__)
            return
        if action != "pause":
            if paused:
                report(source="pipeline", status="running", activity="用户已继续，恢复安全步骤")
            return
        if not paused:
            report(source="pipeline", status="paused", activity="已在安全步骤边界暂停，等待用户继续")
        else:
            report(source="pipeline", heartbeat=True)
        paused = True
        time.sleep(POLL_SECONDS)
