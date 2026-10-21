"""Validated, read-only catalog for desktop installer downloads."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.download_contracts import (
    DOWNLOAD_MANIFEST_FILE,
    DOWNLOAD_MANIFEST_VERSION,
    DOWNLOAD_NOTE_MAX_LENGTH,
    DOWNLOAD_NOTES_MAX,
    DOWNLOAD_PRODUCT_ID,
    DOWNLOAD_STABLE_CHANNEL,
    DownloadManifest,
)


_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.exe$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")


class DownloadUnavailable(RuntimeError):
    """The stable channel has no published installer."""


class DownloadManifestInvalid(RuntimeError):
    """The stable manifest is malformed or points at an unsafe artifact."""


class DownloadNotFound(RuntimeError):
    """The requested artifact is not the currently published installer."""


def _text(value: Any, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise DownloadManifestInvalid(f"{field} 无效")
    return value.strip()


def validate_manifest(value: Any) -> DownloadManifest:
    if not isinstance(value, dict):
        raise DownloadManifestInvalid("下载清单必须是对象")
    if value.get("manifestVersion") != DOWNLOAD_MANIFEST_VERSION:
        raise DownloadManifestInvalid("下载清单版本无效")
    product = _text(value.get("product"), "product", 32)
    channel = _text(value.get("channel"), "channel", 32)
    if product != DOWNLOAD_PRODUCT_ID or channel != DOWNLOAD_STABLE_CHANNEL:
        raise DownloadManifestInvalid("下载产品渠道无效")
    version = _text(value.get("version"), "version", 64)
    if not _VERSION.fullmatch(version):
        raise DownloadManifestInvalid("版本号无效")
    file_name = _text(value.get("fileName"), "fileName", 204)
    if not _FILE_NAME.fullmatch(file_name):
        raise DownloadManifestInvalid("安装包文件名无效")
    size = value.get("sizeBytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise DownloadManifestInvalid("安装包大小无效")
    sha256 = _text(value.get("sha256"), "sha256", 64).lower()
    if not _SHA256.fullmatch(sha256):
        raise DownloadManifestInvalid("SHA256 无效")
    published_at = _text(value.get("publishedAt"), "publishedAt", 64)
    minimum_windows = _text(value.get("minimumWindows"), "minimumWindows", 64)
    signature = _text(value.get("signature"), "signature", 16)
    if signature not in {"signed", "unsigned"}:
        raise DownloadManifestInvalid("签名状态无效")
    raw_notes = value.get("notes", [])
    if not isinstance(raw_notes, list) or len(raw_notes) > DOWNLOAD_NOTES_MAX:
        raise DownloadManifestInvalid("更新说明无效")
    notes = [item.strip() for item in raw_notes if isinstance(item, str) and item.strip()]
    if len(notes) != len(raw_notes) or any(len(item) > DOWNLOAD_NOTE_MAX_LENGTH for item in notes):
        raise DownloadManifestInvalid("更新说明无效")
    return {
        "manifestVersion": DOWNLOAD_MANIFEST_VERSION,
        "product": product,
        "channel": channel,
        "version": version,
        "fileName": file_name,
        "sizeBytes": size,
        "sha256": sha256,
        "publishedAt": published_at,
        "minimumWindows": minimum_windows,
        "signature": signature,
        "notes": notes,
    }


class DownloadCatalog:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def _artifact(self, file_name: str) -> Path:
        if not _FILE_NAME.fullmatch(file_name):
            raise DownloadNotFound("安装包不存在")
        candidate = (self.root / file_name).resolve()
        if candidate.parent != self.root:
            raise DownloadNotFound("安装包不存在")
        return candidate

    def manifest(self) -> DownloadManifest:
        path = self.root / DOWNLOAD_MANIFEST_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise DownloadUnavailable("客户端安装包尚未发布") from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DownloadManifestInvalid("客户端下载清单不可读") from error
        manifest = validate_manifest(raw)
        artifact = self._artifact(manifest["fileName"])
        if not artifact.is_file() or artifact.stat().st_size != manifest["sizeBytes"]:
            raise DownloadManifestInvalid("安装包与下载清单不一致")
        return manifest

    def installer(self, file_name: str) -> Path:
        manifest = self.manifest()
        if file_name != manifest["fileName"]:
            raise DownloadNotFound("安装包不存在")
        return self._artifact(file_name)

