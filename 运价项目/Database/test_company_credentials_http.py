import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import manual_server as server
import manualdb


class CompanyCredentialHttpTests(unittest.TestCase):
    def test_local_admin_can_rotate_credential_without_echoing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "credentials-http.db"
            credential_path = Path(directory) / "credentials.bin"
            server.CFG.update(db=str(db_path), backend="sqlite", token="fixture-agent")
            store = server.COMPANY_CREDENTIAL_STORE
            prior = (store.path, store.protect_data, store.unprotect_data)
            store.path = credential_path
            store.protect_data = lambda value: b"sealed:" + value[::-1]
            store.unprotect_data = lambda value: value.removeprefix(b"sealed:")[::-1]
            connection = manualdb.connect(db_path)
            for user_id, name, role in (("usr-admin", "credential-admin", "admin"),
                                        ("usr-sales", "credential-sales", "sales")):
                connection.execute(
                    "INSERT INTO operators(user_id,name,display_name,role,active,password_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, name, name, role, 1, server.password_hash("fixture-pass"), manualdb.now_cn()),
                )
            connection.commit()
            connection.close()
            httpd = server.FreightHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"

            def call(path, body=None, cookie=""):
                request = urllib.request.Request(base + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type": "application/json", "Cookie": cookie, "Origin": base})
                try:
                    response = urllib.request.urlopen(request, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, json.load(response), response.headers

            try:
                admin_login = call("/api/auth/login", {"name": "credential-admin", "password": "fixture-pass"})
                admin_cookie = admin_login[2]["Set-Cookie"].split(";", 1)[0]
                saved = call("/api/admin/company-credentials", {"token": "rotating-token", "user_id": "123"}, admin_cookie)
                self.assertEqual(saved[0], 200, saved[1])
                self.assertNotIn("token", saved[1]["data"])
                self.assertNotIn(b"rotating-token", credential_path.read_bytes())
                status = call("/api/admin/company-credentials", cookie=admin_cookie)
                self.assertEqual(status[1]["data"]["user_id"], 123)

                sales_login = call("/api/auth/login", {"name": "credential-sales", "password": "fixture-pass"})
                sales_cookie = sales_login[2]["Set-Cookie"].split(";", 1)[0]
                self.assertEqual(call("/api/admin/company-credentials", cookie=sales_cookie)[0], 403)
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.path, store.protect_data, store.unprotect_data = prior


if __name__ == "__main__":
    unittest.main()
