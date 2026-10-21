"""Public contracts for distributing the Prospector desktop installer."""
from typing import Final, TypedDict


DOWNLOAD_PAGE_ROUTE: Final = "/download"
DOWNLOAD_SCRIPT_ROUTE: Final = "/assets/download.js"
DOWNLOAD_STYLE_ROUTE: Final = "/assets/download.css"
DOWNLOAD_MANIFEST_ROUTE: Final = "/api/downloads/prospector/latest"
DOWNLOAD_FILE_PREFIX: Final = "/downloads/prospector/"
DOWNLOAD_PRODUCT_ID: Final = "prospector"
DOWNLOAD_STABLE_CHANNEL: Final = "stable"
DOWNLOAD_MANIFEST_FILE: Final = "latest.json"
DOWNLOAD_MANIFEST_VERSION: Final = 1
DOWNLOAD_NOTES_MAX: Final = 20
DOWNLOAD_NOTE_MAX_LENGTH: Final = 500


class DownloadManifest(TypedDict):
    manifestVersion: int
    product: str
    channel: str
    version: str
    fileName: str
    sizeBytes: int
    sha256: str
    publishedAt: str
    minimumWindows: str
    signature: str
    notes: list[str]

