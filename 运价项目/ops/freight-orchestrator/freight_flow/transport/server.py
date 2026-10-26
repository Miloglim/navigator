import json
import logging
import mimetypes
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from freight_flow.transport.daemon import service_status
from freight_flow.core.contracts import (
    HOST, PORT, HEALTH_PATH, RUNS_PATH, DETAIL_PATH, VERSION,
    TRACE_ROOTS_PATH, TRACE_RUN_PATH, TRACE_STREAM_PATH, TRACE_ACTION_PATH,
    TRACE_IMAGE_PATH, RUNTIME_OVERVIEW_PATH, DWS_LOGIN_PATH, RUNTIME_SCHEDULE_PATH,
    RUNTIME_WORKER_PATH, RUNTIME_TRIGGER_PATH, STUDIO_PATH,
)
from freight_flow.services.trace import trace_detail, trace_index
from freight_flow.services.workflow import validate_schema
from freight_flow.transport.agent import PRODUCTION_SCHEMAS, SCHEMAS
from freight_flow.transport.runtime_control import RuntimeControl


def server(store, web, port=PORT, runtime_control=None):
    runtime = Path(store.path).parent
    scripts = Path(web).resolve().parents[3] / 'Skill' / 'freight-rate-heartbeat' / 'scripts'
    if str(scripts) not in sys.path:
        sys.path.append(str(scripts))
    import ledger_client
    control = runtime_control or RuntimeControl(
        runtime, store, Path(web).parent.parent / "config" / "schedule.json")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if args and len(args) > 1 and int(args[1]) >= 400:
                logging.warning("[HTTP] " + fmt, *args)

        def do_GET(self):
            if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
                return self.reply({"ok": False, "error": "invalid host"}, code=403)
            query = urlparse(self.path)
            if query.path == "/favicon.ico":
                return self.reply(b"", code=204, content="image/x-icon")
            if query.path == "/":
                return self.reply(web.read_bytes(), content="text/html; charset=utf-8")
            if query.path == STUDIO_PATH:
                studio = web.with_name("studio.html")
                if not studio.is_file():
                    return self.reply({"ok": False, "error": "studio not installed"}, code=404)
                return self.reply(studio.read_bytes(), content="text/html; charset=utf-8")
            if query.path == HEALTH_PATH:
                status = service_status(runtime)
                mode = (status.get('data') or {}).get('mode') or 'readonly-monitor'
                return self.reply({"ok": True, "data": {"version": VERSION, "mode": mode,
                    "production_cutover": (runtime / 'production-enabled.json').is_file(),
                    'worker_alive': status['healthy'], 'service': status}})
            if query.path == RUNTIME_OVERVIEW_PATH:
                control.dws_auth.begin_check()
                return self.reply({"ok": True, "data": control.snapshot()})
            if query.path == RUNS_PATH:
                return self.reply({"ok": True, "data": store.runs()})
            if query.path == DETAIL_PATH:
                try:
                    run = parse_qs(query.query).get("id", [""])[0]
                    return self.reply({"ok": True, "data": store.detail(run)})
                except ValueError as exc:
                    return self.reply({"ok": False, "error": str(exc)}, code=404)
            if query.path == TRACE_ROOTS_PATH:
                return self.reply({"ok": True, "data": {"runs": trace_index(store.trace_roots())}})
            if query.path == TRACE_RUN_PATH:
                try:
                    run = parse_qs(query.query).get("id", [""])[0]
                    return self.reply({"ok": True, "data": trace_detail(store.detail(run))})
                except ValueError as exc:
                    return self.reply({"ok": False, "error": str(exc)}, code=404)
            if query.path == TRACE_IMAGE_PATH:
                return self.serve_image(query.query)
            if query.path == TRACE_STREAM_PATH:
                return self.stream_events(query.query)
            return self.reply({"ok": False, "error": "not found"}, code=404)

        def do_POST(self):
            if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
                return self.reply({"ok": False, "error": "invalid host"}, code=403)
            query = urlparse(self.path)
            if query.path == DWS_LOGIN_PATH:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return self.reply({"ok": False, "error": "json required"}, code=415)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > 1024:
                        raise ValueError("invalid request size")
                    json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    return self.reply({"ok": False, "error": "invalid json"}, code=400)
                return self.reply({"ok": True, "data": control.dws_auth.login()})
            if query.path in (RUNTIME_SCHEDULE_PATH, RUNTIME_WORKER_PATH):
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return self.reply({"ok": False, "error": "json required"}, code=415)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > 16384:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if query.path == RUNTIME_SCHEDULE_PATH:
                        control.save_schedule(body)
                    else:
                        control.save_worker(body)
                    return self.reply({"ok": True, "data": control.snapshot()})
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return self.reply({"ok": False, "error": str(exc)}, code=400)
            if query.path == RUNTIME_TRIGGER_PATH:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return self.reply({"ok": False, "error": "json required"}, code=415)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > 2048:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    slug = str(body.get("group_slug") or "").strip()
                    if not slug:
                        raise ValueError("group_slug required")
                    message_id = str(body.get("message_id") or "").strip() or None
                    return self.reply({"ok": True, "data": control.trigger_group(slug, message_id)})
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return self.reply({"ok": False, "error": str(exc)}, code=400)
            if query.path != TRACE_ACTION_PATH:
                return self.reply({"ok": False, "error": "not found"}, code=404)
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self.reply({"ok": False, "error": "json required"}, code=415)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 1024 * 1024:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                action, run_id = body.get("action"), str(body.get("run_id") or "")
                if not run_id:
                    raise ValueError("run_id required")
                if action == "resume":
                    result = {"resumed_tasks": store.resume(run_id)}
                elif action == "recheck_inventory":
                    store.recheck_inventory(run_id)
                    result = {"recheck_inventory": True}
                elif action == "recheck_source":
                    store.recheck_source(run_id, instruction=body.get("instruction"))
                    result = {"recheck_source": True}
                elif action == "amend_result":
                    stage = str(body.get("stage") or "")
                    value = body.get("result")
                    detail = store.detail(run_id)
                    schemas = PRODUCTION_SCHEMAS if detail.get("mode") == "production-message" else SCHEMAS
                    if stage not in ("classify", "extract", "inventory"):
                        raise ValueError("只能修改分类、抽取或盘点结果")
                    validate_schema(value, schemas[stage])
                    result = store.amend_result(run_id, stage, value, instruction=body.get("instruction"))
                elif action == "mark_noise":
                    reason = str(body.get("reason") or "").strip()
                    detail = store.detail(run_id)
                    source = detail.get("source") or {}
                    group = source.get("group") or {}
                    response = ledger_client.dismiss_review_issue(group.get("slug") or "", source.get("message_id") or "")
                    if not response.get("ok") or not (response.get("data") or {}).get("resolved"):
                        raise ValueError(response.get("error") or "台账没有可关闭的待核问题")
                    result = store.mark_noise(run_id, reason)
                elif action == "acknowledge_review":
                    result = store.acknowledge_review(
                        run_id, body.get("note"), str(body.get("reviewer") or "trace-operator"))
                else:
                    raise ValueError("unsupported action")
                return self.reply({"ok": True, "data": result})
            except (ValueError, json.JSONDecodeError) as exc:
                return self.reply({"ok": False, "error": str(exc)}, code=400)

        def serve_image(self, query):
            params = parse_qs(query)
            name = (params.get("name") or params.get("media") or [""])[0]
            if not name or Path(name).name != name:
                return self.reply({"ok": False, "error": "invalid image"}, code=400)
            project_root = Path(web).resolve().parents[3]
            images = (project_root / "Database" / "images").resolve()
            if Path(name).suffix:
                candidates = [images / name]
            else:
                candidates = list(images.glob(name + '.*'))
            candidates = [path for path in candidates if path.is_file() and path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')]
            if len(candidates) != 1:
                return self.reply({"ok": False, "error": "image not found"}, code=404)
            target = candidates[0].resolve()
            if not target.is_relative_to(images):
                return self.reply({"ok": False, "error": "invalid image"}, code=403)
            return self.reply(target.read_bytes(), content=mimetypes.guess_type(target.name)[0] or 'application/octet-stream')

        def stream_events(self, query):
            params = parse_qs(query)
            raw_after = (params.get("after") or [self.headers.get('Last-Event-ID')])[0]
            if raw_after:
                try:
                    after = max(0, int(raw_after))
                except ValueError:
                    after = store.latest_event_seq()
            else:
                after = store.latest_event_seq()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            deadline = time.monotonic() + 25
            try:
                ready = json.dumps({"seq": after, "at": time.time(), "reconnected": bool(raw_after)}, ensure_ascii=False)
                self.wfile.write(f"retry: 1500\nid: {after}\nevent: sync_ready\ndata: {ready}\n\n".encode('utf-8'))
                self.wfile.flush()
                while time.monotonic() < deadline:
                    events = store.events_after(after, 100)
                    if events:
                        for event in events:
                            after = int(event["seq"])
                            payload = json.dumps(event, ensure_ascii=False)
                            self.wfile.write(f"id: {after}\nevent: trace\ndata: {payload}\n\n".encode('utf-8'))
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            finally:
                self.close_connection = True

        def reply(self, body, code=200, content="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass  # Browser navigation can close a completed request before its body is read.
    return ThreadingHTTPServer((HOST, port), Handler)
