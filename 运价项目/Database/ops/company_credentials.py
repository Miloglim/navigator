"""Windows DPAPI-backed storage for the rotating company gateway token."""

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from core.company_credentials_contracts import COMPANY_CREDENTIAL_STORE_DIR, COMPANY_CREDENTIAL_STORE_FILE


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi(operation, data):
    if os.name != "nt":
        raise RuntimeError("公司接口凭据加密仅支持Windows")
    source, source_buffer = _blob(data)
    target = _DataBlob()
    function = getattr(ctypes.windll.crypt32, operation)
    if operation == "CryptProtectData":
        success = function(ctypes.byref(source), "Milogin Navigator", None, None, None, 0,
                           ctypes.byref(target))
    else:
        success = function(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
    del source_buffer
    if not success:
        raise OSError(ctypes.get_last_error(), "Windows凭据加密操作失败")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def protect(data):
    return _dpapi("CryptProtectData", data)


def unprotect(data):
    return _dpapi("CryptUnprotectData", data)


def default_credential_path():
    root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return root / COMPANY_CREDENTIAL_STORE_DIR / COMPANY_CREDENTIAL_STORE_FILE


class CompanyCredentialStore:
    def __init__(self, path=None, protect_data=protect, unprotect_data=unprotect, now=None):
        self.path = Path(path) if path else default_credential_path()
        self.protect_data = protect_data
        self.unprotect_data = unprotect_data
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())

    def load(self):
        if not self.path.exists():
            return None
        payload = json.loads(self.unprotect_data(self.path.read_bytes()).decode("utf-8"))
        token = str(payload.get("token") or "")
        user_id = int(payload.get("user_id") or 0)
        if not token or user_id <= 0:
            raise ValueError("已保存的公司接口凭据无效")
        return {"token": token, "user_id": user_id, "updated_at": payload.get("updated_at"),
                "owner_user_id": payload.get("owner_user_id")}

    def save(self, credentials, owner_user_id):
        owner_user_id = str(owner_user_id or "").strip()
        if not owner_user_id:
            raise PermissionError("无法确认凭据管理员账号")
        existing = self.load()
        existing_owner = str((existing or {}).get("owner_user_id") or "")
        if existing_owner and existing_owner != owner_user_id:
            raise PermissionError("公司查价凭据已绑定其他管理员账号")
        payload = {**credentials, "owner_user_id": existing_owner or owner_user_id,
                   "updated_at": self.now()}
        encrypted = self.protect_data(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)
        return self.status(owner_user_id, payload)

    def status(self, requester_user_id, payload=None):
        payload = self.load() if payload is None else payload
        owner_user_id = str((payload or {}).get("owner_user_id") or "")
        return {"configured": bool(payload), "user_id": payload.get("user_id") if payload else None,
                "updated_at": payload.get("updated_at") if payload else None,
                "can_manage": not owner_user_id or owner_user_id == str(requester_user_id or "")}

    def credentials(self):
        payload = self.load()
        return ({"token": payload["token"], "user_id": payload["user_id"]} if payload else None)
