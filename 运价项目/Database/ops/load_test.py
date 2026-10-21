# -*- coding: utf-8 -*-
"""Read-only capacity test for the sales-facing freight-query APIs.

It never sends POST requests. Credentials may be read from a local file, never
printed or supplied inline on the command line. High concurrency needs
an explicit switch so a capacity test is not started accidentally in business
hours.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

DEFAULT_BASE_URL = "http://127.0.0.1:8795"
DEFAULT_CLIENTS = 30
DEFAULT_REQUESTS = 300
HIGH_LOAD_CLIENTS = 60
MAX_CLIENTS = 300
READ_PATHS = ("/api/bootstrap", "/api/stats", "/api/rates?limit=50", "/api/space?limit=50")


def percentile(values: Iterable[float], percent: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, round((percent / 100) * (len(ordered) - 1))))
    return round(ordered[rank], 2)


def summarize(results: Iterable[Tuple[str, int, float, str]]) -> Dict[str, Any]:
    grouped: Dict[str, List[Tuple[int, float, str]]] = defaultdict(list)
    for path, status, elapsed_ms, error in results:
        grouped[path].append((status, elapsed_ms, error))
    summary: Dict[str, Any] = {}
    for path, samples in grouped.items():
        elapsed = [sample[1] for sample in samples]
        failures = [sample for sample in samples if sample[0] != 200]
        summary[path] = {
            "requests": len(samples),
            "success": len(samples) - len(failures),
            "failed": len(failures),
            "p50_ms": percentile(elapsed, 50),
            "p95_ms": percentile(elapsed, 95),
            "p99_ms": percentile(elapsed, 99),
            "max_ms": round(max(elapsed), 2) if elapsed else 0.0,
            "errors": sorted({sample[2] for sample in failures if sample[2]})[:5],
        }
    return summary


def _request(base_url: str, path: str, timeout_seconds: float, credential: str = "") -> Tuple[str, int, float, str]:
    started = time.perf_counter()
    try:
        request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
        if credential:
            request.add_header("x-push-token", credential)
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response.read()
            status, error = response.status, ""
    except urllib.error.HTTPError as exc:
        status, error = exc.code, "HTTP %d" % exc.code
    except Exception as exc:  # noqa: BLE001 - load reports transport failures instead of stopping early
        status, error = 0, type(exc).__name__
    return path, status, (time.perf_counter() - started) * 1000, error


def run(base_url: str, clients: int, requests: int, timeout_seconds: float, credential: str = "") -> Dict[str, Any]:
    started = time.perf_counter()
    outcomes: List[Tuple[str, int, float, str]] = []
    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(_request, base_url, READ_PATHS[index % len(READ_PATHS)], timeout_seconds, credential)
                   for index in range(requests)]
        for future in as_completed(futures):
            outcomes.append(future.result())
    elapsed_seconds = time.perf_counter() - started
    total_failed = sum(1 for _, status, _, _ in outcomes if status != 200)
    return {
        "ok": total_failed == 0,
        "base_url": base_url,
        "clients": clients,
        "requests": requests,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "requests_per_second": round(len(outcomes) / elapsed_seconds, 2) if elapsed_seconds else 0.0,
        "failed": total_failed,
        "endpoints": summarize(outcomes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运价库销售查询只读压测")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--clients", type=int, default=DEFAULT_CLIENTS)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--confirm-high-load", action="store_true")
    parser.add_argument("--credential-file", type=Path, help="从本机文件读取只读压测所需凭据，不输出内容")
    parser.add_argument("--out", type=Path, help="将 JSON 结果写入指定文件")
    args = parser.parse_args()
    if not 1 <= args.clients <= MAX_CLIENTS:
        raise SystemExit("clients 必须在 1-%d 之间" % MAX_CLIENTS)
    if args.requests < args.clients:
        raise SystemExit("requests 不能小于 clients")
    if args.clients > HIGH_LOAD_CLIENTS and not args.confirm_high_load:
        raise SystemExit("超过 %d 并发必须加 --confirm-high-load，并在低峰执行" % HIGH_LOAD_CLIENTS)
    credential = args.credential_file.read_text(encoding="utf-8").strip() if args.credential_file else ""
    result = run(args.base_url, args.clients, args.requests, args.timeout, credential)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
