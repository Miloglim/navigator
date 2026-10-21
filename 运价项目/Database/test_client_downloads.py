import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.download_contracts import (
    DOWNLOAD_FILE_PREFIX,
    DOWNLOAD_MANIFEST_ROUTE,
    DOWNLOAD_PAGE_ROUTE,
)
from services.download_catalog import DownloadCatalog, DownloadManifestInvalid, DownloadNotFound
from transport.access_rules import authorize
from transport.download_routes import build_download_routes, resolve_download_asset


class ClientDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "download.html").write_text("<html>download</html>", encoding="utf-8")
        (self.web / "download.js").write_text("void 0;", encoding="utf-8")
        (self.web / "download.css").write_text("body{}", encoding="utf-8")
        self.installer = self.root / "prospecting-email-setup-0.54.0.exe"
        self.installer.write_bytes(b"installer")
        self.manifest = {
            "manifestVersion": 1,
            "product": "prospector",
            "channel": "stable",
            "version": "0.54.0",
            "fileName": self.installer.name,
            "sizeBytes": self.installer.stat().st_size,
            "sha256": hashlib.sha256(self.installer.read_bytes()).hexdigest(),
            "publishedAt": "2026-10-21T10:00:00+08:00",
            "minimumWindows": "Windows 10 64-bit",
            "signature": "unsigned",
            "notes": ["客户详情同步修复"],
        }
        self.write_manifest()

    def tearDown(self):
        self.directory.cleanup()

    def write_manifest(self):
        (self.root / "latest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")

    def test_manifest_is_public_and_contains_a_same_origin_download_url(self):
        response = build_download_routes(DownloadCatalog(self.root))[DOWNLOAD_MANIFEST_ROUTE]({})

        self.assertTrue(response["ok"])
        self.assertEqual("0.54.0", response["data"]["version"])
        self.assertEqual(DOWNLOAD_FILE_PREFIX + self.installer.name, response["data"]["downloadUrl"])
        self.assertTrue(authorize("GET", DOWNLOAD_MANIFEST_ROUTE)["ok"])

    def test_page_assets_and_current_installer_resolve_without_directory_traversal(self):
        catalog = DownloadCatalog(self.root)

        page, page_type, _ = resolve_download_asset(catalog, DOWNLOAD_PAGE_ROUTE, self.web)
        artifact, artifact_type, headers = resolve_download_asset(
            catalog, DOWNLOAD_FILE_PREFIX + self.installer.name, self.web)

        self.assertEqual(self.web / "download.html", page)
        self.assertIn("text/html", page_type)
        self.assertEqual(self.installer, artifact)
        self.assertIn("portable-executable", artifact_type)
        self.assertIn("attachment", headers["Content-Disposition"])
        with self.assertRaises(DownloadNotFound):
            resolve_download_asset(catalog, DOWNLOAD_FILE_PREFIX + "..%2Flatest.json", self.web)

    def test_manifest_rejects_size_mismatch(self):
        self.manifest["sizeBytes"] += 1
        self.write_manifest()

        with self.assertRaises(DownloadManifestInvalid):
            DownloadCatalog(self.root).manifest()

    def test_installer_must_match_current_manifest(self):
        other = self.root / "prospecting-email-setup-0.53.0.exe"
        other.write_bytes(b"old")

        with self.assertRaises(DownloadNotFound):
            DownloadCatalog(self.root).installer(other.name)


if __name__ == "__main__":
    unittest.main()

