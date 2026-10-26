#!/usr/bin/env python3
"""Submit Navigator external-Agent intake JSON without embedding credentials."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def read_payload(path: str) -> dict:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("payload must be a JSON object")
    return value


def submit(url: str, token: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"ok": False, "error": {"code": "http_error", "message": raw[:500]}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit parsed Navigator Agent intake JSON")
    parser.add_argument("--file", default="-", help="JSON file path, or - for stdin")
    args = parser.parse_args()
    url = os.environ.get("NAVIGATOR_AGENT_API_URL", "").strip()
    token = os.environ.get("NAVIGATOR_AGENT_API_TOKEN", "").strip()
    if not url:
        raise ValueError("NAVIGATOR_AGENT_API_URL is not set")
    if not token:
        raise ValueError("NAVIGATOR_AGENT_API_TOKEN is not set")
    status, result = submit(url, token, read_payload(args.file))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == 200 and result.get("ok") is True else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - expose a contract-shaped local failure
        print(json.dumps({"ok": False, "error": {"code": "local_error", "message": str(error)}}, ensure_ascii=False))
        raise SystemExit(2)
