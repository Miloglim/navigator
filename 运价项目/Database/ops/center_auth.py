"""Minimal standard-library client for the central identity API."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class CenterAuthError(PermissionError):
    """Central authentication failed or was unavailable."""


def _error_code(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        return str((payload.get("error") or {}).get("code") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str = "",
    operation: str = "中心身份服务",
    auth_message: str = "中心账号或 Navigator 权限无效",
) -> dict:
    url = base_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=8) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in (401, 403):
            raise CenterAuthError(auth_message) from error
        if error.code == 429:
            raise CenterAuthError("中心登录尝试过多，请稍后重试") from error
        if error.code == 400:
            raise CenterAuthError(f"{operation}参数无效") from error
        code = _error_code(error)
        suffix = f"，{code}" if code else ""
        raise CenterAuthError(f"{operation}暂不可用（HTTP {error.code}{suffix}）") from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise CenterAuthError(f"{operation}暂不可用") from error
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        raise CenterAuthError("中心账号或 Navigator 权限无效")
    return raw.get("data") or {}


def login(base_url: str, email: str, password: str) -> dict:
    data = _request(base_url, "POST", "/api/auth/login", {"email": email, "password": password},
                    operation="中心账号登录")
    token = data.get("token")
    identity = data.get("identity")
    if not isinstance(token, str) or not token or not isinstance(identity, dict):
        raise CenterAuthError("中心登录响应无效")
    return {"token": token, "identity": identity}


def login_persistent(base_url: str, email: str, password: str) -> dict:
    """Create a refreshable server-side session for long-lived product access."""
    data = _request(
        base_url,
        "POST",
        "/api/auth/desktop/login",
        {"email": email, "password": password},
        operation="中心账号登录",
    )
    token = data.get("token")
    refresh_token = data.get("refreshToken")
    identity = data.get("identity")
    if not isinstance(token, str) or not token or not isinstance(refresh_token, str) or not refresh_token or not isinstance(identity, dict):
        raise CenterAuthError("中心登录响应无效")
    return {"token": token, "refresh_token": refresh_token, "identity": identity}


def refresh(base_url: str, refresh_token: str) -> dict:
    data = _request(
        base_url,
        "POST",
        "/api/auth/desktop/refresh",
        {"refreshToken": refresh_token},
        operation="中心会话续期",
    )
    token = data.get("token")
    next_refresh = data.get("refreshToken")
    identity = data.get("identity")
    if not isinstance(token, str) or not token or not isinstance(next_refresh, str) or not next_refresh or not isinstance(identity, dict):
        raise CenterAuthError("中心续期响应无效")
    return {"token": token, "refresh_token": next_refresh, "identity": identity}


def logout_refresh(base_url: str, refresh_token: str) -> None:
    if not refresh_token:
        return
    try:
        _request(
            base_url,
            "POST",
            "/api/auth/desktop/logout",
            {"refreshToken": refresh_token},
            operation="中心会话退出",
        )
    except CenterAuthError:
        pass  # Local logout must still remove the browser session when the center is unavailable.


def register(base_url: str, email: str, password: str) -> dict:
    data = _request(
        base_url,
        "POST",
        "/api/auth/desktop/register",
        {"email": email, "password": password, "product": "navigator"},
        operation="中心账号注册",
    )
    if not isinstance(data, dict) or str(data.get("email") or "").lower() != email.lower():
        raise CenterAuthError("中心账号注册响应无效")
    return data


def validate(base_url: str, token: str) -> dict:
    access = _request(base_url, "GET", "/api/navigator/access", token=token,
                      operation="Navigator 授权查询")
    if not isinstance(access, dict):
        raise CenterAuthError("中心身份响应无效")
    identity = {key: access.get(key) for key in ("userId", "email", "displayName") if access.get(key)}
    return {"identity": identity, "access": access}


def account_profile(base_url: str, token: str) -> dict:
    profile = _request(base_url, "GET", "/api/account/profile", token=token,
                       operation="中心账号资料查询")
    if not isinstance(profile, dict) or not profile.get("userId"):
        raise CenterAuthError("中心账号资料响应无效")
    return profile


def update_account_profile(base_url: str, token: str, display_name: str, avatar_id: str | None) -> dict:
    payload = {"displayName": display_name}
    if avatar_id:
        payload["avatarId"] = avatar_id
    return _request(base_url, "POST", "/api/account/profile", payload, token=token,
                    operation="中心账号资料保存")


def record_growth_event(base_url: str, token: str, event: dict) -> dict:
    return _request(base_url, "POST", "/api/account/growth", event, token=token,
                    operation="中心成长记账")


def change_password(base_url: str, token: str, current_password: str, new_password: str) -> dict:
    return _request(base_url, "POST", "/api/account/password",
                    {"currentPassword": current_password, "newPassword": new_password},
                    token=token, operation="中心账号改密",
                    auth_message="当前密码不正确或中心会话已失效")


def logout(base_url: str, token: str) -> None:
    try:
        _request(base_url, "POST", "/api/auth/logout", token=token)
    except CenterAuthError:
        pass
