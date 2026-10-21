import unittest

from services.access_policy import secure_transport


class ProxyTransportTests(unittest.TestCase):
    def test_forwarded_https_is_trusted_only_for_private_peers_when_enabled(self):
        self.assertTrue(secure_transport(False, "172.18.0.4", "https", True))
        self.assertFalse(secure_transport(False, "47.96.159.26", "https", True))
        self.assertFalse(secure_transport(False, "172.18.0.4", "https", False))
        self.assertFalse(secure_transport(False, "172.18.0.4", "http", True))

    def test_native_tls_remains_authoritative(self):
        self.assertTrue(secure_transport(True, "172.18.0.4", "", False))


if __name__ == "__main__":
    unittest.main()
