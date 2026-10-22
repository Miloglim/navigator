import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.watchdog import parse_listener_pids, runtime_backend, runtime_config, start


class WatchdogTests(unittest.TestCase):
    def test_runtime_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "runtime.json"
            config.write_text(json.dumps({"backend": "postgres"}), encoding="utf-8")
            with patch("ops.watchdog.RUNTIME_CONFIG", str(config)):
                self.assertEqual(runtime_backend(), "postgres")
                config.write_text(json.dumps({"backend": "unknown"}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    runtime_backend()
    def test_tls_health_requires_local_matching_protocol_and_certificate_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "runtime.json"
            with patch("ops.watchdog.RUNTIME_CONFIG", str(config)), patch.dict("os.environ", {}, clear=True):
                config.write_text(json.dumps({"backend":"postgres", "tls_cert_file":"cert.pem"}), encoding="utf8")
                with self.assertRaises(ValueError):
                    runtime_config()
                settings={"backend":"postgres", "tls_cert_file":"cert.pem", "tls_key_file":"key.pem"}
                config.write_text(json.dumps(settings), encoding="utf8")
                self.assertEqual(runtime_config()["health_base_url"], "https://localhost:8795")
                for url in ("https://outside.invalid:8795", "http://localhost:8795", "https://localhost:8796"):
                    config.write_text(json.dumps({**settings, "health_base_url":url}), encoding="utf8")
                    with self.assertRaises(ValueError):
                        runtime_config()

    def test_parse_listener_pids_matches_exact_listening_port(self):
        output = """\
  TCP    0.0.0.0:8795           0.0.0.0:0              LISTENING       34492
  TCP    127.0.0.1:87950        0.0.0.0:0              LISTENING       11111
  TCP    127.0.0.1:8795         127.0.0.1:55000        ESTABLISHED     34492
"""
        self.assertEqual(parse_listener_pids(output, 8795), [34492])

    def test_parse_listener_pids_allows_ipv6(self):
        output = "  TCP    [::]:8795              [::]:0                 LISTENING       34492\n"
        self.assertEqual(parse_listener_pids(output, 8795), [34492])

    def test_start_passes_trusted_proxy_flag_to_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "runtime.json"
            config.write_text(json.dumps({
                "backend": "postgres",
                "center_auth_url": "https://center.example",
                "trusted_proxy_https": True,
            }), encoding="utf-8")
            with patch("ops.watchdog.RUNTIME_CONFIG", str(config)), \
                    patch("ops.watchdog.subprocess.run") as run, \
                    patch("ops.watchdog.time.sleep"), \
                    patch("ops.watchdog.http_ok", return_value=True), \
                    patch.dict("os.environ", {"EXISTING": "kept"}, clear=True):
                start()
            self.assertEqual(run.call_args.kwargs["env"]["NAVIGATOR_TRUSTED_PROXY_HTTPS"], "1")
            self.assertEqual(run.call_args.kwargs["env"]["EXISTING"], "kept")


if __name__ == "__main__":
    unittest.main()
