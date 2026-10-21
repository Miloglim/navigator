"""Public route adapters for the Prospector installer page and manifest."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import unquote

from core.contracts import fail, ok
from core.download_contracts import (
    DOWNLOAD_FILE_PREFIX,
    DOWNLOAD_MANIFEST_ROUTE,
    DOWNLOAD_PAGE_ROUTE,
    DOWNLOAD_SCRIPT_ROUTE,
    DOWNLOAD_STYLE_ROUTE,
)
from services.download_catalog import (
    DownloadCatalog,
    DownloadManifestInvalid,
    DownloadNotFound,
    DownloadUnavailable,
)


DownloadAsset = tuple[Path, str, Dict[str, str]]


def build_download_routes(catalog: DownloadCatalog) -> Dict[str, Callable[[Dict[str, str]], Dict[str, Any]]]:
    def manifest(_: Dict[str, str]) -> Dict[str, Any]:
        try:
            value = catalog.manifest()
        except DownloadUnavailable as error:
            return fail(str(error), "not_found")
        except DownloadManifestInvalid as error:
            return fail(str(error), "unavailable")
        return ok({
            **value,
            "downloadUrl": f"{DOWNLOAD_FILE_PREFIX}{value['fileName']}",
        })

    return {DOWNLOAD_MANIFEST_ROUTE: manifest}


def resolve_download_asset(
    catalog: DownloadCatalog,
    path: str,
    web_root: Path,
) -> DownloadAsset | None:
    web_root = web_root.resolve()
    if path in {DOWNLOAD_PAGE_ROUTE, f"{DOWNLOAD_PAGE_ROUTE}/"}:
        return web_root / "download.html", "text/html; charset=utf-8", {}
    if path == DOWNLOAD_SCRIPT_ROUTE:
        return web_root / "download.js", "application/javascript; charset=utf-8", {}
    if path == DOWNLOAD_STYLE_ROUTE:
        return web_root / "download.css", "text/css; charset=utf-8", {}
    if not path.startswith(DOWNLOAD_FILE_PREFIX):
        return None
    file_name = unquote(path[len(DOWNLOAD_FILE_PREFIX):])
    try:
        artifact = catalog.installer(file_name)
    except DownloadNotFound:
        raise DownloadNotFound("安装包不存在") from None
    except (DownloadUnavailable, DownloadManifestInvalid) as error:
        raise DownloadManifestInvalid(str(error)) from error
    headers = {
        "Content-Disposition": f'attachment; filename="{artifact.name}"',
        "Cache-Control": "public, max-age=31536000, immutable",
        "Accept-Ranges": "none",
    }
    return artifact, "application/vnd.microsoft.portable-executable", headers

