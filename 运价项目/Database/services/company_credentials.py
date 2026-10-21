"""Pure validation for company gateway credential updates."""

from core.company_credentials_contracts import (
    COMPANY_CREDENTIAL_TOKEN_MAX_LENGTH,
    COMPANY_CREDENTIAL_USER_ID_MAX_LENGTH,
    LOOPBACK_ADDRESSES,
)


def require_local_admin(role, client_ip):
    if role != "admin":
        raise PermissionError("仅管理员可维护公司接口凭据")
    if str(client_ip or "") not in LOOPBACK_ADDRESSES:
        raise PermissionError("公司接口凭据只能在服务器本机维护")


def validate_credential_update(body):
    if not isinstance(body, dict):
        raise ValueError("凭据请求格式不正确")
    token = str(body.get("token") or "").strip()
    user_id = str(body.get("user_id") or "").strip()
    if not token or len(token) > COMPANY_CREDENTIAL_TOKEN_MAX_LENGTH:
        raise ValueError("Token不能为空或过长")
    if not user_id or len(user_id) > COMPANY_CREDENTIAL_USER_ID_MAX_LENGTH or not user_id.isdigit():
        raise ValueError("业务用户ID须为数字")
    return {"token": token, "user_id": int(user_id)}
