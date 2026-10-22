"""Cross-platform encrypted storage for the rotating company gateway token."""

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from threading import Lock

from core.company_credentials_contracts import COMPANY_CREDENTIAL_STORE_DIR, COMPANY_CREDENTIAL_STORE_FILE

LINUX_FILE_MAGIC = b"MLGC1"
LINUX_KEY_BYTES = 64
LINUX_HMAC_BYTES = 32
OPENSSL_PBKDF2_ITERATIONS = 200_000
DEFAULT_CREDENTIAL_KEY_FILE = "company-credential-master.key"
LINUX_KEY_LOCK = Lock()


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


def _linux_key_path():
    return default_credential_path().with_name(DEFAULT_CREDENTIAL_KEY_FILE)


def _linux_key(path=None):
    key_path = Path(path) if path else _linux_key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(key_path.parent, 0o700)
    with LINUX_KEY_LOCK:
        try:
            key = key_path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(LINUX_KEY_BYTES)
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(key)
            os.chmod(key_path, 0o600)
    if len(key) != LINUX_KEY_BYTES:
        raise RuntimeError("公司接口凭据主密钥无效")
    return key


def _openssl_cipher(data, key_path, decrypt=False):
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("Linux 公司接口凭据加密需要 OpenSSL")
    command = [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(OPENSSL_PBKDF2_ITERATIONS),
               "-salt", "-pass", f"file:{key_path}"]
    if decrypt:
        command.append("-d")
    result = subprocess.run(command, input=data, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("公司接口凭据解密失败" if decrypt else "公司接口凭据加密失败")
    return result.stdout


def _linux_protect(data, key_path=None):
    path = Path(key_path) if key_path else _linux_key_path()
    key = _linux_key(path)
    ciphertext = _openssl_cipher(data, path)
    mac = hmac.new(key[LINUX_HMAC_BYTES:], LINUX_FILE_MAGIC + ciphertext, hashlib.sha256).digest()
    return LINUX_FILE_MAGIC + mac + ciphertext


def _linux_unprotect(data, key_path=None):
    if not data.startswith(LINUX_FILE_MAGIC) or len(data) <= len(LINUX_FILE_MAGIC) + LINUX_HMAC_BYTES:
        raise RuntimeError("公司接口凭据文件格式无效")
    path = Path(key_path) if key_path else _linux_key_path()
    key = _linux_key(path)
    offset = len(LINUX_FILE_MAGIC)
    expected = data[offset:offset + LINUX_HMAC_BYTES]
    ciphertext = data[offset + LINUX_HMAC_BYTES:]
    actual = hmac.new(key[LINUX_HMAC_BYTES:], LINUX_FILE_MAGIC + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("公司接口凭据完整性校验失败")
    return _openssl_cipher(ciphertext, path, decrypt=True)


def protect(data):
    return _dpapi("CryptProtectData", data) if os.name == "nt" else _linux_protect(data)


def unprotect(data):
    return _dpapi("CryptUnprotectData", data) if os.name == "nt" else _linux_unprotect(data)


def default_credential_path():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        root = Path(__file__).resolve().parents[1] / ".runtime"
    return root / COMPANY_CREDENTIAL_STORE_DIR / COMPANY_CREDENTIAL_STORE_FILE


class CompanyCredentialStore:
    def __init__(self, path=None, protect_data=None, unprotect_data=None, now=None, key_path=None):
        self.path = Path(path) if path else default_credential_path()
        self.key_path = Path(key_path) if key_path else self.path.with_name(DEFAULT_CREDENTIAL_KEY_FILE)
        self.protect_data = protect_data or ((lambda value: _dpapi("CryptProtectData", value))
            if os.name == "nt" else (lambda value: _linux_protect(value, self.key_path)))
        self.unprotect_data = unprotect_data or ((lambda value: _dpapi("CryptUnprotectData", value))
            if os.name == "nt" else (lambda value: _linux_unprotect(value, self.key_path)))
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
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)
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
