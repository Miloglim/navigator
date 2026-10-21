"""Smoke the standard-library browser login handoff against a local fixture."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from ops.kb_browser_login import capture_refresh_token


class LoginFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<script>localStorage.setItem('kb_refresh_token','fixture-secret')</script>Ready"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return  # This local fixture has no useful request diagnostics.


class BrowserLoginTests(unittest.TestCase):
    def test_reads_only_dedicated_browser_origin(self):
        browsers = [path for path in (
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        ) if path.is_file()]
        if not browsers:
            self.skipTest("Edge and Chrome unavailable")
        server = HTTPServer(("127.0.0.1", 0), LoginFixture)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for browser in browsers:
                with self.subTest(browser=browser.name), tempfile.TemporaryDirectory() as directory:
                    token = capture_refresh_token(browser_path=browser, profile_path=Path(directory) / "profile",
                        login_url=f"http://127.0.0.1:{server.server_port}/", browser_flags=("--headless=new",))
                    self.assertEqual(token, "fixture-secret")
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
