import hmac
import json
import logging
import secrets
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from agent_watch.core.contracts import HOST, PORT, MAX_BODY_BYTES, VERSION, HEALTH_PATH, REPORT_PATH, CONTROL_PATH
from agent_watch.transport.store import Store
from agent_watch.core.contracts import ORCHESTRATOR_PAGE_PATH, ORCHESTRATOR_RUNS_PATH, ORCHESTRATOR_HEALTH_PATH
from agent_watch.transport.orchestrator import OrchestratorReader
from agent_watch.core.contracts import ORCHESTRATOR_DETAIL_PATH

def serve(runtime, web, port=PORT, observe_desktop=True):
    runtime.mkdir(parents=True, exist_ok=True)
    token_path = runtime / "monitor-token.txt"
    try:
        with token_path.open("x", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe())
    except FileExistsError:
        pass # Existing per-user credential is intentionally reused.
    token = token_path.read_text(encoding="utf-8").strip()
    session = secrets.token_urlsafe()
    store = Store(runtime / "monitor.sqlite3")
    orchestrator = OrchestratorReader(web.parents[2])
    from agent_watch.transport.desktop_observer import DesktopObserver
    if observe_desktop:
        DesktopObserver(store, Path.home()/".codex"/"sessions", web.parents[3]).start()
    origin = "http://" + HOST + ":" + str(port)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if self.command == "GET" and len(args) > 1 and str(args[1]) == "200":
                return # Healthy UI polling is intentionally quiet to avoid unbounded routine logs.
            logger = logging.warning if len(args) > 1 and str(args[1]).startswith(("4", "5")) else logging.info
            logger("[HTTP] %s", fmt % args)
        def reply(self, data, code=HTTPStatus.OK):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        def valid_host(self):
            return self.headers.get("Host") == HOST + ":" + str(port)
        def do_GET(self):
            if not self.valid_host():
                return self.reply({"ok":False,"error":"invalid host"}, HTTPStatus.FORBIDDEN)
            request = urlparse(self.path)
            args = parse_qs(request.query)
            if request.path == "/":
                body = (web / "index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "monitor_session=" + session + "; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif request.path == HEALTH_PATH:
                self.reply({"ok":True,"data":{"service":"freight-agent-monitor","version":VERSION}})
            elif request.path == "/api/runs":
                self.reply({"ok":True,"data":store.runs()})
            elif request.path == "/api/events":
                self.reply({"ok":True,"data":store.events(args.get("run_id", [""])[0])})
            elif request.path == ORCHESTRATOR_RUNS_PATH:
                try:
                    self.reply({"ok":True,"data":orchestrator.runs()})
                except Exception:
                    logging.exception("[ORCHESTRATOR] snapshot read failed")
                    self.reply({"ok":False,"error":"编排器状态读取失败"},HTTPStatus.INTERNAL_SERVER_ERROR)
            elif request.path == ORCHESTRATOR_HEALTH_PATH:
                self.reply({"ok":True,"data":orchestrator.health()})
            elif request.path == ORCHESTRATOR_DETAIL_PATH:
                try:
                    self.reply({'ok': True, 'data': orchestrator.store.detail(args.get('id', [''])[0])})
                except ValueError as exc:
                    self.reply({'ok': False, 'error': str(exc)}, HTTPStatus.NOT_FOUND)
            elif request.path == ORCHESTRATOR_PAGE_PATH:
                body = orchestrator.page()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'self'")
                self.end_headers()
                self.wfile.write(body)
            elif request.path == CONTROL_PATH:
                try:
                    self.reply({"ok":True,"data":{"action":store.control(args.get("run_id", [""])[0])}})
                except ValueError as e:
                    self.reply({"ok":False,"error":str(e)},HTTPStatus.NOT_FOUND)
            else:
                self.reply({"ok":False,"error":"not found"},404)
        def do_POST(self):
            if not self.valid_host():
                return self.reply({"ok":False,"error":"invalid host"}, HTTPStatus.FORBIDDEN)
            auth = self.headers.get("Authorization", "")
            bearer = hmac.compare_digest(auth, "Bearer " + token)
            browser = self.headers.get("Origin") == origin and ("monitor_session=" + session) in self.headers.get("Cookie", "").split("; ")
            if not bearer and not browser:
                return self.reply({"ok":False,"error":"unauthorized"},HTTPStatus.FORBIDDEN)
            if self.path == REPORT_PATH and not bearer:
                return self.reply({"ok":False,"error":"report requires runner identity"},403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length))
                if self.path == REPORT_PATH:
                    data = store.report(body)
                elif self.path == CONTROL_PATH:
                    data = {"action":store.control(body.get("run_id", ""), body.get("action"))}
                else:
                    return self.reply({"ok":False,"error":"not found"},404)
                self.reply({"ok":True,"data":data})
            except (ValueError, TypeError, AttributeError) as e:
                self.reply({"ok":False,"error":str(e)},HTTPStatus.BAD_REQUEST)
            except Exception:
                logging.exception("[API] monitor request failed")
                self.reply({"ok":False,"error":"monitor internal error"},HTTPStatus.INTERNAL_SERVER_ERROR)
    server = ThreadingHTTPServer((HOST, port), Handler)
    logging.info("[START] monitor listening %s", origin)
    server.serve_forever()
