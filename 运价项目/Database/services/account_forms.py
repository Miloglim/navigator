"""Pure validation for local account self service. No database or transport IO."""
from core.contracts import (ACCOUNT_NAME_MIN_LENGTH, ACCOUNT_NAME_MAX_LENGTH,
    ACCOUNT_PASSWORD_MIN_LENGTH, ACCOUNT_PASSWORD_MAX_LENGTH)


def account_name(value):
    name = str(value or "").strip()
    if not ACCOUNT_NAME_MIN_LENGTH <= len(name) <= ACCOUNT_NAME_MAX_LENGTH:
        raise ValueError("账号须为2至64个字符")
    if any(ord(char) < 32 for char in name):
        raise ValueError("账号不能包含控制字符")
    return name


def new_password(value, confirmation):
    password = str(value or "")
    if not ACCOUNT_PASSWORD_MIN_LENGTH <= len(password) <= ACCOUNT_PASSWORD_MAX_LENGTH or not password.strip():
        raise ValueError(f"密码须为{ACCOUNT_PASSWORD_MIN_LENGTH}至{ACCOUNT_PASSWORD_MAX_LENGTH}个字符")
    if password != str(confirmation or ""):
        raise ValueError("两次输入的密码不一致")
    return password


def registration(body):
    if set(body) - {"name", "password", "confirm_password"}:
        raise ValueError("自行创建账号只能开通销售身份，不能设置角色或权限")
    return account_name(body.get("name")), new_password(body.get("password"), body.get("confirm_password"))


def password_change(body):
    if set(body) - {"current_password", "new_password", "confirm_password"}:
        raise ValueError("只能修改当前登录账号的密码")
    current = str(body.get("current_password") or "")
    if not current:
        raise ValueError("请输入原密码")
    password = new_password(body.get("new_password"), body.get("confirm_password"))
    if current == password:
        raise ValueError("新密码不能与原密码相同")
    return current, password


def profile_update(body, available_routes, available_groups, existing_avatar_id=None, existing_preferences=None):
    from core.contracts import (ACCOUNT_PREFERENCE_MAX_ITEMS, ACCOUNT_CONTAINER_TYPES,
                                ACCOUNT_DISPLAY_NAME_MAX_LENGTH, ACCOUNT_GROWTH_LIQUID_PREF,
                                ACCOUNT_AVATAR_PREF, ACCOUNT_AVATAR_ALL_IDS,
                                ACCOUNT_COMPANY_RATES_PREF, ACCOUNT_CARRIER_REALTIME_PREF)
    if set(body) != {"display_name", "preferences"}:
        raise ValueError("只能修改用户名和个人查看偏好")
    display = body["display_name"]
    if not isinstance(display, str):
        raise ValueError("用户名须为文字")
    display = display.strip()
    if not display or len(display) > ACCOUNT_DISPLAY_NAME_MAX_LENGTH or any(ord(char) < 32 for char in display):
        raise ValueError("用户名须为1至64个字符，不能含控制字符")
    prefs = body["preferences"]
    required = {"routes", "groups", "ctypes", "start_tab"}
    optional = {ACCOUNT_GROWTH_LIQUID_PREF, ACCOUNT_AVATAR_PREF,
                ACCOUNT_COMPANY_RATES_PREF, ACCOUNT_CARRIER_REALTIME_PREF}
    if not isinstance(prefs, dict) or not required <= set(prefs) or set(prefs) - required - optional:
        raise ValueError("查看偏好格式不正确")
    requested_growth_liquid = prefs.get(ACCOUNT_GROWTH_LIQUID_PREF, True)
    if not isinstance(requested_growth_liquid, bool):
        raise ValueError("经验液位设置须为开或关")
    avatar_id = prefs.get(ACCOUNT_AVATAR_PREF, existing_avatar_id)
    if avatar_id is not None and (not isinstance(avatar_id, str) or avatar_id not in ACCOUNT_AVATAR_ALL_IDS):
        raise ValueError("请选择头像预设库中的头像")
    normalized = {}
    for key, available in (("routes", available_routes), ("groups", available_groups), ("ctypes", ACCOUNT_CONTAINER_TYPES)):
        items = prefs[key]
        if not isinstance(items, list) or len(items) > ACCOUNT_PREFERENCE_MAX_ITEMS or any(not isinstance(i, str) or i not in available for i in items):
            raise ValueError("请从当前可选范围中选择" + {"routes":"航线", "groups":"来源群", "ctypes":"柜型"}[key])
        normalized[key] = [] if key == "ctypes" else list(dict.fromkeys(items))
    if prefs["start_tab"] not in ("rates", "space", "dyn", "group"):
        raise ValueError("默认页面须为销售端页签")
    normalized["start_tab"] = prefs["start_tab"]
    # The account liquid is now a fixed growth indicator. Keep the legacy key
    # for old clients and stored profiles, but it is no longer user-configurable.
    normalized[ACCOUNT_GROWTH_LIQUID_PREF] = True
    normalized[ACCOUNT_AVATAR_PREF] = avatar_id
    existing_preferences = existing_preferences or {}
    for key in (ACCOUNT_COMPANY_RATES_PREF, ACCOUNT_CARRIER_REALTIME_PREF):
        value = prefs.get(key, existing_preferences.get(key, False))
        if not isinstance(value, bool):
            raise ValueError("公司查价设置须为开或关")
        normalized[key] = value
    return display, normalized


def account_route_sections(route_rows, available_names):
    """Derive selection regions from current master rows; never persist grouping state."""
    from core.contracts import ACCOUNT_ROUTE_REGION_ORDER
    available = set(available_names)
    sections = {}
    for row in route_rows:
        if row["name"] in available:
            region = str(row.get("region") or "").strip() or "未分区"
            sections.setdefault(region, []).append(row["name"])
    order = [name for name in ACCOUNT_ROUTE_REGION_ORDER if name in sections]
    order += [name for name in sections if name not in order and name != "未分区"]
    if "未分区" in sections:
        order.append("未分区")
    return [{"name": name, "routes": sections[name]} for name in order]
