import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ops import center_auth


class CenterAuthClientTests(unittest.TestCase):
    def test_invalid_credentials_are_not_reported_as_outage(self):
        error = HTTPError("http://center/api/auth/login", 401, "invalid", {}, None)
        with patch.object(center_auth, "urlopen", side_effect=error):
            with self.assertRaisesRegex(center_auth.CenterAuthError, "账号或 Navigator 权限无效"):
                center_auth.login("http://center", "user@example.com", "bad")

    def test_transport_failure_is_reported_as_unavailable(self):
        with patch.object(center_auth, "urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(center_auth.CenterAuthError, "中心账号登录暂不可用"):
                center_auth.login("http://center", "user@example.com", "bad")

    def test_rate_limit_is_not_reported_as_an_outage(self):
        error = HTTPError("http://center/api/auth/login", 429, "limited", {}, None)
        with patch.object(center_auth, "urlopen", side_effect=error):
            with self.assertRaisesRegex(center_auth.CenterAuthError, "尝试过多"):
                center_auth.login("http://center", "user@example.com", "bad")

    def test_navigator_access_stage_is_identifiable(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                import json
                return json.dumps(self.payload).encode("utf-8")

        error = HTTPError("http://center/api/navigator/access", 503, "unavailable", {}, None)
        with patch.object(center_auth, "urlopen", side_effect=[error]):
            with self.assertRaisesRegex(center_auth.CenterAuthError, "Navigator 授权查询暂不可用"):
                center_auth.validate("http://center", "token")

    def test_validate_uses_navigator_access_as_the_identity_source(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                import json
                return json.dumps({"ok": True, "data": {
                    "userId": "u1", "email": "admin@yqn.com", "displayName": "admin", "role": "admin"
                }}).encode("utf-8")

        with patch.object(center_auth, "urlopen", return_value=Response()):
            result = center_auth.validate("http://center", "browser-token")
        self.assertEqual(result["identity"]["email"], "admin@yqn.com")
        self.assertEqual(result["access"]["role"], "admin")

    def test_register_requests_navigator_sales_access_without_role_input(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                import json
                return json.dumps({"ok": True, "data": {"email": "new@example.com"}}).encode("utf-8")

        with patch.object(center_auth, "urlopen", return_value=Response()) as request:
            result = center_auth.register("http://center", "new@example.com", "Abc123")
        self.assertEqual(result["email"], "new@example.com")
        sent = request.call_args.args[0]
        import json
        self.assertEqual(json.loads(sent.data), {
            "email": "new@example.com", "password": "Abc123", "product": "navigator",
        })

    def test_password_change_targets_center_desktop_credential_endpoint(self):
        seen = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"ok":true,"data":{}}'
        def open_request(request, timeout):
            seen['url'] = request.full_url
            seen['authorization'] = request.headers.get('Authorization')
            return Response()
        with patch.object(center_auth, 'urlopen', side_effect=open_request):
            result = center_auth.change_password('http://center', 'token', 'CurrentPass1', 'NewPassword2')
        self.assertEqual(result, {})
        self.assertEqual(seen['url'], 'http://center/api/auth/desktop/password')
        self.assertEqual(seen['authorization'], 'Bearer token')


if __name__ == "__main__":
    unittest.main()
