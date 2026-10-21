import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import manual_server as server
import manualdb


class CompanyRateHttpTests(unittest.TestCase):
    def test_authenticated_task_route_is_isolated_from_local_rate_query(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "company-http.db"
            server.CFG.update(db=str(db_path), backend="sqlite", token="fixture-agent")
            previous_provider = server.COMPANY_RATE_RUNTIME.credential_provider
            previous_environ = server.COMPANY_RATE_RUNTIME.environ
            previous_master = server.COMPANY_RATE_RUNTIME.master_provider
            server.COMPANY_RATE_RUNTIME.environ = {}
            # Keep this route test independent from a developer's real rotating-token store.
            server.COMPANY_RATE_RUNTIME.credential_provider = lambda: None
            connection = manualdb.connect(db_path)
            connection.execute(
                "INSERT INTO operators(user_id,name,display_name,role,active,password_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                ("usr-company", "company-sales", "公司查价测试", "sales", 1,
                 server.password_hash("fixture-pass"), manualdb.now_cn()),
            )
            connection.execute(
                "INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,container_type,price_usd,status) VALUES(?,?,?,?,?,?,?,?,?)",
                ("frt-company-local", "company-local", "rates", "SHANGHAI", "LOS ANGELES", "COSCO", "40HQ/HC", 1100, "当前生效"),
            )
            connection.commit()
            connection.close()

            httpd = server.FreightHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"

            def call(path, body=None, cookie=""):
                request = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type": "application/json", "Cookie": cookie, "Origin": base},
                )
                try:
                    response = urllib.request.urlopen(request, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, json.load(response), response.headers

            try:
                login = call("/api/auth/login", {"name": "company-sales", "password": "fixture-pass"})
                cookie = login[2]["Set-Cookie"].split(";", 1)[0]
                local = call("/api/rates?pol=SHANGHAI", cookie=cookie)
                self.assertEqual(local[0], 200)
                self.assertEqual(local[1]["total"], 1)

                created = call("/api/company-rates/query", {
                    "pol": ["CNSHA"], "pod": ["USLAX"], "carriers": ["COSCO"]
                }, cookie)
                self.assertEqual(created[0], 200, created[1])
                task_id = created[1]["data"]["task_id"]
                task = None
                for _attempt in range(20):
                    task = call("/api/company-rates/tasks?id=" + task_id, cookie=cookie)
                    if task[1].get("data", {}).get("status") == "failed":
                        break
                    time.sleep(0.02)
                self.assertEqual(task[1]["data"]["status"], "failed")
                self.assertEqual(call("/api/company-rates/tasks?id=" + task_id)[0], 401)
                self.assertEqual(call('/api/admin/company-mappings',cookie=cookie)[0],403)
                self.assertEqual(call('/api/admin/company-mappings/save',{'_role':'admin','_actor_id':'admin'},cookie)[0],403)
                connection=manualdb.connect(db_path)
                connection.execute("INSERT INTO operators(user_id,name,display_name,role,active,password_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    ('usr-admin','mapping-admin','映射管理员','admin',1,server.password_hash('fixture-pass'),manualdb.now_cn()))
                connection.commit(); connection.close()
                login=call('/api/auth/login',{'name':'mapping-admin','password':'fixture-pass'})
                admin_cookie=login[2]['Set-Cookie'].split(';',1)[0]
                server.COMPANY_RATE_RUNTIME.environ={'YQN_DINGTALK_TOKEN':'fixture','YQN_USER_ID':'99'}
                server.COMPANY_RATE_RUNTIME.master_provider=lambda c:{'ports':[],'carriers':[{'code':'MSK'}]}
                saved=call('/api/admin/company-mappings/save',{'kind':'carrier','local_name':'MSK','company_id':4550,'company_code':'MAERSK',
                    'status':'confirmed','_actor_id':'spoofed'},admin_cookie)
                self.assertEqual(saved[0],200,saved[1])
                self.assertEqual(saved[1]['data']['updated_by'],'usr-admin')
                listed=call('/api/admin/company-mappings',cookie=admin_cookie)
                self.assertEqual(listed[1]['data']['rows'][0]['company_id'],4550)
            finally:
                httpd.shutdown()
                httpd.server_close()
                server.COMPANY_RATE_RUNTIME.credential_provider = previous_provider
                server.COMPANY_RATE_RUNTIME.environ = previous_environ
                server.COMPANY_RATE_RUNTIME.master_provider = previous_master


if __name__ == "__main__":
    unittest.main()
