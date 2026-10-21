"""Local account persistence. Self registration never accepts role/scope fields."""
import secrets
from core.contracts import ROLE_SALES, ok
from services.account_forms import registration, account_name, password_change, account_route_sections


def register(connect, hash_password, now, body):
    name, password = registration(body)
    connection = connect()
    try:
        result = connection.execute(
            "INSERT INTO operators(user_id,name,role,active,password_hash,password_changed_at,created_at,match_mode) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO NOTHING",
            ("usr_" + secrets.token_hex(16), name, ROLE_SALES, 1, hash_password(password), now(), now(), "any"))
        if result.rowcount != 1:
            raise ValueError("账号已被使用；如已有身份或忘记密码，请联系管理员")
        from ops.admin_accounts import record_event
        user_id = connection.execute('SELECT user_id FROM operators WHERE name=?',(name,)).fetchone()[0]
        record_event(connection,user_id,'account_registered',name,user_id,{},now())
        connection.commit()
        return ok({"name": name, "role": ROLE_SALES, "message": "销售账号已创建，请登录"})
    finally:
        connection.close()


def forgot(connect, now, body):
    if set(body) - {"name"}:
        raise ValueError("重置申请只需要账号")
    name = account_name(body.get("name"))
    connection = connect()
    try:
        # Keep unknown names and known accounts indistinguishable to anonymous users.
        account = connection.execute(
            "SELECT user_id,name,COALESCE(NULLIF(display_name,''),name) AS display_name FROM operators WHERE name=?",
            (name,)).fetchone()
        if account:
            connection.execute(
                "INSERT INTO account_recovery_requests(name,status,requested_at) VALUES(?,'requested',?) "
                "ON CONFLICT(name) DO UPDATE SET status='requested',requested_at=excluded.requested_at,resolved_at=NULL,resolved_by=NULL",
                (name, now()))
            from ops.notifications import replace_password_reset_request
            replace_password_reset_request(connection, dict(account), now())
            connection.commit()
        return ok({"message": "如账号已登记，重置申请已提交。请联系管理员核实身份并获取新密码。"})
    finally:
        connection.close()


def change_password(connect, matches, hash_password, now, body, session):
    current, password = password_change(body)
    connection = connect()
    try:
        row = connection.execute("SELECT password_hash FROM operators WHERE name=? AND active=1", (session["name"],)).fetchone()
        if not row or not matches(current, row["password_hash"] or ""):
            raise PermissionError("原密码不正确或账号已停用")
        result = connection.execute(
            "UPDATE operators SET password_hash=?,password_changed_at=? WHERE name=? AND active=1 AND password_hash=?",
            (hash_password(password), now(), session["name"], row["password_hash"]))
        if result.rowcount != 1:
            raise PermissionError("账号状态已变化，请重新登录")
        connection.execute("DELETE FROM auth_sessions WHERE operator_name=?", (session["name"],))
        connection.execute('INSERT INTO account_security(user_id,must_change_password,revision) VALUES(?,0,1) ON CONFLICT(user_id) DO UPDATE SET must_change_password=0,revision=account_security.revision+1',(session['user_id'],))
        from ops.admin_accounts import record_event
        record_event(connection,session['user_id'],'password_changed',session['name'],session['user_id'],{},now())
        resolve_requests(connection, session["name"], session["name"], now())
        connection.commit()
        return ok({"message": "密码已修改，请使用新密码重新登录"})
    finally:
        connection.close()


def resolve_requests(connection, name, actor, stamp):
    changed = connection.execute(
        "UPDATE account_recovery_requests SET status='resolved',resolved_at=?,resolved_by=? WHERE name=? AND status='requested'",
        (stamp, actor, name)).rowcount
    if changed:
        row = connection.execute("SELECT user_id FROM operators WHERE name=?", (name,)).fetchone()
        from ops.notifications import resolve_password_reset_request
        resolve_password_reset_request(connection, row['user_id'] if row else '', stamp)


def recovery_requests(connect):
    connection = connect()
    try:
        return ok({"rows": [dict(row) for row in connection.execute(
            "SELECT r.name,r.requested_at,o.role,o.active FROM account_recovery_requests r JOIN operators o ON o.name=r.name "
            "WHERE r.status='requested' ORDER BY r.requested_at DESC")]})
    finally:
        connection.close()


from core.contracts import (ACCOUNT_GROWTH_LIQUID_PREF, ACCOUNT_GROWTH_LIQUID_DEFAULT,
                            ACCOUNT_AVATAR_PREF, ACCOUNT_AVATAR_ALL_IDS, account_avatar_groups,
                            ACCOUNT_COMPANY_RATES_PREF, ACCOUNT_CARRIER_REALTIME_PREF)

DEFAULT_PREFERENCES = {"routes": [], "groups": [], "ctypes": [], "start_tab": "rates",
                       ACCOUNT_GROWTH_LIQUID_PREF: ACCOUNT_GROWTH_LIQUID_DEFAULT,
                       ACCOUNT_AVATAR_PREF: None,
                       ACCOUNT_COMPANY_RATES_PREF: False,
                       ACCOUNT_CARRIER_REALTIME_PREF: False}


def _profile(connection, session, client_ip, shared_profile=None):
    import json
    row = connection.execute("SELECT user_id,name,display_name,role,created_at,password_changed_at,preferences_json FROM operators WHERE name=? AND active=1", (session["name"],)).fetchone()
    if not row:
        raise PermissionError("账号已停用，请重新登录")
    account = dict(row)
    route_rows = [dict(r) for r in connection.execute("SELECT name,region FROM routes WHERE active=1 ORDER BY COALESCE(sort,900),name")]
    routes = [r["name"] for r in route_rows]
    groups = [r[0] for r in connection.execute("SELECT name FROM chat_groups WHERE active=1 ORDER BY name")]
    # Personal defaults are optional search preferences over the shared catalog.
    preferences = json.loads(account.pop("preferences_json") or "{}")
    preferences = {**DEFAULT_PREFERENCES, **preferences, "ctypes": []}
    preferences[ACCOUNT_GROWTH_LIQUID_PREF] = True
    if preferences[ACCOUNT_AVATAR_PREF] not in ACCOUNT_AVATAR_ALL_IDS:
        preferences[ACCOUNT_AVATAR_PREF] = None
    available = {"routes": routes, "groups": groups}
    # Retired or no-longer-visible choices never silently widen a saved default to all.
    unavailable = {k: [v for v in preferences[k] if v not in available[k]] for k in available}
    if isinstance(shared_profile, dict):
        shared_display = str(shared_profile.get("displayName") or "").strip()
        if shared_display:
            account["display_name"] = shared_display
        shared_avatar = shared_profile.get("avatarId")
        preferences[ACCOUNT_AVATAR_PREF] = None if shared_avatar == "default" else (
            shared_avatar if shared_avatar in ACCOUNT_AVATAR_ALL_IDS else preferences[ACCOUNT_AVATAR_PREF])
        account["shared_growth"] = shared_profile.get("growth")
    account.update({"display_name": account["display_name"] or account["name"], "preferences": preferences,
        "options": {**available, "ctypes": [], "avatar_ids": list(ACCOUNT_AVATAR_ALL_IDS),
                    "avatar_groups": account_avatar_groups(),
                    "route_sections": account_route_sections(route_rows, routes)}, "unavailable": unavailable,
        "current_ip": client_ip, "login_at": session.get("login_at"), "expires_at": session.get("expires_at")})
    return account


def profile(connect, session, client_ip, shared_profile=None):
    connection = connect()
    try:
        return ok(_profile(connection, session, client_ip, shared_profile))
    finally:
        connection.close()


def save_profile(connect, session, client_ip, body, save_shared=None):
    import json
    from services.account_forms import profile_update
    connection = connect()
    try:
        current = _profile(connection, session, client_ip)
        display, preferences = profile_update(body, current["options"]["routes"], current["options"]["groups"],
                                               current["preferences"][ACCOUNT_AVATAR_PREF], current["preferences"])
        shared_profile = None
        if save_shared:
            shared_profile = save_shared(display, preferences[ACCOUNT_AVATAR_PREF] or "default")
        result = connection.execute("UPDATE operators SET display_name=?,preferences_json=? WHERE name=? AND active=1", (display, json.dumps(preferences, ensure_ascii=False), session["name"]))
        if result.rowcount != 1:
            raise PermissionError("账号状态已变化，请重新登录")
        if display != current['display_name']:
            from ops.admin_accounts import record_event
            from manualdb import now_cn
            record_event(connection,session['user_id'],'profile_updated',session['name'],session['user_id'],
                         {'display_name':{'before':current['display_name'],'after':display}},now_cn())
        connection.commit()
        return ok(_profile(connection, session, client_ip, shared_profile))
    finally:
        connection.close()
