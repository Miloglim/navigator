"""Refresh the KB test token without exposing either credential to the UI."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.company_credentials_contracts import (
    COMPANY_TOKEN_REFRESH_STORE_FILE, KB_AUTH_REFRESH_URL, KB_TEST_TOKEN_URL,
    KB_TOKEN_LIFETIME_SECONDS, KB_REFRESH_EARLY_SECONDS, KB_REFRESH_CHECK_SECONDS,
    KB_HTTP_TIMEOUT_SECONDS,
)
from ops.company_credentials import default_credential_path, protect, unprotect
from ops.kb_browser_login import capture_refresh_token


def _utc_now():
    return datetime.now(timezone.utc)


def _as_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _post_json(url, payload, bearer=None):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    request = Request(url, data=json.dumps(payload).encode("utf-8") if payload else b"",
                      headers=headers, method="POST")
    try:
        with urlopen(request, timeout=KB_HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise RuntimeError("知识库登录已失效，请重新连接") from None
        raise RuntimeError(f"知识库接口返回 HTTP {error.code}") from None
    except (URLError, TimeoutError):
        raise RuntimeError("暂时无法连接知识库") from None


def _test_token(response):
    data = response.get("data", response) if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("知识库返回格式不符合预期")
    token = data.get("token") or data.get("test_token")
    if not isinstance(token, str) or not token.startswith("kbtt_"):
        raise RuntimeError("知识库未返回有效测试令牌")
    return token, _as_time(data.get("expires_at"))


class CompanyTokenRefresher:
    def __init__(self, credentials, path=None, protect_data=protect, unprotect_data=unprotect,
                 now=_utc_now, post=_post_json, logger=None, browser_login=capture_refresh_token):
        self.credentials = credentials
        self.path = Path(path) if path else default_credential_path().with_name(COMPANY_TOKEN_REFRESH_STORE_FILE)
        self.protect_data = protect_data
        self.unprotect_data = unprotect_data
        self.now = now
        self.post = post
        self.logger = logger or (lambda level, message: None)
        self.browser_login = browser_login
        self.lock = Lock()
        self.connection_state = "idle"
        self.connection_error = None
        self.connection_generation = 0

    def _load(self):
        if not self.path.exists():
            return None
        return json.loads(self.unprotect_data(self.path.read_bytes()).decode("utf-8"))

    def _save(self, state):
        encrypted = self.protect_data(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)

    def status(self, owner_user_id):
        owner = self.credentials.status(owner_user_id)
        if not owner["can_manage"]:
            return {**owner, "auto_refresh": False, "last_success_at": None,
                    "expires_at": None, "last_error": None, "last_attempt_at": None,
                    "expiry_estimated": None, "can_resume": False,
                    "connection_state": "idle", "connection_error": None}
        state = self._load()
        has_login = bool(state and state.get("refresh_token"))
        enabled = bool(has_login and state.get("enabled", True))
        return {**owner, "auto_refresh": enabled,
                "can_resume": has_login and not enabled,
                "last_success_at": state.get("last_success_at") if has_login else None,
                "expires_at": state.get("expires_at") if has_login else None,
                "expiry_estimated": state.get("expiry_estimated", True) if has_login else None,
                "last_error": state.get("last_error") if has_login else None,
                "last_attempt_at": state.get("last_attempt_at") if has_login else None,
                "connection_state": self.connection_state,
                "connection_error": self.connection_error}

    def configure(self, refresh_token, owner_user_id, generation=None):
        with self.lock:
            if generation is not None and generation != self.connection_generation:
                raise RuntimeError("知识库连接已取消")
            owner = self.credentials.status(owner_user_id)
            if not owner["can_manage"]:
                raise PermissionError("公司接口凭据已绑定其他管理员")
            if not owner["configured"]:
                raise ValueError("请先配置公司业务用户 ID 和测试令牌")
            refresh_token = str(refresh_token or "").strip()
            if not refresh_token or len(refresh_token) > 8192:
                raise ValueError("知识库登录续期凭据不能为空或过长")
            self._save({"refresh_token": refresh_token, "owner_user_id": str(owner_user_id),
                        "enabled": True,
                        "last_success_at": None, "expires_at": None,
                        "expiry_estimated": None, "last_attempt_at": None, "last_error": None})
        return self.status(owner_user_id)

    def disable(self, owner_user_id):
        with self.lock:
            if not self.credentials.status(owner_user_id)["can_manage"]:
                raise PermissionError("公司接口凭据已绑定其他管理员")
            state = self._load()
            if state:
                state["enabled"] = False
                self._save(state)
            self.connection_generation += 1
            self.connection_state = "idle"
            self.connection_error = None
        return self.status(owner_user_id)

    def enable(self, owner_user_id):
        with self.lock:
            if not self.credentials.status(owner_user_id)["can_manage"]:
                raise PermissionError("公司接口凭据已绑定其他管理员")
            state = self._load()
            if not state or not state.get("refresh_token"):
                raise ValueError("请先连接知识库登录")
            state["enabled"] = True
            self._save(state)
        return self.refresh(owner_user_id)

    def connect_browser(self, owner_user_id):
        with self.lock:
            owner = self.credentials.status(owner_user_id)
            if not owner["can_manage"]:
                raise PermissionError("公司接口凭据已绑定其他管理员")
            if not owner["configured"]:
                raise ValueError("请先配置公司业务用户 ID 和测试令牌")
            if self.connection_state == "connecting":
                return self.status(owner_user_id)
            self.connection_generation += 1
            generation = self.connection_generation
            self.connection_state = "connecting"
            self.connection_error = None

        def worker():
            try:
                token = self.browser_login(cancelled=lambda: generation != self.connection_generation)
                self.configure(token, owner_user_id, generation=generation)
                result = self.refresh(owner_user_id, force=True)
                if result["last_error"]:
                    raise RuntimeError(result["last_error"])
                if generation == self.connection_generation:
                    self.connection_state = "connected"
            except Exception as error:
                if generation == self.connection_generation:
                    self.connection_state = "error"
                    self.connection_error = str(error) if isinstance(error, RuntimeError) else "知识库连接失败，请重试"
                    self.logger("WARN", "公司查价知识库连接失败：" + self.connection_error)

        Thread(target=worker, daemon=True, name="company-kb-login").start()
        return self.status(owner_user_id)

    def refresh(self, owner_user_id=None, force=False):
        with self.lock:
            state = self._load()
            if not state or not state.get("refresh_token"):
                if force:
                    raise ValueError("请先连接知识库登录")
                return None
            owner_id = state["owner_user_id"]
            if not force and not state.get("enabled", True):
                return self.status(owner_id)
            if owner_user_id is not None and str(owner_user_id) != owner_id:
                raise PermissionError("公司接口凭据已绑定其他管理员")
            if not self.credentials.status(owner_id)["can_manage"]:
                raise PermissionError("公司接口凭据已绑定其他管理员")
            now = self.now()
            expires = _as_time(state.get("expires_at"))
            if not force and expires and expires - now > timedelta(seconds=KB_REFRESH_EARLY_SECONDS):
                return self.status(owner_id)
            state["last_attempt_at"] = now.isoformat()
            try:
                login = self.post(KB_AUTH_REFRESH_URL, {"refresh_token": state["refresh_token"]})
                access = login.get("access_token") if isinstance(login, dict) else None
                if not isinstance(access, str) or not access:
                    raise RuntimeError("知识库登录续期失败")
                if login.get("refresh_token"):
                    state["refresh_token"] = login["refresh_token"]
                    self._save(state)
                response = self.post(KB_TEST_TOKEN_URL, {}, bearer=access)
                token, expiry = _test_token(response)
                current = self.credentials.credentials()
                if not current:
                    raise RuntimeError("Navigator 公司查价凭据未配置")
                self.credentials.save({"token": token, "user_id": current["user_id"]}, owner_id)
                state.update(last_success_at=now.isoformat(),
                             expires_at=(expiry or now + timedelta(seconds=KB_TOKEN_LIFETIME_SECONDS)).isoformat(),
                             expiry_estimated=expiry is None,
                             last_error=None)
                self.logger("INFO", "公司查价测试令牌刷新成功")
            except Exception as error:
                state["last_error"] = str(error) if isinstance(error, RuntimeError) else "刷新失败，请检查知识库连接"
                self.logger("WARN", "公司查价测试令牌刷新失败：" + state["last_error"])
            self._save(state)
            return self.status(owner_id)

    def run_periodically(self):
        from threading import Event
        def worker():
            while True:
                try:
                    self.refresh()
                except Exception as error:
                    self.logger("WARN", f"公司查价自动刷新检查失败：{type(error).__name__}")
                Event().wait(KB_REFRESH_CHECK_SECONDS)
        Thread(target=worker, daemon=True, name="company-token-refresh").start()
