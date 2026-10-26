"""Local progress service and Agent reporting entrypoint."""
import argparse
import json
import logging
import sys
from pathlib import Path
from agent_watch.transport.client import RUNTIME, ensure_server, report, checkpoint
from agent_watch.transport.server import serve
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("wake")
    progress = sub.add_parser("report")
    for name in ("stage", "activity", "group", "message-id", "file"):
        progress.add_argument("--" + name, default="")
    args = parser.parse_args()
    if args.cmd == "serve":
        serve(RUNTIME, Path(__file__).parent / "web")
    elif args.cmd == "wake":
        print(json.dumps({"ok":ensure_server()}))
    else:
        checkpoint()
        print(json.dumps({"ok":report(**{k:v for k,v in vars(args).items() if k != "cmd" and v})}))
if __name__ == "__main__":
    main()
