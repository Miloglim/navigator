import tempfile
import unittest
import os
from pathlib import Path

from ops.company_credentials import CompanyCredentialStore
from services.company_credentials import require_local_admin, validate_credential_update
from transport.company_credential_routes import build_company_credential_routes


class CompanyCredentialTests(unittest.TestCase):
    def test_local_admin_only_and_strict_values(self):
        require_local_admin("admin", "127.0.0.1")
        with self.assertRaises(PermissionError):
            require_local_admin("sales", "127.0.0.1")
        with self.assertRaises(PermissionError):
            require_local_admin("admin", "192.168.1.5")
        self.assertEqual(validate_credential_update({"token": "abc", "user_id": "123"}),
                         {"token": "abc", "user_id": 123})
        with self.assertRaises(ValueError):
            validate_credential_update({"token": "abc", "user_id": "sample"})

    def test_store_never_writes_or_returns_plaintext_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.bin"
            store = CompanyCredentialStore(path, lambda value: b"sealed:" + value[::-1],
                                           lambda value: value.removeprefix(b"sealed:")[::-1],
                                           now=lambda: "2026-10-14T00:00:00+00:00")
            status = store.save({"token": "private-token", "user_id": 456}, "usr-owner")
            self.assertNotIn(b"private-token", path.read_bytes())
            self.assertNotIn("token", status)
            self.assertEqual(store.credentials(), {"token": "private-token", "user_id": 456})

    def test_routes_never_echo_token(self):
        class Store:
            def save(self, credentials, owner_user_id):
                self.credentials = credentials
                self.owner_user_id = owner_user_id
                return {"configured": True, "user_id": credentials["user_id"], "updated_at": "now", "can_manage": True}
            def status(self, requester_user_id):
                return {"configured": True, "user_id": 99, "updated_at": "now", "can_manage": requester_user_id == "usr-owner"}
        store = Store()
        get_routes, post_routes = build_company_credential_routes(store)
        query = {"_role": "admin", "_client_ip": "::1", "_actor_id": "usr-owner"}
        self.assertNotIn("token", get_routes["/api/admin/company-credentials"](query)["data"])
        result = post_routes["/api/admin/company-credentials"]({**query, "token": "secret", "user_id": "99"})
        self.assertNotIn("token", result["data"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_windows_dpapi_round_trip_keeps_plaintext_off_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.bin"
            store = CompanyCredentialStore(path)
            store.save({"token": "dpapi-private-token", "user_id": 789}, "usr-owner")
            self.assertNotIn(b"dpapi-private-token", path.read_bytes())
            self.assertEqual(store.credentials(), {"token": "dpapi-private-token", "user_id": 789})

    @unittest.skipUnless(os.name != "nt", "Linux OpenSSL storage only")
    def test_linux_openssl_round_trip_uses_protected_key_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.bin"
            key_path = Path(directory) / "master.key"
            store = CompanyCredentialStore(path, key_path=key_path)
            store.save({"token": "linux-private-token", "user_id": 790}, "usr-owner")
            self.assertNotIn(b"linux-private-token", path.read_bytes())
            self.assertEqual(store.credentials(), {"token": "linux-private-token", "user_id": 790})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
            damaged = bytearray(path.read_bytes())
            damaged[-1] ^= 1
            path.write_bytes(damaged)
            with self.assertRaisesRegex(RuntimeError, "完整性校验失败"):
                store.credentials()

    def test_first_admin_becomes_credential_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.bin"
            store = CompanyCredentialStore(path, lambda value: b"sealed:" + value[::-1],
                                           lambda value: value.removeprefix(b"sealed:")[::-1])
            store.save({"token": "first", "user_id": 123}, "usr-owner")
            self.assertTrue(store.status("usr-owner")["can_manage"])
            self.assertFalse(store.status("usr-other-admin")["can_manage"])
            with self.assertRaises(PermissionError):
                store.save({"token": "second", "user_id": 123}, "usr-other-admin")


if __name__ == "__main__":
    unittest.main()
