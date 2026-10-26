"""Account authorization regressions; every write targets a temporary database/schema."""
from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

import manualdb
import manual_server
from core.contracts import LIVENESS_ROUTE, SALES_SCOPED_GET, OPS_OWNERSHIP_GET
from core.agent_intake_contracts import AGENT_INTAKE_ROUTE
from test_port_routes import seed, quote
from transport.access_rules import (authorize, SALES_GET, OPS_GET, ADMIN_GET, OPS_POST, ADMIN_POST)


def verify_account_http(case, connection_factory):
    password = secrets.token_urlsafe(24)
    connection = connection_factory()
    try:
        connection.execute("DELETE FROM auth_sessions")
        connection.execute("DELETE FROM operators")
        connection.commit()
        seed(connection)
        for role in ("admin", "operator", "sales"):
            manualdb.save_operator(connection, {"name": "test-" + role, "role": role,
                                   "password_hash": manual_server.password_hash(password)})
    finally:
        connection.close()
    original = dict(manual_server.CFG)
    credential = "frtm_" + secrets.token_hex(16)
    manual_server.CFG["token"] = credential
    try:
        with patch.object(manual_server, "conn", side_effect=connection_factory):
            server = manual_server.FreightHTTPServer(("127.0.0.1", 0), manual_server.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_port

            def request(path, payload=None, cookie="", token=""):
                headers = {"Content-Type": "application/json"}
                if cookie:
                    headers["Cookie"] = cookie
                if token:
                    headers["x-push-token"] = token
                data = json.dumps(payload).encode() if payload is not None else None
                req = urllib.request.Request(base + path, data=data, headers=headers)
                try:
                    response = urllib.request.urlopen(req, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, json.load(response), response.headers

            try:
                case.assertEqual(request(LIVENESS_ROUTE)[0], 200)
                for path in ("/api/rates", "/api/stats", "/api/bootstrap", "/api/master", "/images/unknown.png"):
                    case.assertEqual(request(path)[0], 401, path)
                case.assertEqual(request("/api/message-board")[0], 404)
                case.assertEqual(request("/api/message-board/create", {"content": "development only"})[0], 404)
                case.assertEqual(request("/api/validate", {})[0], 401)
                cookies = {}
                identities = {}
                for role in ("admin", "operator", "sales"):
                    status, result, headers = request("/api/auth/login", {"name": "test-" + role, "password": password})
                    case.assertEqual(status, 200)
                    case.assertTrue(result["user"]["user_id"].startswith("usr_"))
                    if role == 'operator':
                        case.assertIn('account', result['user']['views'])
                        case.assertNotIn('config', result['user']['views'])
                    if role == 'admin':
                        case.assertIn('account', result['user']['views'])
                    if role == 'sales':
                        case.assertIn('account', result['user']['views'])
                    identities[role] = result["user"]["user_id"]
                    cookies[role] = headers["Set-Cookie"].split(";", 1)[0]
                private_error = 'internal database path must never reach clients'
                with patch.dict(manual_server.GET_ROUTES, {'/api/stats': Mock(side_effect=RuntimeError(private_error))}):
                    status, result, _ = request('/api/stats', cookie=cookies['sales'])
                    case.assertEqual(status, 500)
                    case.assertEqual(result['error'], '服务器内部错误')
                with patch.dict(manual_server.POST_ROUTES, {'/api/master/resolve': Mock(side_effect=RuntimeError(private_error))}):
                    status, result, _ = request('/api/master/resolve', {}, cookies['sales'])
                    case.assertEqual(status, 500)
                    case.assertEqual(result['error'], '服务器内部错误')
                for path in SALES_GET - {"/api/item-feedback", "/api/company-rates/tasks"}:
                    case.assertEqual(request(path, cookie=cookies["sales"])[0], 200, path)
                case.assertEqual(request('/api/company-rates/tasks', cookie=cookies['sales'])[0], 400)
                case.assertEqual(request('/api/company-rates/tasks?id=missing', cookie=cookies['sales'])[0], 404)
                case.assertEqual(request("/api/item-feedback", cookie=cookies["sales"])[0], 400)
                for path in OPS_GET | ADMIN_GET:
                    case.assertEqual(request(path, cookie=cookies["sales"])[0], 403, path)
                for path in OPS_POST | ADMIN_POST:
                    case.assertEqual(request(path, {}, cookies["sales"], credential)[0], 403, path)
                case.assertEqual(request("/api/master?full=1", cookie=cookies["sales"])[0], 403)
                case.assertEqual(request("/api/bootstrap", cookie=cookies["sales"])[1]["operators"], [])
                for path in OPS_GET:
                    case.assertEqual(request(path, cookie=cookies["operator"])[0], 200, path)
                for path in ADMIN_GET:
                    case.assertEqual(request(path, cookie=cookies["operator"])[0], 403, path)
                for path in ADMIN_POST:
                    case.assertEqual(request(path, {}, cookies["operator"])[0], 403, path)
                case.assertEqual(request("/api/local-token")[0], 403)
                record = dict(quote(), **{"消息ID": "auth-isolated", "来源群": "auth isolated group"})
                case.assertEqual(request("/api/master?full=1", token=credential)[0], 200)
                validated = request("/api/validate", {"kind": "rates", "records": [record]}, token=credential)
                case.assertEqual(validated[1]["good"], 1)
                staged = request("/api/pending", {"kind": "rates", "records": [record]}, token=credential)[1]
                case.assertEqual(staged["staged"], 1)
                case.assertEqual(request("/api/pending", token=credential)[0], 200)
                for path in ("/api/push", "/api/pending/approve", "/api/record/update", "/api/master/save"):
                    case.assertEqual(request(path, {}, token=credential)[0], 403, path)
                approved = request("/api/pending/approve", {"id": staged["ids"][0], "_actor": "forged-admin"}, cookies["operator"])
                case.assertTrue(approved[1]["ok"], approved)
                connection = connection_factory()
                try:
                    case.assertEqual(connection.execute("SELECT reviewed_by FROM pending_records WHERE id=?", (staged["ids"][0],)).fetchone()[0], "test-operator")
                finally:
                    connection.close()
                case.assertEqual(request("/api/pending/approve_all", {}, cookies["operator"])[0], 200)
                case.assertEqual(request("/api/pending/delete", {"id": staged["ids"][0], "force": True}, cookies["operator"])[0], 403)
                # Viewer is only an admin preview; sales/operator cannot request another scope.
                connection = connection_factory()
                try:
                    manualdb.save_operator(connection, {"name": "test-sales", "groups": "somewhere-else"})
                    manualdb.save_operator(connection, {"name": "test-operator", "groups": "somewhere-else"})
                finally:
                    connection.close()
                for cookie in (cookies["sales"], cookies["operator"]):
                    for path in ("/api/rates", "/api/rates_grouped"):
                        case.assertEqual(request(path, cookie=cookie)[1]["total"], 1, path)
                        case.assertEqual(request(path + "?viewer=test-admin", cookie=cookie)[1]["total"], 1, path)
                case.assertGreater(request("/api/rates", cookie=cookies["admin"])[1]["total"], 0)
                connection = connection_factory()
                try:
                    manualdb.save_operator(connection, {"name": "test-sales", "groups": "auth isolated group"})
                finally:
                    connection.close()
                case.assertGreater(request("/api/rates?viewer=test-admin", cookie=cookies["sales"])[1]["total"], 0)
                published = request("/api/rates", cookie=cookies["sales"])[1]["rows"][0]
                entry_id = published["entry_id"]
                original_price = published["price_usd"]
                suggestion = request("/api/item-feedback", {
                    "entry_id": entry_id, "changes": {"price_usd": str(original_price + 1)},
                    "expected": {"price_usd": str(original_price)}, "note": "客户核实", "_actor_id": identities["admin"]
                }, cookies["sales"])
                case.assertEqual(suggestion[0], 200, suggestion)
                feedback = request("/api/item-feedback?entry_id=" + entry_id, cookie=cookies["operator"])[1]["data"]["feedback"]
                case.assertEqual(feedback[0]["author_user_id"], identities["sales"])
                case.assertEqual(request("/api/rates", cookie=cookies["sales"])[1]["rows"][0]["price_usd"], original_price)
                case.assertEqual(request("/api/item-feedback", {"entry_id": entry_id}, token=credential)[0], 403)
                dismissed = request("/api/item-feedback/dismiss", {
                    "submission_id": suggestion[1]["data"]["submission_id"], "action": "reject"
                }, cookies["operator"])
                case.assertEqual(dismissed[0], 200, dismissed)
                case.assertFalse(request("/api/item-feedback?entry_id=" + entry_id, cookie=cookies["sales"])[1]["data"]["feedback"])
                for body in ({"name": "test-admin", "active": 0}, {"name": "test-admin", "role": "sales"}):
                    case.assertEqual(request("/api/operator/save", body, cookies["admin"])[0], 403)
                case.assertEqual(request("/api/operator/delete", {"name": "test-admin"}, cookies["admin"])[0], 403)
                profile = request("/api/operator/save", {"name": "test-sales", "title": "updated profile"}, cookies["admin"])[1]
                case.assertFalse(profile["sessions_revoked"])
                case.assertEqual(profile["user_id"], identities["sales"])
                case.assertEqual(request("/api/auth/status", cookie=cookies["sales"])[1]["user"]["role"], "sales")
                changed = request("/api/operator/save", {"name": "test-sales", "role": "operator"}, cookies["admin"])[1]
                case.assertTrue(changed["sessions_revoked"])
                case.assertEqual(request("/api/rates", cookie=cookies["sales"])[0], 401)
                # Two browser sessions coexist; logging out one does not revoke the other.
                second = request("/api/auth/login", {"name": "test-admin", "password": password})[2]["Set-Cookie"].split(";", 1)[0]
                case.assertEqual(request("/api/auth/logout", {}, cookies["admin"])[0], 200)
                case.assertEqual(request("/api/rates", cookie=cookies["admin"])[0], 401)
                case.assertEqual(request("/api/rates", cookie=second)[0], 200)
                case.assertEqual(request("/api/operator/save", {"name": "new-sales", "role": "sales", "password": password}, second)[0], 200)
                # Disable and password-reset both invalidate existing sessions.
                case.assertTrue(request("/api/operator/save", {"name": "test-operator", "active": 0}, second)[1]["sessions_revoked"])
                case.assertEqual(request("/api/pending", cookie=cookies["operator"])[0], 401)
                changed = request("/api/operator/save", {"name": "test-admin", "password": password + "changed"}, second)[1]
                case.assertTrue(changed["sessions_revoked"])
                case.assertEqual(request("/api/rates", cookie=second)[0], 401)
            finally:
                server.shutdown()
                thread.join(timeout=10)
                server.server_close()
    finally:
        manual_server.CFG.clear()
        manual_server.CFG.update(original)
        manualdb.invalidate_master()


class AccountAuthTests(unittest.TestCase):
    def test_center_auth_status_never_checks_local_admin_setup(self):
        with patch.dict(manual_server.CFG, {"center_auth_url":"https://center.example","dev_mode":False}, clear=False), \
                patch.object(manual_server, "active_admin_exists") as local_admin:
            status = manual_server.auth_status(None)
        local_admin.assert_not_called()
        self.assertEqual(status["auth_mode"], "center")
        self.assertFalse(status["setup_required"])
        self.assertFalse(status["authenticated"])
        self.assertFalse(status["features"]["message_board"])

    def test_local_auth_status_keeps_first_admin_setup(self):
        with patch.dict(manual_server.CFG, {"center_auth_url":""}, clear=False), \
                patch.object(manual_server, "active_admin_exists", return_value=False):
            status = manual_server.auth_status(None)
        self.assertEqual(status["auth_mode"], "local")
        self.assertTrue(status["setup_required"])

    def test_dev_mode_is_the_only_message_board_feature_switch(self):
        with patch.dict(manual_server.CFG, {"center_auth_url":"https://center.example","dev_mode":True}, clear=False):
            self.assertTrue(manual_server.auth_status(None)["features"]["message_board"])
        with patch.dict(manual_server.CFG, {"center_auth_url":"https://center.example","dev_mode":False}, clear=False):
            self.assertFalse(manual_server.auth_status(None)["features"]["message_board"])

    def test_center_mode_rejects_local_admin_setup(self):
        with patch.dict(manual_server.CFG, {"center_auth_url":"https://center.example"}, clear=False):
            with self.assertRaisesRegex(PermissionError, "中心管理库"):
                manual_server.auth_setup({"name":"local-admin","password":"fixture-pass"})

    def test_center_outage_preserves_existing_browser_session(self):
        sid = "center-outage-session"
        with tempfile.TemporaryDirectory(prefix="freight-center-outage-") as directory:
            database = Path(directory) / "isolated.db"
            connection = manualdb.connect(database)
            try:
                manualdb.save_operator(connection, {"name": "center-user", "role": "admin"})
                connection.execute(
                    "INSERT INTO auth_sessions(token_hash,operator_name,client_ip,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (hashlib.sha256(sid.encode("ascii")).hexdigest(), "center-user", "127.0.0.1",
                     int(time.time()) + 1000, "fixture"))
                connection.commit()
            finally:
                connection.close()
            handler = manual_server.Handler.__new__(manual_server.Handler)
            handler.headers = {"Cookie": f"freight_session={sid}; freight_center_refresh=refresh-token"}
            handler._pending_auth_headers = {}
            manual_server.CENTER_SESSION_TOKENS.pop(sid, None)
            try:
                with patch.dict(manual_server.CFG, {"center_auth_url": "https://center.example"}, clear=False), \
                        patch.object(manual_server, "conn", side_effect=lambda: manualdb.connect(database)), \
                        patch.object(manual_server.center_auth, "refresh",
                                     side_effect=manual_server.center_auth.CenterAuthUnavailable("中心会话续期暂不可用")):
                    with self.assertRaises(manual_server.center_auth.CenterAuthUnavailable):
                        handler._session()
                connection = manualdb.connect(database)
                try:
                    count = connection.execute("SELECT COUNT(*) FROM auth_sessions WHERE token_hash=?",
                                               (hashlib.sha256(sid.encode("ascii")).hexdigest(),)).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(count, 1)
            finally:
                manual_server.CENTER_SESSION_TOKENS.pop(sid, None)

    def test_concurrent_center_refresh_consumes_rotating_token_once(self):
        sid = "center-refresh-session"
        with tempfile.TemporaryDirectory(prefix="freight-center-refresh-") as directory:
            database = Path(directory) / "isolated.db"
            connection = manualdb.connect(database)
            try:
                manualdb.save_operator(connection, {"name": "center-user", "role": "admin"})
                connection.execute(
                    "INSERT INTO auth_sessions(token_hash,operator_name,client_ip,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (hashlib.sha256(sid.encode("ascii")).hexdigest(), "center-user", "127.0.0.1",
                     int(time.time()) + 1000, "fixture"))
                connection.commit()
            finally:
                connection.close()

            refresh_calls = []
            def refresh(_base_url, _refresh_token):
                refresh_calls.append(_refresh_token)
                time.sleep(0.05)
                return {"token": "new-access", "refresh_token": "new-refresh", "identity": {"displayName": "Center User"}}
            def validate(_base_url, token):
                if token != "new-access":
                    raise manual_server.center_auth.CenterAuthError("access expired")
                return {"identity": {"displayName": "Center User"}, "access": {"userId": "u1", "role": "superadmin"}}

            results = []
            def run_session():
                handler = manual_server.Handler.__new__(manual_server.Handler)
                handler.headers = {"Cookie": f"freight_session={sid}; freight_center_refresh=refresh-token"}
                handler.connection = object()
                handler.client_address = ("127.0.0.1", 0)
                handler._pending_auth_headers = {}
                try:
                    results.append(handler._session())
                except Exception as error:  # noqa: BLE001 - surface thread failures in the assertion
                    results.append(error)

            manual_server.CENTER_SESSION_TOKENS.pop(sid, None)
            try:
                with patch.dict(manual_server.CFG, {"center_auth_url": "https://center.example"}, clear=False), \
                        patch.object(manual_server, "conn", side_effect=lambda: manualdb.connect(database)), \
                        patch.object(manual_server.center_auth, "refresh", side_effect=refresh), \
                        patch.object(manual_server.center_auth, "validate", side_effect=validate):
                    threads = [threading.Thread(target=run_session) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=10)
                self.assertEqual(refresh_calls, ["refresh-token"])
                self.assertEqual(len(results), 2)
                self.assertTrue(all(isinstance(result, dict) for result in results), results)
                connection = manualdb.connect(database)
                try:
                    role = connection.execute("SELECT role FROM operators WHERE name='center-user'").fetchone()["role"]
                finally:
                    connection.close()
                self.assertEqual(role, "superadmin")
            finally:
                manual_server.CENTER_SESSION_TOKENS.pop(sid, None)

    def test_center_auth_cache_only_serves_allowlisted_reads(self):
        sid = "center-auth-cache-session"
        with tempfile.TemporaryDirectory(prefix="freight-center-cache-") as directory:
            database = Path(directory) / "isolated.db"
            connection = manualdb.connect(database)
            try:
                manualdb.save_operator(connection, {"name": "center-user", "role": "sales"})
                connection.execute(
                    "INSERT INTO auth_sessions(token_hash,operator_name,client_ip,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (hashlib.sha256(sid.encode("ascii")).hexdigest(), "center-user", "127.0.0.1",
                     int(time.time()) + 1000, "fixture"))
                connection.commit()
            finally:
                connection.close()
            handler = manual_server.Handler.__new__(manual_server.Handler)
            handler.headers = {"Cookie": f"freight_session={sid}"}
            handler.connection = object()
            handler.client_address = ("127.0.0.1", 0)
            handler._pending_auth_headers = {}
            access = {"identity": {"displayName": "Center User"}, "access": {"userId": "u1", "role": "admin"}}
            manual_server.CENTER_SESSION_TOKENS[sid] = "center-token"
            manual_server.CENTER_AUTH_CACHE.pop(sid, None)
            try:
                with patch.dict(manual_server.CFG, {"center_auth_url": "https://center.example"}, clear=False), \
                        patch.object(manual_server, "conn", side_effect=lambda: manualdb.connect(database)), \
                        patch.object(manual_server.center_auth, "validate", return_value=access) as validate:
                    self.assertEqual(handler._session(cache_auth=True)["role"], "admin")
                    self.assertEqual(handler._session(cache_auth=True)["role"], "admin")
                    self.assertEqual(validate.call_count, 1)
                    manual_server.CENTER_AUTH_CACHE[sid] = (time.monotonic() - 1, "center-token", access)
                    self.assertEqual(handler._session(cache_auth=True)["role"], "admin")
                    self.assertEqual(validate.call_count, 2)
                    self.assertEqual(handler._session()["role"], "admin")
                    self.assertEqual(validate.call_count, 3)
                self.assertIn("/api/rates", manual_server.CENTER_AUTH_CACHED_READ_ROUTES)
                self.assertNotIn("/api/admin/company-credentials", manual_server.CENTER_AUTH_CACHED_READ_ROUTES)
                self.assertNotIn("/api/operator/save", manual_server.CENTER_AUTH_CACHED_READ_ROUTES)
            finally:
                manual_server.CENTER_SESSION_TOKENS.pop(sid, None)
                manual_server.CENTER_AUTH_CACHE.pop(sid, None)

    def test_fonts_are_self_hosted_and_cacheable(self):
        web_root = Path(__file__).resolve().parent / "web"
        source = (web_root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("fonts.googleapis.com", source)
        self.assertIn("/assets/fonts/outfit-latin-wght-normal.woff2", source)
        self.assertTrue((web_root / "fonts" / "outfit-latin-wght-normal.woff2").is_file())
        self.assertTrue((web_root / "fonts" / "ibm-plex-mono-latin-400-normal.woff2").is_file())

        server = manual_server.FreightHTTPServer(("127.0.0.1", 0), manual_server.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/assets/fonts/outfit-latin-wght-normal.woff2")
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "font/woff2")
                self.assertIn("immutable", response.headers["Cache-Control"])
                self.assertEqual(response.read(4), b"wOF2")
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()

    def test_web_auth_has_no_admin_initialization_entry(self):
        source = (Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("初始化管理员", source)
        self.assertNotIn("/api/auth/setup", source)
        self.assertNotIn("bootstrapToken", source)
        self.assertNotIn("/api/auth/setup", manual_server.POST_ROUTES)

    def test_disconnected_browser_does_not_retry_response(self):
        for failure in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            handler = manual_server.Handler.__new__(manual_server.Handler)
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.wfile = Mock()
            handler.wfile.write.side_effect = failure("client disconnected")
            handler._send(200, b"fixture", "text/plain")
            handler.wfile.write.assert_called_once_with(b"fixture")

    def test_all_sales_routes_bind_to_current_login(self):
        from services.access_policy import scoped_sales_query
        for route in SALES_SCOPED_GET:
            self.assertEqual(scoped_sales_query(route, {"viewer": "other"},
                             {"name": "self", "role": "sales"}), {})
            self.assertEqual(scoped_sales_query(route, {},
                             {"name": "self", "role": "operator"}), {'viewer':'self'} if route in OPS_OWNERSHIP_GET else {})
            self.assertEqual(scoped_sales_query(route, {"viewer": "other"},
                             {"name": "admin", "role": "admin"}), {})
            self.assertEqual(scoped_sales_query(route, {}, None), {})

    def test_postgres_request_connection_never_runs_schema_ddl(self):
        import postgresdb
        with patch.object(postgresdb.psycopg, "connect") as connect:
            connection = postgresdb.Connection("isolated-fixture")
            connect.return_value.execute.assert_not_called()
            connect.return_value.commit.assert_not_called()
            connection.ensure_identity_schema()
            self.assertGreater(connect.return_value.execute.call_count, 0)
            connect.return_value.commit.assert_called_once()
            connection.close()

    def test_sqlite_http(self):
        with tempfile.TemporaryDirectory(prefix="freight-auth-") as directory:
            database = Path(directory) / "isolated.db"
            verify_account_http(self, lambda: manualdb.connect(database))

    def test_every_registered_route_has_an_explicit_admin_rule(self):
        for method, routes in (("GET", manual_server.GET_ROUTES), ("POST", manual_server.POST_ROUTES)):
            for route in routes:
                if route == AGENT_INTAKE_ROUTE:
                    self.assertFalse(authorize(method, route, "admin")["ok"])
                    self.assertTrue(authorize(method, route, agent=True)["ok"])
                    continue
                self.assertTrue(authorize(method, route, "admin")["ok"], route)
        self.assertFalse(authorize("POST", "/api/future-unlisted", "admin")["ok"])

    def test_sqlite_legacy_schema_upgrade_preserves_session_and_identity(self):
        connection = manualdb.connect(":memory:")
        try:
            connection.execute("DROP TABLE operators")
            legacy = manualdb.DDL["operators"].replace("user_id TEXT UNIQUE, ", "").replace("'admin','operator','sales'", "'admin','operator'")
            connection.execute(legacy)
            connection.execute("INSERT INTO operators(name,role,password_hash) VALUES(?,?,?)", ("legacy-admin", "admin", "fixture-hash"))
            connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?)", ("fixture-token", "legacy-admin", "127.0.0.1", int(time.time()) + 1000, "fixture"))
            connection.commit()
            self.assertEqual(manualdb.ensure_account_schema(connection)["data"]["identities_backfilled"], 1)
            first = connection.execute("SELECT user_id FROM operators").fetchone()[0]
            self.assertEqual(manualdb.find_active_auth_session(connection, "fixture-token", int(time.time()))["user_id"], first)
            self.assertEqual(manualdb.ensure_account_schema(connection)["data"]["identities_backfilled"], 0)
            manualdb.save_operator(connection, {"name": "legacy-sales", "role": "sales"})
            snapshot = manualdb.export_all(connection)
            for row in snapshot["operators"]:
                row.pop("user_id")
            manualdb.load_snapshot(connection, snapshot)
            self.assertEqual(connection.execute("SELECT user_id FROM operators WHERE name='legacy-admin'").fetchone()[0], first)
        finally:
            connection.close()

    def test_expired_session_and_recreated_account_identity(self):
        connection = manualdb.connect(":memory:")
        try:
            first = manualdb.save_operator(connection, {"name": "sales", "role": "sales", "password_hash": "fixture"})["user_id"]
            connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?)", ("expired", "sales", "127.0.0.1", int(time.time()) - 1, "fixture"))
            connection.commit()
            self.assertIsNone(manualdb.find_active_auth_session(connection, "expired", int(time.time())))
            manualdb.delete_named(connection, "operators", "sales")
            second = manualdb.save_operator(connection, {"name": "sales", "role": "sales", "password_hash": "fixture"})["user_id"]
            self.assertNotEqual(first, second)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
