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


if __name__ == "__main__":
    unittest.main()
