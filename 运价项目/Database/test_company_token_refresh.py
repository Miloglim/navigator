import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.company_credentials_contracts import (KB_AUTH_REFRESH_URL, KB_TEST_TOKEN_URL,
    KB_DINGTALK_START_URL, COMPANY_TOKEN_REFRESH_CONNECT_ROUTE, COMPANY_TOKEN_REFRESH_ROUTE)
from ops.company_credentials import CompanyCredentialStore
from ops.company_token_refresh import CompanyTokenRefresher
from transport.company_credential_routes import build_company_credential_routes


class CompanyTokenRefreshTests(unittest.TestCase):
    def test_refresh_rotates_existing_credential_without_exposing_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            seal = lambda value: b"sealed:" + value[::-1]
            unseal = lambda value: value.removeprefix(b"sealed:")[::-1]
            clock = [datetime(2026, 10, 16, tzinfo=timezone.utc)]
            credentials = CompanyCredentialStore(Path(directory) / "company.bin", seal, unseal)
            credentials.save({"token": "kbtt_old", "user_id": 2246998}, "admin-1")
            calls = []

            def post(url, body, bearer=None):
                calls.append((url, bearer))
                if url == KB_AUTH_REFRESH_URL:
                    self.assertIn(body["refresh_token"], ("refresh-secret", "rotated-refresh"))
                    return {"access_token": "short-access", "refresh_token": "rotated-refresh"}
                self.assertEqual(url, KB_TEST_TOKEN_URL)
                return {"token": "kbtt_new"}

            refresher = CompanyTokenRefresher(credentials, Path(directory) / "refresh.bin", seal, unseal,
                                               now=lambda: clock[0], post=post)
            refresher.configure("refresh-secret", "admin-1")
            result = refresher.refresh("admin-1", force=True)
            self.assertEqual(credentials.credentials(), {"token": "kbtt_new", "user_id": 2246998})
            self.assertEqual(calls, [(KB_AUTH_REFRESH_URL, None), (KB_TEST_TOKEN_URL, "short-access")])
            self.assertTrue(result["auto_refresh"])
            self.assertNotIn("kbtt_new", str(result))
            self.assertNotIn("refresh-secret", (Path(directory) / "refresh.bin").read_bytes().decode())
            self.assertEqual(len(calls), 2)
            refresher.refresh()
            self.assertEqual(len(calls), 2)
            get_routes, post_routes = build_company_credential_routes(credentials, refresher)
            request = {"_role": "admin", "_client_ip": "127.0.0.1", "_actor_id": "admin-1"}
            self.assertIsNone(post_routes["/api/admin/company-token-refresh/now"](request)["data"]["last_error"])
            self.assertEqual(len(calls), 4)
            clock[0] += timedelta(hours=22)
            self.assertIsNone(refresher.refresh()["last_error"])
            self.assertEqual(len(calls), 6)
            self.assertFalse(post_routes["/api/admin/company-token-refresh/disable"](request)["data"]["auto_refresh"])
            self.assertTrue(refresher.status("admin-1")["can_resume"])
            refresher.refresh()
            self.assertEqual(len(calls), 6)
            self.assertTrue(post_routes["/api/admin/company-token-refresh/enable"](request)["data"]["auto_refresh"])
            self.assertEqual(len(calls), 6)
            self.assertNotIn("refresh_token", str(get_routes["/api/admin/company-token-refresh"](request)))
            self.assertFalse(refresher.status("admin-2")["auto_refresh"])
            self.assertIsNone(refresher.status("admin-2")["last_success_at"])
            with self.assertRaises(PermissionError):
                post_routes["/api/admin/company-token-refresh/now"]({**request, "_client_ip": "192.0.2.1"})
            with self.assertRaises(PermissionError):
                post_routes["/api/admin/company-token-refresh/enable"]({**request, "_client_ip": "192.0.2.1"})

    def test_failed_refresh_keeps_current_token_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            seal = lambda value: b"sealed:" + value[::-1]
            unseal = lambda value: value.removeprefix(b"sealed:")[::-1]
            credentials = CompanyCredentialStore(Path(directory) / "company.bin", seal, unseal)
            credentials.save({"token": "kbtt_old", "user_id": 2246998}, "admin-1")

            def failing_post(url, body, bearer=None):
                if url == KB_AUTH_REFRESH_URL:
                    return {"access_token": "short-access"}
                raise RuntimeError("知识库接口返回 HTTP 503")

            refresher = CompanyTokenRefresher(credentials, Path(directory) / "refresh.bin", seal, unseal,
                                               post=failing_post)
            refresher.configure("refresh-secret", "admin-1")
            status = refresher.refresh("admin-1", force=True)
            self.assertEqual(credentials.credentials()["token"], "kbtt_old")
            self.assertIn("503", status["last_error"])
            self.assertNotIn("refresh-secret", str(status))
            with self.assertRaises(PermissionError):
                refresher.disable("admin-2")
            self.assertFalse(refresher.disable("admin-1")["auto_refresh"])

    def test_remote_dingtalk_connection_keeps_secret_out_of_status(self):
        with tempfile.TemporaryDirectory() as directory:
            seal = lambda value: b"sealed:" + value[::-1]
            unseal = lambda value: value.removeprefix(b"sealed:")[::-1]
            credentials = CompanyCredentialStore(Path(directory) / "company.bin", seal, unseal)
            credentials.save({"token": "kbtt_old", "user_id": 2246998}, "admin-1")

            def post(url, body, bearer=None):
                if url == KB_DINGTALK_START_URL:
                    self.assertEqual(body, {"device_type": "web"})
                    return {"data": {"auth_url": "https://login.dingtalk.com/oauth2/auth?x=1", "state": "state-1", "expires_in": 300}}
                if url == KB_AUTH_REFRESH_URL:
                    self.assertEqual(body, {"refresh_token": "browser-secret"})
                    return {"access_token": "short-access"}
                self.assertEqual(url, KB_TEST_TOKEN_URL)
                return {"token": "kbtt_new"}

            def get(url):
                self.assertIn("state=state-1", url)
                return {"data": {"status": "success", "refresh_token": "browser-secret"}}

            refresher = CompanyTokenRefresher(credentials, Path(directory) / "refresh.bin", seal, unseal,
                                               post=post, get=get)
            get_routes, routes = build_company_credential_routes(credentials, refresher)
            request = {"_role": "admin", "_client_ip": "127.0.0.1", "_actor_id": "admin-1"}
            with self.assertRaises(PermissionError):
                routes[COMPANY_TOKEN_REFRESH_CONNECT_ROUTE]({**request, "_client_ip": "192.0.2.1"})
            started = routes[COMPANY_TOKEN_REFRESH_CONNECT_ROUTE](request)["data"]
            self.assertIn("login.dingtalk.com", started["auth_url"])
            self.assertNotIn("browser-secret", str(started))
            result = get_routes[COMPANY_TOKEN_REFRESH_ROUTE](request)["data"]
            self.assertEqual(result["connection_state"], "connected")
            self.assertTrue(result["expiry_estimated"])
            self.assertEqual(credentials.credentials()["token"], "kbtt_new")
            self.assertNotIn("browser-secret", str(result))

    def test_pending_remote_connection_and_disable_do_not_change_current_token(self):
        with tempfile.TemporaryDirectory() as directory:
            seal = lambda value: b"sealed:" + value[::-1]
            unseal = lambda value: value.removeprefix(b"sealed:")[::-1]
            credentials = CompanyCredentialStore(Path(directory) / "company.bin", seal, unseal)
            credentials.save({"token": "kbtt_old", "user_id": 2246998}, "admin-1")
            started = {"data": {"auth_url": "https://login.dingtalk.com/oauth2/auth?x=2", "state": "state-2", "expires_in": 300}}
            refresher = CompanyTokenRefresher(credentials, Path(directory) / "refresh.bin", seal, unseal,
                                               post=lambda url, body, bearer=None: started,
                                               get=lambda url: {"data": {"status": "pending"}})
            refresher.connect_browser("admin-1")
            self.assertEqual(refresher.poll_connection("admin-1")["connection_state"], "connecting")
            refresher.disable("admin-1")
            self.assertEqual(refresher.status("admin-1")["connection_state"], "idle")
            self.assertFalse(refresher.status("admin-1")["auto_refresh"])
            self.assertEqual(credentials.credentials()["token"], "kbtt_old")

    def test_connection_check_reuses_valid_refresh_without_starting_login(self):
        with tempfile.TemporaryDirectory() as directory:
            seal = lambda value: b"sealed:" + value[::-1]
            unseal = lambda value: value.removeprefix(b"sealed:")[::-1]
            credentials = CompanyCredentialStore(Path(directory) / "company.bin", seal, unseal)
            credentials.save({"token": "kbtt_old", "user_id": 2246998}, "admin-1")
            calls = []

            def post(url, body, bearer=None):
                calls.append(url)
                if url == KB_AUTH_REFRESH_URL:
                    return {"access_token": "short-access"}
                self.assertEqual(url, KB_TEST_TOKEN_URL)
                return {"token": "kbtt_checked"}

            refresher = CompanyTokenRefresher(credentials, Path(directory) / "refresh.bin", seal, unseal,
                                               post=post, get=lambda url: self.fail("DingTalk login must not start"))
            refresher.configure("valid-refresh", "admin-1")
            result = refresher.connect_browser("admin-1")
            self.assertTrue(result["authenticated"])
            self.assertIsNone(result["auth_url"])
            self.assertEqual(calls, [KB_AUTH_REFRESH_URL, KB_TEST_TOKEN_URL])
            self.assertEqual(credentials.credentials()["token"], "kbtt_checked")


if __name__ == "__main__":
    unittest.main()
