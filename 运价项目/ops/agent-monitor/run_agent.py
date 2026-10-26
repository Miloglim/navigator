"""Wrap scheduled Codex execution with automatic wake and real tool telemetry."""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from agent_watch.core.contracts import HEARTBEAT_SECONDS
from agent_watch.services.progress import project_cli, clean
from agent_watch.transport.client import ensure_server, report
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
def main():
    parser = argparse.ArgumentParser()
    for name in ("codex", "prompt", "last-message", "project", "dws-state", "shard"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + args.shard + "-" + uuid.uuid4().hex[:8]
    os.environ["FRT_MONITOR_RUN_ID"] = run_id
    try:
        ensure_server()
    except Exception as e:
        logging.warning("[MONITOR] wake error (%s); collection continues", type(e).__name__)
    report(source="runner", shard=args.shard, status="running", stage="START", activity="启动采集 Agent")
    stop = threading.Event()
    def pulse():
        while not stop.wait(HEARTBEAT_SECONDS):
            report(source="runner", heartbeat=True)
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    child = None
    exit_code = 1
    try:
        prompt = Path(args.prompt).read_text(encoding="utf-8-sig")
        command = [args.codex, "--sandbox", "danger-full-access", "--add-dir", args.dws_state, "--ask-for-approval", "never", "-C", args.project, "-m", args.model, "exec", "--json", "-o", args.last_message, "-"]
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        child.stdin.write(prompt)
        child.stdin.close()
        for line in child.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                # Startup diagnostics are retained in existing runner log, sanitized.
                print(clean(line.rstrip()), flush=True)
                continue
            if not isinstance(event, dict):
                continue
            projected = project_cli(event)
            if projected:
                report(source="runner", **projected)
                print(json.dumps({"event":event.get("type"), **projected}, ensure_ascii=False), flush=True)
        exit_code = child.wait()
        outcome = "unknown"
        if Path(args.last_message).is_file():
            final = Path(args.last_message).read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?m)^\s*(?:#+\s*)?ABORT\b", final):
                outcome = "ABORT"
            elif re.search(r"(?m)^\s*(?:#+\s*)?DONE\b", final):
                outcome = "DONE"
        report(source="runner", status="completed" if exit_code == 0 and outcome != "ABORT" else "failed", stage="END", activity="执行器退出；业务结果以流水线阶段及回执为准" if outcome == "unknown" else "本轮 " + outcome)
        return exit_code
    except BaseException as e:
        if child and child.poll() is None:
            child.terminate()
        report(source="runner", status="interrupted" if isinstance(e, KeyboardInterrupt) else "failed", stage="END", activity="执行器异常：" + type(e).__name__)
        logging.exception("[RUNNER] execution failed")
        return 1
    finally:
        if child and child.stdout:
            child.stdout.close()
        stop.set()
        thread.join(timeout=1)
if __name__ == "__main__":
    raise SystemExit(main())
