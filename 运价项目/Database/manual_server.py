# -*- coding: utf-8 -*-
"""人工维护台账（局域网版）服务：本机独立 SQLite + 与已发布版同一套接口，前端页面原样复用。

本文件与 manualdb.py 只依赖 Python 标准库，不引用任何其它看板的代码或数据库。

用法：
    python manual_server.py                        前台跑，0.0.0.0:8795
    python manual_server.py --daemon              后台常驻，日志写 manual.log
    python manual_server.py --import snapshot.json 导入整库快照后退出
    python manual_server.py --init-only           只建库建表后退出

接口：
    GET  /  /index.html                看板页
    GET  /api/rates /api/space         扁平查询（目的港/起运港筛选会双向展开港群）
    GET  /api/rates_grouped /api/space_grouped   折叠视图
    GET  /api/stats /api/options /api/bootstrap /api/operators /api/chat-groups
    GET  /api/master[?full=1]          主数据 + 港群 + 使用频次
    GET  /api/local-token              仅首次管理员初始化前允许本机回环读取
    GET  /api/health/live              无业务数据的公开存活检查
    GET  /download                     Prospector Windows 客户端公开下载页
    GET  /api/downloads/prospector/latest  当前稳定版安装包清单
    GET  /downloads/prospector/<文件>       当前稳定版安装包流式下载
    GET  /images/<名>                  静态原图：从本机 images/ 目录取运价截图供看板显示
    GET  /api/export                   整库快照
    GET  /api/pending                  待审核列表 ?status=awaiting|approved|rejected|all&kind=&limit=&offset=（每行附解析后的图片）
    GET  /api/audit                    全库标准化体检（列出存量不达标行）
    POST /api/push                     录入 {table, records:[中文字段], origin}；过硬标准化闸门 + 自动判新增/更新(留痕)
    POST /api/validate                 预检（只读，不写库）：{table, records} → 每行 {ok,resolved,errors}
    POST /api/pending                  待审核暂存 {table, records, origin='agent'}：机械校验 + 判新增/更新
    POST /api/agent/intake/v1          外部 Agent 标准 JSON 入口：Bearer 鉴权，复用待审核校验
    POST /api/pending/approve          放行一条暂存记录入库（仅 check_status=passed）
    POST /api/pending/update           编辑暂存记录 {id, fields}：重校验 + 重判新增/更新
    POST /api/pending/sync             队列批量同步 {field,old,new,...}：preview=true 只回报同字段同旧值的其他待审条目；传 ids 则逐条重校验后改值（只改值不放行）
    POST /api/pending/reject           驳回（软释放，移入驳回日志，留原因）
    POST /api/pending/rework           驳回回炉：agent 按原因改一版交回人工（只许改驳回指向字段，≤2 轮）
    POST /api/pending/restore          撤回误驳（驳回日志 → 待处理）
    POST /api/pending/delete           物理删除暂存行或明确 ID 组（不可恢复）
    POST /api/record/update            就地改（不留痕）
    POST /api/record/revise            留痕更新；可带 record_type 切换运价/舱位分类
    POST /api/record/delete            删一条
    POST /api/master/save|delete       港口 / 船司 / 航线 / 发送人 / 港群 维护
    POST /api/operator/save|delete /api/chat-group/save|delete /api/chat-groups/import
    POST /api/sync                     批量导入英文列名行
    POST /api/import                   导入整库快照
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime
from threading import RLock, BoundedSemaphore
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:  # Windows 控制台默认 GBK，中文输出会乱码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 输出重定向到文件时无需处理
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import manualdb  # noqa: E402
from core.contracts import AGENT_REVIEW_ISSUES_SETTLE_ROUTE
import postgresdb  # noqa: E402
import price_sanitize  # noqa: E402
from core.contracts import (PENDING_REJECT_GROUP_ROUTE, PENDING_SOURCE_VIEW, PENDING_WHOLE_PASSED_PARAM, OPS_ID_QUERY_PARAM, MONITORING_GROUPS_ROUTE, MONITORING_RUNS_ROUTE,
                            AGENT_PENDING_SUPPRESS_ROUTE,
                            MONITORING_SUMMARY_ROUTE, RELEASE_DECISION_ROUTE, REVIEW_ISSUES_ROUTE, REVIEW_ISSUES_RESOLVE_ROUTE,
                            SYSTEM_HEALTH_ROUTE, SYSTEM_READINESS_ROUTE)  # noqa: E402
from core.agent_intake_contracts import AGENT_INTAKE_ROUTE  # noqa: E402
from core.settings import DeploymentSettings  # noqa: E402
from transport.release_routes import build_release_post_routes  # noqa: E402
from transport.system_routes import build_system_get_routes  # noqa: E402
from transport.monitoring_routes import build_monitoring_get_routes  # noqa: E402
from core.contracts import (AUTH_STATUS_ROUTE, LIVENESS_ROUTE, CENTER_NAVIGATOR_DASHBOARD_ROUTE, ROLE_ADMIN,
                            PENDING_PRESERVE_OTHER_SOURCES, PENDING_PRESERVE_EXISTING_SOURCE_ROWS,
                            SALES_GROUP_SUMMARY_ROUTE, SALES_GROUP_VISIT_ROUTE, fail, ok)
from services.access_policy import account_identity, session_security_changed, scoped_sales_query, browser_write_allowed, secure_transport
from ops.account_security import LoginLimiter, LoginThrottled
from core.contracts import AUTH_REQUEST_MAX_BYTES, HTTP_REQUEST_TIMEOUT_SECONDS, HTTP_MAX_ACTIVE_REQUESTS, ROLE_OPERATOR, ROLE_SALES
from transport.agent_routes import build_agent_post_routes
from transport.access_rules import authorize
from transport.agent_intake_routes import build_agent_intake_route
from transport.publisher_routes import build_publisher_routes
from ops.publisher_accounts import (PublisherPeople, accounts as publisher_accounts,
                                    decide as publisher_decide, confirmed_people, publisher_members)
from ops.publisher_identity import (remove_excluded_placeholders, remove_unopened_placeholder,
                                    remove_unopened_placeholder_by_user_id)

from core.contracts import AUTH_REGISTER_ROUTE, AUTH_FORGOT_ROUTE, AUTH_PASSWORD_ROUTE, ACCOUNT_PROFILE_ROUTE
from transport.account_routes import build_account_routes, build_profile_routes
from ops import account_selfservice
from ops import account_growth
from core.growth_contracts import (GROWTH_APPROVAL_EVENT, GROWTH_APPROVAL_POINTS,
                                   GROWTH_CORRECTION_EVENT, GROWTH_CORRECTION_POINTS,
                                   GROWTH_GOVERNANCE_EVENT, GROWTH_GOVERNANCE_ITEM_EVENT,
                                   GROWTH_GOVERNANCE_POINTS, GROWTH_REVIEW_EVENT,
                                   GROWTH_ROUTE, GROWTH_SEARCH_ROUTE, GROWTH_DAILY_CAP,
                                   GROWTH_RULE_VERSION, GROWTH_HISTORY_SIZE, GROWTH_HISTORY_MAX_SIZE)
from services.account_growth import summary as growth_summary
from ops import admin_accounts
from transport.admin_routes import build_admin_routes
from core.admin_contracts import PASSWORD_CHANGE_REQUIRED
from transport.notice_routes import build_notice_routes
from core.notice_contracts import NOTICES_ROUTE, NOTICE_SAVE_ROUTE, NOTICE_PIN_ROUTE, NOTICE_REQUEST_MAX
from core.notification_contracts import NOTIFICATION_GET_ROUTES
from core.feedback_contracts import FEEDBACK_ROUTE, FEEDBACK_DISMISS_ROUTE
from ops import item_feedback
from ops import notifications
from ops import user_activity
from transport.notification_routes import build_notification_routes
from core.activity_contracts import (ACTIVITY_EVENTS_ROUTE, ACTIVITY_PAGE_OPEN_ROUTE,
                                     ACTIVITY_QUOTE_USAGE_ROUTE, ACTIVITY_SUMMARY_ROUTE,
                                     ACTIVITY_TIMELINE_ROUTE)
from core.api_monitor_contracts import API_MONITOR_ROUTES
from ops import api_monitor
from ops import center_auth
from transport.api_monitor_routes import build_api_monitor_routes
from core.download_contracts import DOWNLOAD_MANIFEST_ROUTE
from services.download_catalog import DownloadCatalog, DownloadManifestInvalid, DownloadNotFound, DownloadUnavailable
from transport.download_routes import build_download_routes, resolve_download_asset

from core.prospector_contracts import AUTHORIZE_ROUTE as PROSPECTOR_AUTHORIZE, TOKEN_ROUTE as PROSPECTOR_TOKEN, ME_ROUTE as PROSPECTOR_ME, OPTIONS_ROUTE as PROSPECTOR_OPTIONS, LOGOUT_ROUTE as PROSPECTOR_LOGOUT, POST_ROUTES as PROSPECTOR_POST
from ops import prospector_auth
from services.prospector_auth import authorization_params, read_allowed as prospector_read_allowed, query_identity, private_http_transport
from transport import prospector_routes
from core.company_rates_contracts import (COMPANY_RATE_QUERY_ROUTE, COMPANY_RATE_TASK_ROUTE,
                                          COMPANY_REALTIME_QUERY_ROUTE)
from ops.company_rates import CompanyRateRuntime
from transport.company_rate_routes import build_company_rate_routes
from ops.company_credentials import CompanyCredentialStore
from ops.company_token_refresh import CompanyTokenRefresher
from transport.company_credential_routes import build_company_credential_routes

CFG: Dict[str, str] = {"db": "", "token": "", "backend": "sqlite", "postgres_env_file": "", "center_auth_url": "", "agent_api_token": ""}
CENTER_SESSION_TOKENS: Dict[str, str] = {}
CENTER_SESSION_LOCK = RLock()
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # Absolute lifetime from login; ordinary reads do not extend it.
SESSION_COOKIE = "freight_session"
LOGIN_LIMITER = LoginLimiter()
RECOVERY_LIMITER = LoginLimiter()
DEFAULT_PORT = 8795
MAX_BODY = 64 * 1024 * 1024      # POST body 上限（64MB）：正常单批 1000 条带原文远小于此，防异常大包吃内存
BACKUP_KEEP = 7                  # 启动自动备份保留份数
BACKUP_DIR_NAME = "backup"
SERVER_STARTED_MONOTONIC = time.monotonic()
SERVER_REQUEST_QUEUE_SIZE = 128  # 300 人局域网突发打开页面时避免默认 backlog=5 的 TCP 重传
DEPLOYMENT_SETTINGS = DeploymentSettings.from_env()
DOWNLOAD_CATALOG = DownloadCatalog(Path(os.environ.get("NAVIGATOR_DOWNLOADS_DIR", str(HERE / ".runtime" / "downloads" / "prospector"))))


def log(level: str, msg: str) -> None:
    print(f"[{manualdb.now_cn()}] [{level}] [manual-board] {msg}", flush=True)


def contract_failure(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def award_growth_safe(connection, body, activity_event, subjects=(), activity_daily_cap=None):
    """Growth is best-effort after a verified business write and never breaks it."""
    user_id = str(body.get("_actor_id") or "").strip()
    if not user_id or user_id == "agent":
        return None
    try:
        growth = account_growth.award_business_activity(
            connection, user_id, activity_event, subjects,
            activity_daily_cap=activity_daily_cap)
        center_token = str(body.get("_center_token") or "")
        if growth and CFG.get("center_auth_url") and center_token:
            try:
                account_growth.sync_center(connection, CFG["center_auth_url"], center_token, user_id)
            except Exception as sync_error:  # noqa: BLE001 - local outbox remains pending
                log("WARN", f"中心经验同步待重试 user={user_id}: {sync_error}")
        return growth
    except Exception as error:  # noqa: BLE001 - business result already committed
        log("ERROR", f"经验记账失败 user={user_id} event={activity_event}: {error}")
        return None


def center_growth_summary(growth):
    if not isinstance(growth, dict):
        return None
    return {
        "total_xp": int(growth.get("totalXp") or 0), "level": int(growth.get("level") or 1),
        "cycle": int(growth.get("cycle") or 1), "level_name": str(growth.get("levelName") or ""),
        "level_start": int(growth.get("levelStart") or 0), "next_threshold": int(growth.get("nextThreshold") or 0),
        "level_cost": int(growth.get("levelCost") or 0), "level_progress": int(growth.get("levelProgress") or 0),
        "cycle_start": int(growth.get("cycleStart") or 0), "cycle_end": int(growth.get("cycleEnd") or 0),
        "cycle_xp": int(growth.get("cycleXp") or 0), "today_awarded": bool(growth.get("todayAwarded")),
    }


def center_growth_payload(profile, query):
    try:
        limit = int(query.get("limit", GROWTH_HISTORY_SIZE))
        offset = int(query.get("offset", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid growth pagination") from error
    if not 1 <= limit <= GROWTH_HISTORY_MAX_SIZE or offset < 0:
        raise ValueError("invalid growth pagination")
    growth = profile.get("growth") if isinstance(profile, dict) else None
    return ok({"summary": center_growth_summary(growth), "history": [], "total": 0,
        "today_xp": int(growth.get("todayXp") or 0) if isinstance(growth, dict) else 0,
        "limit": limit, "offset": offset, "rule": {
            "version": GROWTH_RULE_VERSION, "daily_search_points": 18,
            "daily_activity_points": 18, "approval_points": GROWTH_APPROVAL_POINTS,
            "correction_points": GROWTH_CORRECTION_POINTS, "governance_points": GROWTH_GOVERNANCE_POINTS,
            "daily_cap": GROWTH_DAILY_CAP, "cycle_xp": 1026}})


def center_growth_award(receipt):
    if not isinstance(receipt, dict) or not receipt.get("awarded"):
        return None
    summary = center_growth_summary(receipt.get("summary"))
    points = int(receipt.get("awardedPoints") or 0)
    if not summary or points <= 0:
        return None
    previous = growth_summary(max(0, summary["total_xp"] - points), False)
    return {"awarded": True, "awarded_points": points,
        "today_xp": int(receipt.get("summary", {}).get("todayXp") or 0),
        "daily_cap": GROWTH_DAILY_CAP,
        "level_up": (summary["cycle"], summary["level"]) != (previous["cycle"], previous["level"]),
        "cycle_up": summary["cycle"] != previous["cycle"],
        "previous_level": previous["level"], "previous_cycle": previous["cycle"], "summary": summary}


def record_sales_activity_safe(connection, query, endpoint, result):
    """Usage analytics is best-effort and must never interrupt a sales query."""
    try:
        return user_activity.record_sales_search(connection, query, endpoint, result)
    except Exception as error:  # noqa: BLE001 - the primary read already succeeded
        log("ERROR", f"用户查询统计写入失败 user={query.get('_actor_id')} endpoint={endpoint}: {error}")
        return {"recorded": False}


def conn():
    if CFG["backend"] == "postgres":
        return postgresdb.connect(Path(CFG["postgres_env_file"]))
    return manualdb.connect(CFG["db"])


def database_probe():
    if CFG["backend"] == "postgres":
        return postgresdb.database_probe(Path(CFG["postgres_env_file"]))
    return manualdb.database_probe(CFG["db"])


def heartbeat_runtime_dir() -> Path:
    """Resolve the configured heartbeat runtime without exposing it through HTTP."""
    contract = HERE.parent / "Skill" / "freight-rate-heartbeat" / "scripts" / "config" / "ledger_contract.json"
    try:
        raw = json.loads(contract.read_text(encoding="utf-8"))
        return (HERE.parent / str(raw.get("runtime_dir") or "Skill/heartbeat-data")).resolve()
    except (OSError, ValueError, TypeError):
        return (HERE.parent / "Skill" / "heartbeat-data").resolve()


def password_hash(password: str, salt: str | None = None) -> str:
    """PBKDF2 hash stored as algorithm$iterations$salt$digest; passwords never enter SQLite in plaintext."""
    import hashlib
    salt = salt or secrets.token_hex(16)
    iterations = 310000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def password_matches(password: str, stored: str) -> bool:
    import hashlib
    try:
        algo, iters, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(iters)).hex()
        return secrets.compare_digest(actual, digest)
    except (TypeError, ValueError):
        return False


def active_admin_exists() -> bool:
    c = conn()
    try:
        return bool(c.execute("SELECT 1 FROM operators WHERE active=1 AND role='admin' AND password_hash IS NOT NULL LIMIT 1").fetchone())
    finally:
        c.close()


def auth_status(session: Dict[str, Any] | None) -> Dict[str, Any]:
    return {"ok": True, "authenticated": bool(session), "setup_required": not active_admin_exists(),
            "user": (account_identity(session) if session else None)}


def auth_login(body: Dict[str, Any], client_ip: str) -> tuple[Dict[str, Any], str]:
    name, password = str(body.get("name") or "").strip(), str(body.get("password") or "")
    if not name or not password:
        raise ValueError("name and password required")
    if CFG.get("center_auth_url"):
        center = center_auth.login(CFG["center_auth_url"], name.lower(), password)
        access = center_auth.validate(CFG["center_auth_url"], center["token"])["access"]
        c = conn()
        try:
            row = c.execute("SELECT o.user_id,o.name,o.display_name,o.role,o.active,o.password_hash,0 AS must_change_password FROM operators o WHERE lower(o.name)=?", (name.lower(),)).fetchone()
            if not row or not row["active"]:
                raise PermissionError("中心账号未建立本地运价业务身份")
            role = {"admin": ROLE_ADMIN, "operator": ROLE_OPERATOR, "sales": ROLE_SALES}.get(str(access.get("role") or ""))
            if not role:
                raise PermissionError("账号没有 Navigator 产品权限")
            sid = secrets.token_urlsafe(32)
            c.execute("INSERT INTO auth_sessions(token_hash,operator_name,client_ip,expires_at,created_at) VALUES(?,?,?,?,?)",
                      (hashlib.sha256(sid.encode("ascii")).hexdigest(), row["name"], client_ip,
                       int(time.time()) + SESSION_TTL_SECONDS, manualdb.now_cn()))
            c.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (int(time.time()),))
            c.commit()
            with CENTER_SESSION_LOCK:
                CENTER_SESSION_TOKENS[sid] = center["token"]
            try:
                account_growth.sync_center(c, CFG["center_auth_url"], center["token"], row["user_id"])
            except Exception as error:  # noqa: BLE001 - login remains valid and outbox stays pending
                log("WARN", f"登录后中心经验同步待重试 user={row['user_id']}: {error}")
            local = dict(row)
            local["role"] = role
            local["display_name"] = center["identity"].get("displayName") or row["display_name"] or row["name"]
            return {"ok": True, "user": account_identity(local)}, sid
        finally:
            c.close()
    c = conn()
    try:
        row = c.execute("SELECT o.user_id,o.name,o.display_name,o.role,o.active,o.password_hash,COALESCE(sec.must_change_password,0) AS must_change_password FROM operators o LEFT JOIN account_security sec ON sec.user_id=o.user_id WHERE o.name=?", (name,)).fetchone()
        if not row or not row['active'] or not password_matches(password, row["password_hash"] or ""):
            if row:
                admin_accounts.record_event(c,row['user_id'],'login_failed','未认证请求','anonymous',{'client_ip':client_ip},manualdb.now_cn())
                c.commit()
            raise PermissionError("用户名、密码或账号状态不正确")
        sid = secrets.token_urlsafe(32)
        c.execute("INSERT INTO auth_sessions(token_hash,operator_name,client_ip,expires_at,created_at) VALUES(?,?,?,?,?)",
                  (hashlib.sha256(sid.encode("ascii")).hexdigest(), row["name"], client_ip,
                   int(time.time()) + SESSION_TTL_SECONDS, manualdb.now_cn()))
        c.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (int(time.time()),))
        admin_accounts.record_event(c,row['user_id'],'login_success',row['name'],row['user_id'],{'client_ip':client_ip},manualdb.now_cn())
        c.commit()
        return {"ok": True, "user": account_identity(row)}, sid
    finally:
        c.close()


def auth_setup(body: Dict[str, Any]) -> Dict[str, Any]:
    if active_admin_exists():
        raise PermissionError("管理员已初始化，请使用管理员账号登录")
    name, password = str(body.get("name") or "").strip(), str(body.get("password") or "")
    if len(name) < 2 or len(name) > 64:
        raise ValueError("管理员名称须为 2-64 个字符")
    if not password:
        raise ValueError("密码不能为空")
    c = conn()
    try:
        data = {"name": name, "role": "admin", "active": 1, "match_mode": "any",
                "password_hash": password_hash(password)}
        return {"ok": True, **manualdb.save_operator(c, data)}
    finally:
        c.close()


def ensure_token(path: Path) -> str:
    from ops.token_security import protect_token_file
    path.touch(exist_ok=True)
    protect_token_file(path)
    if path.exists():
        t = path.read_text(encoding="utf-8").strip()
        # A short hand-written value is not a bearer secret.  Keep only tokens
        # generated by this service (``frtm_`` + 32 hex chars); rotate legacy
        # values on the next restart.
        if t.startswith("frtm_") and len(t) == 37 and all(c in "0123456789abcdef" for c in t[5:]):
            return t
    t = "frtm_" + secrets.token_hex(16)
    path.write_text(t, encoding="utf-8")
    return t


def auto_backup(db_path: str) -> None:
    """启动时在线热备份：manualdb.backup_to 不锁库，保留最近 BACKUP_KEEP 份，旧的最先删。"""
    import glob
    dst = manualdb.backup_to(db_path)   # 默认落在 <库目录>/backup/
    bdir = str(Path(dst).parent)
    olds = sorted(glob.glob(os.path.join(bdir, "freight_manual-*.db")))
    for f in olds[:-BACKUP_KEEP]:
        try:
            os.remove(f)
        except OSError:
            pass
    log("INFO", f"启动自动备份 -> {dst}（保留最近 {BACKUP_KEEP} 份）")


def port_taken(port: int) -> bool:
    """端口预检：Windows 允许 0.0.0.0 与 127.0.0.1 同时绑同一端口且都 bind 成功，
    浏览器却先撞更具体的那条（本次就是这么被心跳板接走的），所以要主动探一次。"""
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def images_for(row: Dict[str, Any]) -> list:
    """列图片引用并解析本地文件：Database/images/ 里存在的给可访问 url，否则标 missing。"""
    import re
    names, seen = [], set()

    def add(nm: str) -> None:
        nm = (nm or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)

    # 两条链路共用这一个解析：业务表行是英文列名，待审核传的是中文字段 payload，两边都认
    declared = row.get("image_file") or row.get("image_url") or row.get("图片名")
    if declared:
        for part in re.split(r"[,，、;；]\s*", str(declared)):
            add(part)
    text = str(row.get("message_text") or row.get("消息原文") or "")
    for m in re.finditer(r"mediaId=(@[A-Za-z0-9_\-]+)", text):
        add(m.group(1))  # 兜底：无 image_file 时按消息原文里的 mediaId 找同名图
    out = []
    for nm in names:
        url, found = _resolve_image(nm)
        out.append({"name": nm, "file": found, "url": url, "missing": url is None})
    return out


_IMG_INDEX: Dict[str, Any] = {"key": None, "by_stem": {}, "memo": {}}   # 目录 mtime 指纹 + 词干索引 + 单名结果记忆


def _img_index(imgdir: Path) -> Dict[str, str]:
    """建一次并缓存 images/ 的"mediaId 词干前 16 字符 → 文件名"索引。
    指纹只取目录自身 mtime（一次 stat，微秒级）：增删文件会改动它，故新下载的图下一请求就生效。
    绝不能在指纹判断里 iterdir——那样等于把旧实现的全目录扫描留在每次解析上。"""
    try:
        key = int(imgdir.stat().st_mtime)
    except OSError:
        return {}                                     # 目录没了：空索引，调用方按"找不到图"处理
    if _IMG_INDEX["key"] != key:
        by_stem: Dict[str, str] = {}
        try:
            for f in imgdir.iterdir():
                stem = f.stem.lstrip("@")[:16]        # 文件名是 <mediaId>_宽_高，键取前 16 字符（与旧实现前缀匹配同口径）
                if stem:
                    by_stem.setdefault(stem, f.name)  # 同词干多份文件留第一份（等价旧实现的 iterdir 顺序）
        except OSError:
            return _IMG_INDEX["by_stem"] or {}
        _IMG_INDEX.update(key=key, by_stem=by_stem, memo={})   # 目录变了，单名记忆一起作废
    return _IMG_INDEX["by_stem"]


def _resolve_image(name: str):
    """在 Database/images/ 里按精确名或 mediaId 词干找真实文件，返回 (url, filename) 或 (None, None)。"""
    from urllib.parse import quote
    from unicodedata import normalize

    name = normalize("NFC", str(name))
    memo = _IMG_INDEX["memo"]
    if name in memo:
        return memo[name]
    imgdir = HERE / "images"
    stem = name.lstrip("@")[:16]
    direct = imgdir / name
    if direct.is_file():
        out = ("/images/" + quote(direct.name), direct.name)
    elif not stem:
        out = (None, None)
    else:
        hit = _img_index(imgdir).get(stem)
        out = (("/images/" + quote(hit), hit) if hit else (None, None))
    if len(memo) > 4000:                              # 防御：长跑进程里名字集合无界增长就清一次
        memo.clear()
    memo[name] = out
    return out


def with_images(payload: Dict[str, Any]) -> Dict[str, Any]:
    for row in payload.get("rows") or []:
        row["images"] = images_for(row)
    for g in payload.get("groups") or []:
        if isinstance(g.get("current"), dict):
            g["current"]["images"] = images_for(g["current"])
        for h in g.get("history") or []:
            if isinstance(h, dict):
                h["images"] = images_for(h)
    return payload


def _kind(body: Dict[str, Any]) -> str:
    # 契约文档写的是 "kind"，历史实现读的是 "table"；两者都收，避免 agent 按文档传 kind 被 500。
    kind = body.get("table") or body.get("kind")
    if kind not in ("rates", "space", manualdb.GSUG_KIND):
        raise ValueError('table/kind must be "rates", "space" or "group_suggestion"')
    return kind


# ---------------- 业务动作：每个都开自己的连接，多线程安全 ----------------

def act_flat(kind: str, q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        result = manualdb.query(c, manualdb.get_master(c), kind, q)
        feedback = item_feedback.list_for_entries(c, [row.get("entry_id") for row in result["rows"]])
        for row in result["rows"]:
            row["feedback"] = feedback.get(row.get("entry_id"), [])
        record_sales_activity_safe(c, q, "/api/rates" if kind == "rates" else "/api/space", result)
        return {"ok": True, "feedback_supported": True, **with_images(result)}
    finally:
        c.close()


def act_grouped(kind: str, q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        result = manualdb.query_grouped(c, manualdb.get_master(c), kind, q)
        quotes = [quote for group in result["groups"] for quote in
                  ([group["current"]] if group.get("current") else []) + group.get("history", [])]
        feedback = item_feedback.list_for_entries(c, [entry_id for quote in quotes
            for entry_id in [quote.get("entry_id"), *(quote.get("cab_entry_id") or {}).values()]])
        for quote in quotes:
            quote["feedback_by_entry"] = {entry_id: feedback[entry_id] for entry_id in
                [quote.get("entry_id"), *(quote.get("cab_entry_id") or {}).values()] if feedback.get(entry_id)}
        record_sales_activity_safe(c, q, "/api/rates_grouped" if kind == "rates" else "/api/space_grouped", result)
        return {"ok": True, "feedback_supported": True, **with_images(result)}
    finally:
        c.close()


def act_sales_groups(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        return ok(manualdb.sales_group_summary(c, manualdb.get_master(c), q, q.get("_actor_id") or ""))
    finally:
        c.close()


def act_sales_group_visit(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        scope = scoped_sales_query(SALES_GROUP_SUMMARY_ROUTE, {"group": body.get("name")},
                                   {"role": body["_role"], "name": body["_actor"]})
        return ok(manualdb.record_sales_group_visit(c, body["_actor_id"], body.get("name"), scope))
    finally:
        c.close()


def act_activity_page_open(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        return user_activity.record_page_open(c, body)
    finally:
        c.close()


def act_activity_quote_usage(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        return user_activity.record_quote_usage(c, body)
    finally:
        c.close()


def act_activity_summary(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        return user_activity.summary(c, q)
    finally:
        c.close()


def act_activity_events(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        return user_activity.events(c, q)
    finally:
        c.close()


def act_activity_timeline(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        return user_activity.timeline(c, q)
    finally:
        c.close()


def act_feedback_read(q: Dict[str, str]) -> Dict[str, Any]:
    if not q.get("entry_id"):
        raise ValueError("条目编号必填")
    c = conn()
    try:
        return ok(item_feedback.read(c, q.get("entry_id") or ""))
    finally:
        c.close()


def act_feedback_create(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        result = item_feedback.create(c, body.get("entry_id"), body.get("changes"),
                                      body.get("expected"), body.get("note"),
                                      body.get("_actor_id"), body.get("_actor_display"), manualdb.now_cn())
        log("INFO", f"条目反馈提交 entry_id={result['entry_id']} submission_id={result['submission_id']}")
        return ok(result)
    finally:
        c.close()


def act_feedback_dismiss(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        result = item_feedback.dismiss(c, body.get("submission_id"), body.get("action"),
                                       body.get("_actor_id"), body.get("_role"), manualdb.now_cn())
        log("INFO", f"条目反馈处理 submission_id={result['submission_id']} status={result['status']}")
        return ok(result)
    finally:
        c.close()


def act_stats() -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True, "backend": CFG["backend"], **manualdb.stats(c)}
    finally:
        c.close()


def act_center_navigator_dashboard(token: str) -> Dict[str, Any]:
    """给中心管理页的只读摘要；不暴露价格、原文、图片、凭据或明细。"""
    if not CFG.get("center_auth_url"):
        raise RuntimeError("中心身份服务未配置")
    access = center_auth.validate(CFG["center_auth_url"], token).get("access") or {}
    if access.get("role") not in (ROLE_ADMIN, ROLE_OPERATOR):
        raise PermissionError("Navigator 管理摘要需要航管或管理员权限")
    c = conn()
    try:
        stats = manualdb.stats(c)
        pending_total = c.execute(
            "SELECT COUNT(*) FROM pending_records WHERE review_status='awaiting'"
        ).fetchone()[0]
        pending_failed = c.execute(
            "SELECT COUNT(*) FROM pending_records WHERE review_status='awaiting' AND check_status='failed'"
        ).fetchone()[0]
        issues_open = c.execute(
            "SELECT COUNT(*) FROM review_issues WHERE status='open'"
        ).fetchone()[0]
        active_groups = c.execute(
            "SELECT COUNT(*) FROM chat_groups WHERE active=1"
        ).fetchone()[0]
        return ok({
            "service": {"status": "ok", "backend": CFG["backend"], "port": DEFAULT_PORT},
            "counts": {
                "rates": stats.get("ratesTotal", 0),
                "currentRates": stats.get("ratesCurrent", 0),
                "space": stats.get("spaceTotal", 0),
                "groups": stats.get("groups", 0),
                "activeGroups": active_groups,
                "carriers": stats.get("carriers", 0),
                "pendingAwaiting": pending_total,
                "pendingFailed": pending_failed,
                "reviewIssuesOpen": issues_open,
            },
            "latestMessage": stats.get("latestMessage") or None,
        })
    finally:
        c.close()


def act_options() -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True, **manualdb.options(c)}
    finally:
        c.close()


def act_bootstrap(role: str = "") -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True,
                "operators": [o for o in manualdb.list_operators(c) if o.get("active")] if role == ROLE_ADMIN else [],
                "chatGroups": [g for g in manualdb.list_chat_groups(c) if g.get("active")],
                "options": manualdb.options(c), "stats": manualdb.stats(c)}
    finally:
        c.close()


def act_master(full: bool) -> Dict[str, Any]:
    c = conn()
    try:
        return manualdb.master_payload(c, full=full)
    finally:
        c.close()


def act_operators() -> Dict[str, Any]:
    c = conn()
    try:
        people = confirmed_people()
        rows = manualdb.list_operators(c)
        for row in rows:
            row["confirmed_roster_groups"] = people.get(row.get("publisher_name") or row["name"], [])
        return {"ok": True, "rows": rows}
    finally:
        c.close()


def act_chat_groups() -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True, "rows": manualdb.list_chat_groups(c)}
    finally:
        c.close()


def act_export() -> Dict[str, Any]:
    c = conn()
    try:
        return manualdb.export_all(c)
    finally:
        c.close()


def act_push(body: Dict[str, Any]) -> Dict[str, Any]:
    kind = _kind(body)
    records = body.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records empty")
    if len(records) > 1000:
        raise ValueError("records > 1000 per request")
    origin = body.get("origin") or "manual"
    c = conn()
    try:
        res = manualdb.push_with_review(c, manualdb.get_master(c), kind, records, origin=origin, viewer=body.get("_scope_viewer") or "")
        log("INFO", f"push {kind} 新增{res['accepted']} 更新{res['revised']} "
                    f"拒绝{len(res['rejected'])} 条 origin={origin}")
        return res
    finally:
        c.close()


def act_revise(body: Dict[str, Any]) -> Dict[str, Any]:
    kind = _kind(body)
    key = body.get("content_key")
    if not key:
        raise ValueError("content_key required")
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields required")
    record_type = body.get("record_type")
    c = conn()
    try:
        if record_type and record_type != kind:
            res = manualdb.reclassify_record(c, manualdb.get_master(c), str(key), str(record_type), fields,
                                             origin=body.get("origin") or "manual")
        else:
            res = manualdb.revise_record(c, manualdb.get_master(c), kind, key, fields,
                                         origin=body.get("origin") or "manual")
        if res.get("ok"):
            log("INFO", f"revise {kind}{'->' + str(record_type) if record_type else ''} {str(key)[:12]} → {res['revised']} 行，"
                        f"{len(res['changes'])} 处变动")
        return res
    finally:
        c.close()


def act_update(body: Dict[str, Any]) -> Dict[str, Any]:
    kind = _kind(body)
    key = body.get("content_key")
    if not key:
        raise ValueError("content_key required")
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields required")
    c = conn()
    try:
        res = manualdb.update_record(c, kind, key, fields)
        return {"ok": True, "table": kind, **res} if res["updated"] \
            else {"ok": False, "error": "record not found", "table": kind, **res}
    finally:
        c.close()


def act_delete(body: Dict[str, Any]) -> Dict[str, Any]:
    kind = _kind(body)
    key = body.get("content_key")
    if not key:
        raise ValueError("content_key required")
    c = conn()
    try:
        n = manualdb.delete_record(c, kind, key)
        log("WARN", f"删除记录 table={kind} key={str(key)[:12]}（不可撤销）")
        return {"ok": True, "table": kind, "deleted": n} if n \
            else {"ok": False, "error": "record not found"}
    finally:
        c.close()


def act_master_save(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        res = manualdb.master_save(c, body.get("table"), body.get("row") or {})
        log("INFO", f"主数据 {body.get('table')} {res['action']} {res['key']}")
        return res
    finally:
        c.close()


def act_master_delete(body: Dict[str, Any]) -> Dict[str, Any]:
    table = body.get("table")
    c = conn()
    try:
        pk = manualdb.MASTER_TABLES.get(table or "", {}).get("pk")
        key = str((body.get(pk) if pk else None) or body.get("key") or "").strip()
        res = manualdb.master_delete(c, table, key)
        if res["ok"]:
            log("INFO", f"删除主数据 {table} {key}")
        return res
    finally:
        c.close()


def act_operator_save(body: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(body)
    if "password_hash" in body:
        raise ValueError("use password, not password_hash")
    password = str(body.pop("password", "") or "")
    if password:
        body["password_hash"] = password_hash(password)
    c = conn()
    try:
        before = c.execute("SELECT * FROM operators WHERE name=?", (str(body.get("name") or "").strip(),)).fetchone()
        if not before and not password.strip():
            body["active"] = 0  # A person record does not grant login.
        res = manualdb.save_operator(c, body, commit=False)
        after = c.execute("SELECT * FROM operators WHERE name=?", (res["name"],)).fetchone()
        revoked = session_security_changed(dict(before) if before else None, dict(after))
        if revoked:
            c.execute("DELETE FROM auth_sessions WHERE operator_name=?", (res["name"],))
        if password:
            account_selfservice.resolve_requests(c, res["name"], str(body.get("_actor") or "admin"), manualdb.now_cn())
        c.commit()  # 改密码/停用/改角色后强制重新登录
        return {"ok": True, **res, "sessions_revoked": revoked}
    finally:
        c.close()


def act_chat_group_save(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        r = manualdb.save_chat_group(c, body)
        manualdb.invalidate_master()      # 群的默认起运港参与归一兜底
        return {"ok": True, **r}
    finally:
        c.close()


def act_alias_list(q: Dict[str, str]) -> Dict[str, Any]:
    from core.contracts import ALIAS_PAGE_SIZE
    paged = "limit" in q or "offset" in q
    try:
        limit = int(q.get("limit") or ALIAS_PAGE_SIZE) if paged else None
        offset = int(q.get("offset") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "分页参数必须为整数"}
    c = conn()
    try:
        rows = manualdb.alias_scope_list(
            c, q.get("q") or None, q.get("group") or None, q.get("kind") or None,
            limit=limit, offset=offset
        )
        if paged:
            return {"ok": True, "data": rows}
        return {"ok": True, "rows": rows, "total": len(rows)}
    finally:
        c.close()


def act_alias_save(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        res = manualdb.alias_scope_save(c, body)
        key = res["key"]
        log("INFO", f"别名 {res['action']} {key['alias']}·{key['kind']}·{key['scope_group']} → {body.get('target')}")
        return res
    finally:
        c.close()


def act_alias_delete(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        res = manualdb.alias_scope_delete(
            c,
            str(body.get("alias") or "").strip(),
            str(body.get("kind") or "").strip(),
            str(body.get("scope_group") or "*").strip() or "*",
        )
        if res["ok"]:
            k = res["key"]
            log("INFO", f"删除别名 {k['alias']}·{k['kind']}·{k['scope_group']}")
        return res
    finally:
        c.close()


def act_shadow_replay(body: Dict[str, Any]) -> Dict[str, Any]:
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids 非空数组必填")
    try:
        ids_int = [int(x) for x in ids]
    except (TypeError, ValueError):
        raise ValueError("ids 必须是整数数组")
    modes = body.get("modes") or ["baseline", "no_alias"]
    if not isinstance(modes, list) or not modes:
        raise ValueError("modes 非空数组")
    c = conn()
    try:
        master = manualdb.get_master(c)
        res = manualdb.shadow_replay(c, master, ids_int, modes)
        log("INFO", f"影子重放 {res['replayed']} 条（跳过 {res['skipped']}） modes={modes}")
        return {"ok": True, **res}
    finally:
        c.close()


def act_shadow_list(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        pid = q.get("pending_id") or None
        mode = q.get("mode") or None
        limit = int(q.get("limit") or "200")
        rows = manualdb.shadow_list(c, int(pid) if pid else None, mode, limit)
        return {"ok": True, "rows": rows, "total": len(rows)}
    finally:
        c.close()


def act_shadow_clear(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        res = manualdb.shadow_clear(
            c, body.get("older_than") or None, body.get("mode") or None
        )
        log("INFO", f"清理影子重放 {res['deleted']} 条")
        return res
    finally:
        c.close()


def act_candidates_list(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        status = q.get("status") or "pending"
        source = q.get("source") or None
        limit = int(q.get("limit") or "200")
        rows = manualdb.candidates_list(c, status, source, limit)
        return {"ok": True, "rows": rows, "total": len(rows)}
    finally:
        c.close()


_SPEC_LOG_LOCK = RLock()


def _group_spec_path(group_name):
    package = HERE.parent / "Skill" / "freight-rate-heartbeat"
    root = package / "references" / "群聊规范"
    configured = package / "scripts" / "config" / "chat_sources.json"
    relative = None
    if configured.exists():
        try:
            groups = json.loads(configured.read_text(encoding="utf-8")).get("groups", [])
            relative = next((group.get("spec_dir") for group in groups if group.get("name") == group_name), None)
        except (OSError, ValueError, TypeError) as error:
            log("WARN", "读取群规范目录配置失败：" + str(error))
            return None
    if not group_name:
        return None
    spec = (package / relative / "规范.md") if relative else (root / group_name.replace('/', '+') / "规范.md")
    return spec if spec.resolve().is_relative_to(root.resolve()) else None


def _append_spec_log(group_name: str, line: str) -> bool:
    with _SPEC_LOG_LOCK:
        return _append_spec_log_unlocked(group_name, line)


def _append_spec_log_unlocked(group_name: str, line: str) -> bool:
    """P3-B：候选批准后往对应群规范.md 追加登记日志（transport 层做文件 IO，core 不碰）。
    群名→目录名规则：钉钉群名里的 '/' 在目录名里是 '+'（README 表约定）。失败只 WARN 不阻断。"""
    try:
        spec = _group_spec_path(group_name)
        if not spec or not spec.exists():
            return False
        dir_name = spec.parent.name
        text = spec.read_text(encoding='utf-8')
        marker = "## 别名登记日志"
        entry = f"- {line}\n"
        if marker in text:
            text = text.rstrip() + "\n" + entry
        else:
            text = text.rstrip() + f"\n\n{marker}\n\n<!-- 候选管理页签批准 alias_scope 别名时自动追加，人工可修订 -->\n\n" + entry
        spec.write_text(text, encoding='utf-8')
        _sync_spec_to_runtime(dir_name, spec)
        return True
    except Exception as e:
        log("WARN", f"追加规范日志失败（不阻断批准）：{e}")
        return False


def _sync_spec_to_runtime(dir_name: str, src_spec) -> bool:
    """把源目录的群规范.md 同步到采集器运行副本（~/.qwenworkcn/skills/freight-rate-heartbeat/）。
    采集器每轮从运行副本读群规范（换机器只改锚点，副本在 QwenWork 的 skills 目录）；
    只写源目录的话，刚追加的缩写表采集器读不到。失败只 WARN 不阻断批准。"""
    try:
        import shutil
        dst = Path.home() / ".qwenworkcn" / "skills" / "freight-rate-heartbeat" \
              / "references" / "群聊规范" / dir_name / "规范.md"
        if dst.parent.exists():
            shutil.copyfile(src_spec, dst)
            return True
    except Exception as e:
        log("WARN", f"同步规范到运行副本失败（不阻断批准）：{e}")
    return False


def _append_spec_abbrev(group_name: str, alias: str, target: str, kind: str) -> bool:
    """P3-B2：候选批准后把「写法→归一目标」追加进该群规范.md 的缩写表。
    缩写表是机器可读强约束（spec_index.py 可解析、check_llm_out 可校验、LLM 直接引用），
    比「别名登记日志」列表更有效——AI 下轮读到就直接照规范写，从源头减少识别错。
    同写法+同归一+同作用域已存在则跳过；失败只 WARN 不阻断批准。"""
    kind_cn = {"pol": "起运港", "pod": "目的港", "carrier": "船司", "route": "航线",
               "ctype": "柜型", "space_type": "舱位类型"}.get(str(kind or "").strip(),
                                                             str(kind or "").strip())
    try:
        spec = _group_spec_path(group_name)
        if not spec or not spec.exists():
            return False
        dir_name = spec.parent.name
        alias = (alias or "").strip()
        target = (target or "").strip()
        if not alias or not target:
            return False
        row = f"| `{alias}` | {target} | `{target}` | {kind_cn} |"
        text = spec.read_text(encoding='utf-8')
        # 已存在同写法+同归一+同作用域 → 跳过
        for ln in text.split("\n"):
            if ln.lstrip().startswith("|") and f"`{alias}`" in ln \
                    and f"`{target}`" in ln and ln.rstrip().endswith(f"| {kind_cn} |"):
                return True
        lines = text.split("\n")
        sec = -1
        for i, ln in enumerate(lines):
            if ln.startswith("## ") and "缩写表" in ln:
                sec = i
                break
        if sec < 0:
            # 无缩写表小节 → 文件末尾新建标准小节（标题+表头+分隔行+数据行）
            lines.append(f"\n## 缩写表（作用域＝本群 + 该列）\n\n"
                         f"| 写法 | 本群含义 | 归一目标 | 作用域 |\n|---|---|---|---|\n{row}")
        else:
            # 缩写表小节内：找到最后一行表格行，在其后插入（跳过表头与分隔行之间的空行）
            at = sec
            for j in range(sec + 1, len(lines)):
                if lines[j].lstrip().startswith("|"):
                    at = j
                elif not lines[j].strip():
                    continue
                else:
                    break
            lines.insert(at + 1, row)
        spec.write_text("\n".join(lines).rstrip() + "\n", encoding='utf-8')
        _sync_spec_to_runtime(dir_name, spec)
        return True
    except Exception as e:
        log("WARN", f"追加规范缩写表失败（不阻断批准）：{e}")
        return False


def act_candidates_approve(body: Dict[str, Any]) -> Dict[str, Any]:
    cand_id = int(body.get("id") or 0)
    reviewer = str(body.get("_actor") or body.get("reviewer") or "unknown").strip()
    override_group = body.get("override_scope_group") or None
    if not cand_id:
        raise ValueError("id 必填")
    c = conn()
    try:
        res = manualdb.candidates_approve(c, cand_id, reviewer, override_group)
        log("INFO", f"候选 #{cand_id} 批准 by {reviewer} → {res.get('layer_action', {}).get('action')}")
        # P3-B：alias_scope 层候选批准后往群规范.md 追加登记日志（agent 下轮读规范时可见）
        ci = res.get("candidate") or {}
        # Isolated databases are used by E2E tests and recovery drills.  Their
        # candidate decisions must never rewrite the production skill sources.
        is_primary_db = Path(CFG["db"]).resolve() == manualdb.DEFAULT_DB.resolve()
        if (res.get("ok") and ci.get("target_layer") == "alias_scope" and is_primary_db
                and res.get('layer_action', {}).get('action') != 'context_alias_saved'):
            parts = [p.strip() for p in str(ci.get("scope_key") or "").split("|")]
            group = parts[-1] if parts else ""
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            _append_spec_log(group,
                             f"{ts} [别名登记] `{ci.get('alias')}` → `{ci.get('suggested_target')}`"
                             f"（{ci.get('kind')}，候选#{cand_id} 批准 by {reviewer}）")
            # P3-B2：同步把写法写进缩写表（机器可读强约束，AI 下轮直读直写）
            _append_spec_abbrev(group, ci.get("alias"), ci.get("suggested_target"), ci.get("kind"))
        if res.get("ok"):
            growth = award_growth_safe(c, body, GROWTH_GOVERNANCE_EVENT,
                                       [(GROWTH_GOVERNANCE_ITEM_EVENT, f"candidate:{cand_id}",
                                         GROWTH_GOVERNANCE_POINTS)])
            if growth and growth["awarded"]:
                res["growth_award"] = growth
        return res
    finally:
        c.close()


def act_candidates_reject(body: Dict[str, Any]) -> Dict[str, Any]:
    cand_id = int(body.get("id") or 0)
    reviewer = str(body.get("_actor") or body.get("reviewer") or "unknown").strip()
    note = str(body.get("note") or "").strip()
    if not cand_id:
        raise ValueError("id 必填")
    c = conn()
    try:
        res = manualdb.candidates_reject(c, cand_id, reviewer, note)
        if res.get("ok"):
            log("INFO", f"候选 #{cand_id} 驳回 by {reviewer}")
            growth = award_growth_safe(c, body, GROWTH_GOVERNANCE_EVENT)
            if growth and growth["awarded"]:
                res["growth_award"] = growth
        return res
    finally:
        c.close()


def act_candidates_scan(body: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(body.get("limit") or 2000)
    cutoff = float(body.get("cutoff") or 0.75)
    c = conn()
    try:
        res_diff = manualdb.scan_failed_for_candidates(c, limit=limit, cutoff=cutoff)
        res_audit = manualdb.scan_audit_corrections(
            c, min_count=int(body.get("min_count") or 3),
            min_span_days=int(body.get("min_span_days") or 2))
        log("INFO", f"扫描候选：difflib 新建 {res_diff['candidates_created']} 累加 {res_diff['candidates_updated']}｜"
                    f"audit 新建 {res_audit['candidates_created']} 累加 {res_audit['candidates_updated']} "
                    f"低于门槛 {res_audit['below_threshold']}")
        return {"ok": True, "difflib": res_diff, "audit": res_audit}
    finally:
        c.close()


def act_queue_stats(q: Dict[str, str]) -> Dict[str, Any]:
    """队列红点计数：待审核(rates/space)、港群建议、候选待批准——前端红点/角标一次拉全。"""
    c = conn()
    try:
        scope_sql = manualdb.scope_clause(c, q.get("viewer", ""), "json_extract(p.payload,'$.来源群')", "json_extract(p.payload,'$.航线')")
        scope_sql = " AND " + scope_sql if scope_sql else ""
        pa = c.execute("SELECT COUNT(*) FROM pending_records p "
                       "WHERE review_status='awaiting' AND kind IN ('rates','space')" + scope_sql).fetchone()[0]
        sug = c.execute("SELECT COUNT(*) FROM pending_records p "
                        "WHERE review_status='awaiting' AND kind='group_suggestion'" + scope_sql).fetchone()[0]
        cand = c.execute("SELECT COUNT(*) FROM rule_candidates "
                         "WHERE status='pending'").fetchone()[0] if not q.get("viewer") else 0
        return {"ok": True, "pending_awaiting": pa, "sugg_awaiting": sug,
                "candidates_pending": cand}
    finally:
        c.close()


def act_audit_hints(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        gs = None
        if q.get("groups"):
            gs = [g for g in q["groups"].split(",") if g.strip()]
        return manualdb.audit_hints(
            c, q.get("group") or None, q.get("sender") or None, q.get("carrier") or None,
            limit=int(q.get("limit") or 500), groups=gs, viewer=q.get("viewer", ""))
    finally:
        c.close()


def act_group_stats(q: Dict[str, str]) -> Dict[str, Any]:
    raw_hours = q.get("hours")
    hours = int(raw_hours) if raw_hours else None
    if hours not in (None, 12, 24, 48, 168):
        raise ValueError("hours must be one of 12, 24, 48, 168")
    c = conn()
    try:
        return manualdb.group_accuracy_stats(c, hours, viewer=q.get("viewer", ""))
    finally:
        c.close()


def act_memory_lookup(q: Dict[str, str]) -> Dict[str, Any]:
    """记忆层：返回本群历史人工纠正对（高频重复错误），供 agent 解析前参考。
    入参：group（必填）；可选 carrier/pod/sender 进一步过滤上下文。
    返回：corrections（按频次排的纠正对）+ field_defaults（本群该字段人工最常填的值）。
    """
    group = (q.get("group") or "").strip()
    if not group:
        raise ValueError("group required")
    carrier = (q.get("carrier") or "").strip()
    pod = (q.get("pod") or "").strip()
    c = conn()
    try:
        rows = c.execute(
            "SELECT payload, edit_log FROM pending_records "
            "WHERE review_status='approved' AND edit_log IS NOT NULL AND edit_log NOT IN ('','[]')"
        ).fetchall()
    finally:
        c.close()

    from collections import Counter
    corr_counter = Counter()   # (field, old, new) -> count
    field_new_counter = Counter()  # (field, new) -> count
    field_total = Counter()    # field -> 总修改次数
    for r in rows:
        try:
            p = json.loads(r['payload'] or '{}')
        except Exception:
            continue
        if str(p.get('来源群') or '').strip() != group:
            continue
        if carrier and str(p.get('船司') or '').strip() != carrier:
            continue
        if pod and str(p.get('目的港') or '').strip() != pod:
            continue
        try:
            logs = json.loads(r['edit_log'] or '[]')
        except Exception:
            continue
        for e in logs:
            if e.get('action') not in ('update', 'approve_diff') or not e.get('field'):
                continue
            f = str(e['field']).strip()
            old = str(e.get('old') or '').strip()
            new = str(e.get('new') or '').strip()
            if not new:
                continue
            corr_counter[(f, old, new)] += 1
            field_new_counter[(f, new)] += 1
            field_total[f] += 1

    # 高频纠正对（>=2 次才算重复模式）
    corrections = []
    for (f, old, new), n in corr_counter.most_common(30):
        if n < 2:
            continue
        corrections.append({
            "field": f,
            "old": old[:40],
            "new": new[:60],
            "count": n,
            "pattern": ("空→" if not old else f'"{old[:20]}"→') + f'"{new[:30]}"'
        })

    # 字段默认值：本群该字段人工最常填的值（>=2 次）
    field_defaults = {}
    for (f, new), n in field_new_counter.most_common(50):
        if n < 2:
            continue
        if f not in field_defaults:
            field_defaults[f] = {"value": new[:60], "count": n}

    return {
        "ok": True,
        "group": group,
        "corrections": corrections,
        "field_defaults": field_defaults,
        "total_fields_edited": sum(field_total.values()),
    }


def act_named_delete(table: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handler(body: Dict[str, Any]) -> Dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("name required")
        c = conn()
        try:
            if table == 'operators':
                target = c.execute('SELECT user_id,publisher_name FROM operators WHERE name=?',(name,)).fetchone()
                if target:
                    if (target['publisher_name'] or name) in confirmed_people():
                        raise ValueError("该人员仍有已确认的报价发布身份，请先在报价发布人中恢复识别")
                    admin_accounts.record_event(c,target[0],'account_deleted',body.get('_actor') or '管理员',body.get('_actor_id') or 'system',{'name':name},manualdb.now_cn())
            n = manualdb.delete_named(c, table, name)
            if table == "operators":
                c.execute("DELETE FROM auth_sessions WHERE operator_name=?", (name,))
                c.commit()
            manualdb.invalidate_master()
            log("WARN", f"删除 {table} {name}")
            return {"ok": bool(n), "deleted": n} if n else {"ok": False, "error": "not found"}
        finally:
            c.close()
    return handler


def act_groups_import(body: Dict[str, Any]) -> Dict[str, Any]:
    groups = body.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("请内联传 groups 数组")
    c = conn()
    try:
        return {"ok": True, **manualdb.import_chat_groups(c, groups)}
    finally:
        c.close()


def act_sync(body: Dict[str, Any]) -> Dict[str, Any]:
    table = body.get("table")
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows empty")
    if len(rows) > 20000:
        raise ValueError("rows > 20000 per request")
    c = conn()
    try:
        n = manualdb.sync_rows(c, table, rows)
        log("INFO", f"sync {table} {n} 条")
        return {"ok": True, "table": table, "accepted": n}
    finally:
        c.close()


# ---------------- 校验 / 体检 / 待审核 ----------------

def act_validate(body: Dict[str, Any]) -> Dict[str, Any]:
    """预检（只读）：对一批中文字段记录跑机械校验，返回每行 {ok, resolved, errors}。"""
    kind = _kind(body)
    records = body.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records empty")
    c = conn()
    try:
        m = manualdb.get_master(c)
        results = [manualdb.validate_record(c, m, kind, rec) for rec in records]
        bad = sum(1 for r in results if not r["ok"])
        return {"ok": True, "table": kind, "results": results,
                "good": len(results) - bad, "bad": bad}
    finally:
        c.close()


def act_audit(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        m = manualdb.get_master(c)
        res = manualdb.audit_all(c, m, viewer=q.get("viewer", ""))
        res["master_health"] = manualdb.audit_master(c, m)   # 字典自身有没有洞：成员断链/死别名/重名/坏数据
        return res
    finally:
        c.close()


def act_pending(body: Dict[str, Any]) -> Dict[str, Any]:
    kind = _kind(body)
    records = body.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records empty")
    if len(records) > 1000:
        raise ValueError("records > 1000 per request")
    origin = body.get("origin") or "agent"
    # P3 价格向量纠错：只在 agent 心跳路径生效（人工 push 自己会看，不打扰）
    # 批内互相比对，超区间且删一位命中中位数的 → 挂 payload 独立键"价格建议"给审核员看。
    # 绝不写进"备注"：备注映射 remark 参与 content_key 指纹，带/不带标签会让同一报价
    # 指纹不同 → upsert 变重复插行，且提示文本会永久落业务表（2026-09-17 审查修复）。
    if origin == "agent":
        try:
            price_sanitize.annotate_batch(records)
        except Exception as e:
            # annotate 是旁路增强，失败不阻断入库（AGENTS.md：空 catch 必须注释原因）
            log("WARN", f"价格纠错 annotate 异常（不阻断入库）：{e}")
        for r in records:
            sug = r.pop("_price_suggestion", None)   # pop 在 try 外：异常时内部键也不进 payload
            if sug and sug.get("suggested") is not None:
                r["价格建议"] = (f"建议 {sug['suggested']}｜置信 {sug['confidence']:.2f}｜"
                                f"{sug.get('reason') or ''}")
    c = conn()
    try:
        inherited = manualdb.inherit_message_fields(records)   # 只补空：同消息只写一次的条款字段别只挂在一行上
        if inherited:
            log("INFO", f"消息级字段补齐 {len(inherited)} 处："
                        + "；".join(f"{i['来源群'] or '—'}/{i['柜型'] or '—'} {i['字段']}={i['值']}" for i in inherited[:5]))
        master = manualdb.get_master(c)
        preserve_other_sources = body.get(PENDING_PRESERVE_OTHER_SOURCES,
            origin == 'agent' and all(r.get('来源群') and r.get('消息ID') for r in records))
        preserve_existing_source_rows = body.get(PENDING_PRESERVE_EXISTING_SOURCE_ROWS, False)
        preserved_ids = []
        ids = manualdb.stage_pending(c, master, kind, records, origin=origin, viewer=body.get("_scope_viewer") or "",
                                     preserve_other_sources=preserve_other_sources,
                                     preserve_existing_source_rows=preserve_existing_source_rows,
                                     preserved_ids=preserved_ids)
        # Normalize the retained request with the same snapshot (including carrier
        # normalization learned by stage_pending), never infer expectations from ids.
        expected = [one for record in records for one in master.expand_cn(record)] if kind in ('rates','space') else records
        if kind in ('rates', 'space'):
            expected = [dict(record, **(manualdb.validate_record(c, master, kind, record).get('resolved') or {})) for record in expected]
        entry_ids = []
        validation_failures = []
        staged_rows = []
        covered_ids = list(dict.fromkeys([*ids, *preserved_ids]))
        if covered_ids:
            marks = ",".join("?" for _ in covered_ids)
            staged_rows = c.execute(f"SELECT id,entry_id,kind,payload,check_status,errors FROM pending_records WHERE id IN ({marks}) ORDER BY id", covered_ids).fetchall()
            entry_ids = [r["entry_id"] for r in staged_rows]
            validation_failures = [{"pending_id": r["id"], "entry_id": r["entry_id"], "kind": r["kind"],
                "record": json.loads(r["payload"]), "errors": json.loads(r["errors"] or "[]")}
                for r in staged_rows if r["check_status"] != "passed"]
        from services.submission_coverage import compare_submission
        actual = []
        for row in staged_rows:
            payload = json.loads(row['payload'])
            comparable = dict(payload)
            if kind in ('rates', 'space'):
                comparable.update(manualdb.validate_record(c, master, kind, payload).get('resolved') or {})
            actual.append({'entry_id':row['entry_id'], 'payload':payload, 'comparison_payload':comparable})
        coverage = compare_submission(kind, expected, actual)
        log("INFO" if coverage['ok'] else 'WARN',
            f"待审核暂存 {kind} 新增={len(ids)} 保留={len(preserved_ids)} origin={origin} missing={len(coverage['missing'])}")
        return {"ok": True, "coverage": coverage, "staged": len(ids), "ids": ids,
                "preserved": len(preserved_ids), "preserved_ids": preserved_ids,
                "covered_ids": covered_ids, "entry_ids": entry_ids, "inherited": inherited,
                "check_failed": len(validation_failures),
                "check_passed": len(covered_ids) - len(validation_failures), "validation_failures": validation_failures}
    finally:
        c.close()


def act_review_issues_list(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        result = manualdb.list_review_issues(c, q.get("status") or "open", int(q.get("limit") or 200))
        for row in result["rows"]:
            row["images"] = images_for({"图片名": ",".join(row["media_ids"]), "消息原文": row["message_text"]})
        return ok(result)
    finally:
        c.close()


def act_review_issues_submit(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        return ok(manualdb.upsert_review_issues(c, body.get("issues")))
    finally:
        c.close()


def act_review_issues_resolve(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        return ok(manualdb.resolve_review_issue(c, str(body.get("source_group") or ""),
                                               str(body.get("message_id") or ""),
                                               str(body.get("_actor") or body.get("reviewer") or "")))
    finally:
        c.close()


def act_review_issues_settle(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        return ok(manualdb.settle_review_issue(c, body))
    finally:
        c.close()


def act_pending_list(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        if q.get("view") == "grouped":
            res = manualdb.list_pending_grouped(
                c, q.get("status") or "awaiting", q.get("kind") or "",
                int(q.get("limit") or 200), int(q.get("offset") or 0), q.get("check") or "", q.get("entry_id") or "", q.get("message_id") or "", q.get(OPS_ID_QUERY_PARAM) or "", viewer=q.get("viewer") or "")
            for g in res.get("groups", []):
                images = images_for(g.get("meta") or {})
                g["meta"]["images"] = images
            return {"ok": True, **res}
        list_records = manualdb.list_pending_sources if q.get("view") == PENDING_SOURCE_VIEW else manualdb.list_pending
        bulk_options = ({"whole_passed": q.get(PENDING_WHOLE_PASSED_PARAM) == "1"}
                        if list_records is manualdb.list_pending else {})
        res = list_records(
            c, q.get("status") or "awaiting", q.get("kind") or "",
            int(q.get("limit") or 200), int(q.get("offset") or 0), q.get("check") or "",
            entry_id=q.get("entry_id") or "", message_id=q.get("message_id") or "", id_query=q.get(OPS_ID_QUERY_PARAM) or "", viewer=q.get("viewer") or "", **bulk_options)
        # 待审核卡片要能对照原图：payload 里的图片引用走销售端同一解析（单一真源）
        for row in res.get("rows", []):
            row["images"] = images_for(row.get("payload") or {})
            # orig_payload 是审计与反向沉淀的数据源，看板一次都不读（50 行里占 53KB）；
            # 报表脚本走自己的 sqlite 连接，摘掉它不影响它们，只给列表瘦身。edit_log 小且留痕核对要读，保留。
            row.pop("orig_payload", None)
        return {"ok": True, **res}
    finally:
        c.close()


def act_pending_regroup(body: Dict[str, Any]) -> Dict[str, Any]:
    """一次性复原被「组完整性」连坐标记污染的判定（剥标记 + 还原各行自己的 check_status）。
    整组归属现在由读取路径派生，不再需要重算写库；默认 dry_run 只报会动多少条。"""
    dry = bool(body.get("dry_run", True))
    c = conn()
    try:
        res = manualdb.group_mark_cleanup(c, dry_run=dry)
        log("INFO", "连坐标记清理%s：扫 %d 行 / 剥标记 %d / 还原判定 %d"
            % ("(预览)" if dry else "", res["scanned"], res["marks_stripped"], res["status_restored"]))
        return res
    finally:
        c.close()


def act_pending_approve_all(body: Dict[str, Any]) -> Dict[str, Any]:
    c = conn()
    try:
        res = manualdb.approve_all_pending(c, manualdb.get_master(c),
                                           str(body.get("kind") or ""),
                                           str(body.get("_actor") or body.get("reviewed_by") or ""), viewer=body.get("_scope_viewer") or "")
        log("INFO", f"一键放行：命中 {res['total']} 条，成功 {res['approved']}，跳过 {len(res['skipped'])}")
        if res.get("approved"):
            subjects = [(GROWTH_APPROVAL_EVENT, f"pending:{pid}", GROWTH_APPROVAL_POINTS)
                        for pid in res.get("approved_ids", [])]
            growth = award_growth_safe(c, body, GROWTH_REVIEW_EVENT, subjects,
                                       activity_daily_cap=None)
            if growth and growth["awarded"]:
                res["growth_award"] = growth
        return res
    finally:
        c.close()


def act_pending_approve(body: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    c = conn()
    try:
        res = manualdb.approve_pending(c, manualdb.get_master(c), pid,
                                       str(body.get("_actor") or body.get("reviewed_by") or ""), viewer=body.get("_scope_viewer") or "")
        if res.get("ok"):
            log("INFO", f"待审核放行 id={pid} kind={res['kind']}")
            corrected = bool(res.get("audit_diffs"))
            growth = award_growth_safe(c, body, GROWTH_REVIEW_EVENT, [(
                GROWTH_CORRECTION_EVENT if corrected else GROWTH_APPROVAL_EVENT,
                f"pending:{pid}", GROWTH_CORRECTION_POINTS if corrected else GROWTH_APPROVAL_POINTS)])
            if growth and growth["awarded"]:
                res["growth_award"] = growth
        return res
    finally:
        c.close()


def act_pending_update(body: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("fields required")
    actor = str(body.get("_actor") or body.get("reviewer") or body.get("reviewed_by") or "").strip()
    c = conn()
    try:
        res = manualdb.update_pending(c, manualdb.get_master(c), pid, fields, body.get("kind"), actor=actor)
        if res.get("ok"):
            log("INFO", f"待审核编辑 id={pid} kind={res.get('kind')} check={res.get('check_status')} "
                        f"action={res.get('action')} by={actor or 'anon'} edits={res.get('edits_recorded', 0)}")
            for rj in (res.get("rejected") or []):    # 看着像改成功了，其实被回退：必须留痕
                log("WARN", f"强匹配回退 id={pid} {rj.get('field')}「{rj.get('input')}」未写入，"
                            f"保留原值「{rj.get('kept')}」by={actor or 'anon'}")
        return res
    finally:
        c.close()


def act_pending_sync(body: Dict[str, Any]) -> Dict[str, Any]:
    """队列批量同步：把锚点条目上的一次字段修改，同步到"同字段且旧值相同"的其他待审条目。
    preview=True → 只回报候选（改了哪些、多少条、每条摘要），不写库；
    preview 缺省 → 按前端确认过的 ids 执行（预览即所得，执行时逐条复核值没被别人改过）。
    只改值不放行：条目仍留待审队列，放行永远是人工动作。"""
    field = str(body.get("field") or "").strip()
    new = str(body.get("new") or "").strip()
    old = str(body.get("old") or "").strip()
    if field not in manualdb.SYNC_SAFE_FIELDS:
        return {"ok": False, "error": f"字段「{field or '空'}」不参与批量同步",
                "allowed": list(manualdb.SYNC_SAFE_FIELDS)}
    c = conn()
    try:
        anchor = int(body.get("anchor_id") or 0)
        if body.get("preview"):
            kind = str(body.get("kind") or "").strip()
            status = str(body.get("status") or "awaiting")
            check = str(body.get("check") or "")
            res = manualdb.scan_sync_targets(c, field, old, new, kind=kind,
                                             status=status, check=check, exclude_id=anchor, viewer=body.get("_scope_viewer") or "")
            if res.get("ok"):
                log("INFO", f"同步候选扫描 {field}「{old or '空'}」→「{new}」 kind={kind or '全部'} "
                            f"命中 {res['matched']} 条 by={str(body.get('reviewer') or 'anon')}")
            return res
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise ValueError("ids required")
        res = manualdb.sync_pending_field(c, manualdb.get_master(c), field, old, new, ids,
                                          actor=str(body.get("_actor") or body.get("reviewer") or ""), anchor_id=anchor,
                                          viewer=body.get("_scope_viewer") or "")
        if res.get("ok"):
            log("INFO", f"队列同步 {field}「{old or '空'}」→「{new}」 改 {res['synced']} 条 / "
                        f"跳 {len(res['skipped'])} 条 / 校验未过 {len(res['failed_check_ids'])} 条 "
                        f"锚点={anchor} by={str(body.get('reviewer') or 'anon')}")
        return res
    finally:
        c.close()


def act_pending_reject(body: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    c = conn()
    try:
        res = manualdb.reject_pending(c, pid, str(body.get("note") or ""),
                                      str(body.get("_actor") or body.get("reviewed_by") or ""))
        if res.get("ok"):
            log("WARN", f"待审核驳回（移入驳回日志）id={pid}")
            growth = award_growth_safe(c, body, GROWTH_REVIEW_EVENT)
            if growth and growth["awarded"]:
                res["growth_award"] = growth
        return res
    finally:
        c.close()


def act_pending_update_group(body: Dict[str, Any]) -> Dict[str, Any]:
    """合并视图：改整组共享字段（起运港/目的港/船司/航线/有效期/备注等），循环组内各柜型格。"""
    group_key = body.get("group_key")
    fields = body.get("fields")
    if not isinstance(group_key, dict) or not isinstance(fields, dict):
        raise ValueError("group_key and fields required")
    kind = str(body.get("kind") or "rates")
    actor = str(body.get("_actor") or body.get("reviewer") or body.get("reviewed_by") or "").strip()
    c = conn()
    try:
        res = manualdb.update_pending_group(c, manualdb.get_master(c), group_key,
                                            fields, kind=kind, actor=actor)
        log("INFO", f"合并视图改整组 kind={kind} 组数={res.get('total')} by={actor or 'anon'}")
        return {"ok": True, **res}
    finally:
        c.close()


def act_pending_approve_group(body: Dict[str, Any]) -> Dict[str, Any]:
    """合并视图：放行当前卡片内所有已通过条目。"""
    group_key = body.get("group_key")
    kind = str(body.get("kind") or "rates")
    reviewer = str(body.get("_actor") or body.get("reviewed_by") or "").strip()
    ids = body.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise ValueError("ids must be a list")
    if ids is None and not isinstance(group_key, dict):
        raise ValueError("group_key or ids required")
    c = conn()
    try:
        if ids is not None:
            res = manualdb.approve_pending_ids(c, manualdb.get_master(c), ids,
                                               kind=kind, reviewed_by=reviewer, viewer=body.get("_scope_viewer") or "")
        else:
            # 兼容旧页面；新版一律传明确的 ID 集合，避免首条组键缩窄范围。
            res = manualdb.approve_pending_group(c, manualdb.get_master(c), group_key,
                                                 kind=kind, reviewed_by=reviewer, viewer=body.get("_scope_viewer") or "")
        log("INFO", f"合并视图整组放行 kind={kind} 放行={len(res.get('approved', []))} "
                    f"跳过={len(res.get('skipped', []))} by={reviewer or 'anon'}")
        subjects = []
        for item in res.get("approved", []):
            item_result = item.get("res") or {}
            if not item_result.get("ok"):
                continue
            corrected = bool(item_result.get("audit_diffs"))
            subjects.append((GROWTH_CORRECTION_EVENT if corrected else GROWTH_APPROVAL_EVENT,
                             f"pending:{item['id']}",
                             GROWTH_CORRECTION_POINTS if corrected else GROWTH_APPROVAL_POINTS))
        payload = {"ok": True, **res}
        if subjects:
            growth = award_growth_safe(c, body, GROWTH_REVIEW_EVENT, subjects)
            if growth and growth["awarded"]:
                payload["growth_award"] = growth
        return payload
    finally:
        c.close()


def act_pending_reject_group(body: Dict[str, Any]) -> Dict[str, Any]:
    """按明确条目编号驳回；兼容旧页面组键。"""
    group_key = body.get("group_key")
    ids = body.get("ids")
    if ids is not None and (not isinstance(ids, list) or any(type(pid) is not int or pid <= 0 for pid in ids)):
        raise ValueError("ids must be a list of positive integers")
    if ids is None and not isinstance(group_key, dict):
        raise ValueError("group_key or ids required")
    kind = str(body.get("kind") or "rates")
    note = str(body.get("note") or "")
    c = conn()
    try:
        reviewer = str(body.get("_actor") or body.get("reviewed_by") or "")
        if ids is not None:
            res = manualdb.reject_pending_ids(c, ids, kind=kind, note=note, reviewed_by=reviewer)
        else:
            res = manualdb.reject_pending_group(c, group_key, kind=kind, note=note, reviewed_by=reviewer)
        log("WARN", f"合并视图整组驳回 kind={kind} 组数={len(res.get('rejected', []))}")
        payload = {"ok": True, **res}
        if res.get("rejected"):
            growth = award_growth_safe(c, body, GROWTH_REVIEW_EVENT)
            if growth and growth["awarded"]:
                payload["growth_award"] = growth
        return payload
    finally:
        c.close()


def act_agent_pending_suppress(body: Dict[str, Any]) -> Dict[str, Any]:
    """Move only awaiting Agent rows from non-publisher identities to reject log."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or any(type(pid) is not int or pid <= 0 for pid in ids):
        raise ValueError("ids must be a non-empty list of positive integers")
    note = str(body.get("note") or "").strip()
    if not note.startswith("publisher-filter:"):
        raise ValueError("publisher-filter note required")
    audit = str(body.get("identity_audit") or "").strip()
    if not audit:
        raise ValueError("identity audit reference required")
    c = conn()
    try:
        marks = ",".join("?" for _ in ids)
        rows = c.execute(f"SELECT id,origin,review_status FROM pending_records WHERE id IN ({marks})", ids).fetchall()
        if len(rows) != len(set(ids)) or any(r["origin"] != "agent" or r["review_status"] != "awaiting" for r in rows):
            raise ValueError("only awaiting Agent rows may be suppressed")
        res = manualdb.reject_pending_ids(c, ids, kind=str(body.get("kind") or "rates"),
                                          note=note + " audit=" + audit,
                                          reviewed_by=str(body.get("_actor") or "agent"))
        log("WARN", f"航管身份过滤驳回 {len(res.get('rejected', []))} 条")
        return {"ok": True, **res}
    finally:
        c.close()


def act_pending_rework(body: Dict[str, Any]) -> Dict[str, Any]:
    """驳回回炉：采集 agent 按驳回原因改一版交回人工复核。护栏都在 manualdb.rework_pending 里。"""
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields required")
    c = conn()
    try:
        res = manualdb.rework_pending(c, manualdb.get_master(c), pid, fields,
                                      str(body.get("reason") or ""),
                                      str(body.get("_actor") or body.get("actor") or "采集agent"))
        if res.get("ok"):
            log("INFO", f"待审核返修 id={pid} → {res['status']} 第{res['round']}轮 "
                        f"改={','.join(d['field'] for d in res.get('changes') or [])}")
        return res
    finally:
        c.close()


def act_pending_restore(body: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    c = conn()
    try:
        res = manualdb.restore_pending(c, pid)
        if res.get("ok"):
            log("INFO", f"待审核恢复（撤回驳回）id={pid}")
        return res
    finally:
        c.close()


def act_pending_delete(body: Dict[str, Any]) -> Dict[str, Any]:
    ids = body.get("ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids or any(type(pid) is not int or pid <= 0 for pid in ids):
            raise ValueError("ids must be a non-empty list of positive integers")
    else:
        pid = int(body.get("id") or 0)
        if not pid:
            raise ValueError("id required")
    force = bool(body.get("force"))
    c = conn()
    try:
        if ids is not None:
            res = manualdb.delete_pending_ids(c, ids, kind=str(body.get("kind") or ""), force=force)
        else:
            res = manualdb.delete_pending(c, pid, force=force)
        if res.get("ok"):
            log("WARN", f"待审核物理删除（{'force' if force else '待审/驳回'}）ids={res.get('ids') or [pid]}")
        return res
    finally:
        c.close()


GET_ROUTES: Dict[str, Callable[[Dict[str, str]], Dict[str, Any]]] = {
    "/api/stats": lambda q: act_stats(),
    "/api/options": lambda q: act_options(),
    "/api/bootstrap": lambda q: act_bootstrap(q.get("_role") or ""),
    "/api/master": lambda q: act_master(q.get("full") == "1"),
    "/api/operators": lambda q: act_operators(),
    "/api/chat-groups": lambda q: act_chat_groups(),
    "/api/export": lambda q: act_export(),
    "/api/rates": lambda q: act_flat("rates", q),
    "/api/space": lambda q: act_flat("space", q),
    "/api/rates_grouped": lambda q: act_grouped("rates", q),
    "/api/space_grouped": lambda q: act_grouped("space", q),
    SALES_GROUP_SUMMARY_ROUTE: act_sales_groups,
    FEEDBACK_ROUTE: act_feedback_read,
    ACTIVITY_SUMMARY_ROUTE: act_activity_summary,
    ACTIVITY_EVENTS_ROUTE: act_activity_events,
    ACTIVITY_TIMELINE_ROUTE: act_activity_timeline,
    "/api/pending": lambda q: act_pending_list(q),
    REVIEW_ISSUES_ROUTE: act_review_issues_list,
    "/api/audit": lambda q: act_audit(q),
    "/api/alias": lambda q: act_alias_list(q),
    "/api/shadow": lambda q: act_shadow_list(q),
    "/api/queue-stats": act_queue_stats,
    "/api/candidates": lambda q: act_candidates_list(q),
    "/api/audit-hints": lambda q: act_audit_hints(q),
    "/api/group-stats": lambda q: act_group_stats(q),
    "/api/memory-lookup": lambda q: act_memory_lookup(q),
}
COMPANY_CREDENTIAL_STORE = CompanyCredentialStore()
COMPANY_TOKEN_REFRESHER = CompanyTokenRefresher(COMPANY_CREDENTIAL_STORE, logger=log)
def company_mapping_master(connection):
    master = manualdb.get_master(connection)
    return {"ports": master.ports, "carriers": master.carriers}


COMPANY_RATE_RUNTIME = CompanyRateRuntime(conn, credential_provider=COMPANY_CREDENTIAL_STORE.credentials,
                                       master_provider=company_mapping_master)
COMPANY_RATE_GET, COMPANY_RATE_POST = build_company_rate_routes(COMPANY_RATE_RUNTIME)
GET_ROUTES.update(COMPANY_RATE_GET)
COMPANY_CREDENTIAL_GET, COMPANY_CREDENTIAL_POST = build_company_credential_routes(
    COMPANY_CREDENTIAL_STORE, COMPANY_TOKEN_REFRESHER)
GET_ROUTES.update(COMPANY_CREDENTIAL_GET)
GET_ROUTES.update(build_system_get_routes(lambda: CFG["db"], DEPLOYMENT_SETTINGS,
                                          lambda _: database_probe()))
GET_ROUTES.update(build_monitoring_get_routes(heartbeat_runtime_dir))
NOTICE_GET, NOTICE_POST = build_notice_routes(
    conn, manualdb.now_cn, log, notifications.replace_notice_request,
    notifications.resolve_notice_request)
GET_ROUTES.update(NOTICE_GET)


def act_notification_action(target: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    if target.get('kind') != 'notice_pin_request' or target.get('entity_type') != 'notice':
        raise PermissionError('这条消息不能在此处理')
    return NOTICE_POST[NOTICE_PIN_ROUTE]({
        'id':target['entity_id'], 'revision':target['payload'].get('revision'),
        'action':target['action'], '_actor':body['_actor'], '_actor_id':body['_actor_id'],
        '_actor_display':body['_actor_display'], '_role':body['_role']})


NOTIFICATION_GET, NOTIFICATION_POST = build_notification_routes(
    conn, notifications, act_notification_action, manualdb.now_cn)
GET_ROUTES.update(NOTIFICATION_GET)
GET_ROUTES.update(build_api_monitor_routes(lambda: conn(), lambda: database_probe(),
                                           lambda: SERVER_STARTED_MONOTONIC))
GET_ROUTES.update(build_download_routes(DOWNLOAD_CATALOG))
def act_publisher_accounts() -> Dict[str, Any]:
    c = conn()
    try:
        operators = manualdb.list_operators(c)
    finally:
        c.close()
    return publisher_accounts(heartbeat_runtime_dir() / ".work", operators)


def act_publisher_decision(payload: Dict[str, Any]) -> Dict[str, Any]:
    account_conn = conn()
    try:
        operators = manualdb.list_operators(account_conn)
    finally:
        account_conn.close()
    result = publisher_decide(heartbeat_runtime_dir() / ".work", payload, operators)
    if result.get("ok") and result["data"]["action"] == "confirm":
        try:
            c = conn()
            try:
                created = manualdb.ensure_publisher_people(c, confirmed_people())
                result["data"]["account_created"] = created
                log("INFO", f"已确认发布人同步至账号库：新增 {created} 人")
            finally:
                c.close()
        except Exception as error:
            log("ERROR", f"发布人名单已保存，人员账号库同步待修复：{error}")
            result["data"]["account_sync_pending"] = True
    elif result.get("ok") and result["data"]["action"] == "exclude":
        try:
            c = conn()
            try:
                identity_keys = result["data"].get("identity_keys", [])
                removed = (remove_unopened_placeholder(
                    c, identity_keys, payload["_actor"], payload["_actor_id"], manualdb.now_cn())
                    if identity_keys else remove_unopened_placeholder_by_user_id(
                        c, result["data"].get("account_user_id", ""), payload["_actor"],
                        payload["_actor_id"], manualdb.now_cn()))
                result["data"]["account_removed"] = removed
                log("INFO", f"已排除发布人并清理未开通占位账号：{removed} 人")
            finally:
                c.close()
        except Exception as error:
            log("ERROR", f"发布人已排除，未开通占位账号清理待修复：{error}")
            result["data"]["account_cleanup_pending"] = True
    return result


PROFILE_GET, PROFILE_POST = build_profile_routes(
    lambda session, ip: account_selfservice.profile(conn, session, ip),
    lambda session, ip, body: account_selfservice.save_profile(conn, session, ip, body))

ACCOUNT_GET, ACCOUNT_POST = build_account_routes(
    lambda body: account_selfservice.register(conn, password_hash, manualdb.now_cn, body),
    lambda body: account_selfservice.forgot(conn, manualdb.now_cn, body),
    lambda body, session: account_selfservice.change_password(conn, password_matches, password_hash, manualdb.now_cn, body, session),
    lambda: account_selfservice.recovery_requests(conn))
GET_ROUTES.update(ACCOUNT_GET)

PUBLISHER_GET, PUBLISHER_POST = build_publisher_routes(
    act_publisher_accounts, act_publisher_decision)
GET_ROUTES.update(PUBLISHER_GET)
POST_ROUTES: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    **PUBLISHER_POST,
    SALES_GROUP_VISIT_ROUTE: act_sales_group_visit,
    FEEDBACK_ROUTE: act_feedback_create,
    FEEDBACK_DISMISS_ROUTE: act_feedback_dismiss,
    ACTIVITY_PAGE_OPEN_ROUTE: act_activity_page_open,
    ACTIVITY_QUOTE_USAGE_ROUTE: act_activity_quote_usage,
    "/api/push": act_push,
    "/api/validate": act_validate,
    "/api/record/update": act_update,
    "/api/record/delete": act_delete,
    "/api/record/revise": act_revise,
    "/api/master/save": act_master_save,
    "/api/master/delete": act_master_delete,
    "/api/alias/save": act_alias_save,
    "/api/alias/delete": act_alias_delete,
    "/api/shadow/replay": act_shadow_replay,
    "/api/shadow/clear": act_shadow_clear,
    "/api/candidates/approve": act_candidates_approve,
    "/api/candidates/reject": act_candidates_reject,
    "/api/candidates/scan": act_candidates_scan,
    "/api/operator/save": act_operator_save,
    "/api/operator/delete": act_named_delete("operators"),
    "/api/chat-group/save": act_chat_group_save,
    "/api/chat-group/delete": act_named_delete("chat_groups"),
    "/api/chat-groups/import": act_groups_import,
    "/api/sync": act_sync,
    "/api/pending": act_pending,
    REVIEW_ISSUES_ROUTE: act_review_issues_submit,
    REVIEW_ISSUES_RESOLVE_ROUTE: act_review_issues_resolve,
    AGENT_REVIEW_ISSUES_SETTLE_ROUTE: act_review_issues_settle,
    "/api/pending/approve": act_pending_approve,
    "/api/pending/approve_all": act_pending_approve_all,
    "/api/pending/regroup": act_pending_regroup,
    "/api/pending/update": act_pending_update,
    "/api/pending/sync": act_pending_sync,
    "/api/pending/reject": act_pending_reject,
    "/api/pending/rework": act_pending_rework,
    "/api/pending/restore": act_pending_restore,
    "/api/pending/delete": act_pending_delete,
    "/api/pending/update_group": act_pending_update_group,
    "/api/pending/approve_group": act_pending_approve_group,
    PENDING_REJECT_GROUP_ROUTE: act_pending_reject_group,
    AGENT_PENDING_SUPPRESS_ROUTE: act_agent_pending_suppress,
}
POST_ROUTES.update(COMPANY_RATE_POST)
POST_ROUTES.update(COMPANY_CREDENTIAL_POST)
POST_ROUTES.update(build_release_post_routes(DEPLOYMENT_SETTINGS))
POST_ROUTES.update(NOTICE_POST)
POST_ROUTES.update(NOTIFICATION_POST)
from transport.master_resolution_routes import build_master_resolution_routes
POST_ROUTES.update(build_master_resolution_routes(conn, manualdb.get_master))


def _agent_master_knowledge(result, body):
    row = body["row"]
    if body["entity"] == "alias":
        detail = f"`{row['alias']}` → `{row['target']}`（{row['kind']}，群内作用域）"
    else:
        detail = f"港口 `{row['name']}`（{row['country']}）"
    references = "；".join(e["reference"] for e in body["evidence"])
    with _SPEC_LOG_LOCK:
        spec = _group_spec_path(body["source_group"])
        try:
            if spec and spec.exists() and detail in spec.read_text(encoding="utf-8"):
                return True
        except OSError as error:
            log("WARN", "Agent知识同步检查失败：" + str(error))
        return _append_spec_log(body["source_group"], f"{manualdb.now_cn()} [Agent预检修复] {detail}；来源消息 {body['message_id']}；依据 {references}")


POST_ROUTES.update(build_agent_post_routes(lambda: conn(), manualdb, log, _agent_master_knowledge))
POST_ROUTES.update(build_agent_intake_route(act_pending))
ADMIN_TERMINAL_GET, ADMIN_TERMINAL_POST = build_admin_routes(lambda:conn(), admin_accounts, confirmed_people, password_hash, manualdb.now_cn)
GET_ROUTES.update(ADMIN_TERMINAL_GET)
POST_ROUTES.update(ADMIN_TERMINAL_POST)

class FreightHTTPServer(ThreadingHTTPServer):
    """Threaded LAN server with a backlog suitable for a sales-page burst."""

    daemon_threads = True
    request_queue_size = SERVER_REQUEST_QUEUE_SIZE

    def __init__(self, *args, **kwargs):
        self.work_slots = BoundedSemaphore(HTTP_MAX_ACTIVE_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        with self.work_slots:
            super().process_request_thread(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "FreightManual/2.0"

    def setup(self):
        self.request.settimeout(HTTP_REQUEST_TIMEOUT_SECONDS)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def _begin_api_monitor(self, method: str, path: str) -> None:
        if (not path.startswith("/api/") or path in API_MONITOR_ROUTES
                or path in (LIVENESS_ROUTE, SYSTEM_HEALTH_ROUTE, SYSTEM_READINESS_ROUTE, DOWNLOAD_MANIFEST_ROUTE)):
            self._api_monitor_context = None
            return
        self._api_monitor_context = {
            "request_id": secrets.token_hex(16), "method": method,
            "path": path, "started": time.monotonic(), "identity": None,
            "recorded": False,
        }

    def _set_api_monitor_identity(self, identity) -> None:
        context = getattr(self, "_api_monitor_context", None)
        if not context or not identity:
            return
        context["identity"] = {
            "user_id": identity.get("user_id"),
            "role": str(identity.get("role") or ""),
        }

    def _record_api_response(self, status_code: int) -> None:
        context = getattr(self, "_api_monitor_context", None)
        if not context or context["recorded"]:
            return
        context["recorded"] = True
        identity = context.get("identity") or {}
        connection = None
        try:
            connection = conn()
            api_monitor.record_request(
                connection, context["request_id"], context["method"], context["path"],
                status_code, round((time.monotonic() - context["started"]) * 1000),
                identity.get("user_id"), identity.get("role", ""), self.client_address[0],
            )
        except Exception as error:  # noqa: BLE001 - telemetry must not change the user response
            log("WARN", f"接口监控记录失败 {context['method']} {context['path']}: {type(error).__name__}")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as error:  # noqa: BLE001 - closing telemetry storage must not interrupt the response
                    log("WARN", f"接口监控连接关闭失败: {type(error).__name__}")

    def _send(self, code: int, body: bytes, ctype: str, headers: Dict[str, str] | None = None) -> None:
        self._record_api_response(code)
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # The client disconnected; retrying an error response cannot reach it.

    def _json(self, code: int, obj: Any, headers: Dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    def _send_file(self, path: Path, ctype: str, headers: Dict[str, str] | None = None,
                   head_only: bool = False) -> None:
        self._record_api_response(200)
        try:
            with path.open("rb") as source:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(path.stat().st_size))
                if not any(key.lower() == "cache-control" for key in (headers or {})):
                    self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if not head_only:
                    shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # The client disconnected while downloading.

    def _serve_download(self, path: str, head_only: bool = False) -> bool:
        try:
            asset = resolve_download_asset(DOWNLOAD_CATALOG, path, HERE / "web")
        except DownloadNotFound as error:
            self._json(404, fail(str(error), "not_found"))
            return True
        except (DownloadUnavailable, DownloadManifestInvalid) as error:
            self._json(503, fail(str(error), "unavailable"))
            return True
        if asset is None:
            return False
        file_path, ctype, headers = asset
        if not file_path.is_file():
            self._json(404, fail("下载资源不存在", "not_found"))
            return True
        self._send_file(file_path, ctype, headers, head_only=head_only)
        return True

    def _session_id(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _center_token(self) -> str:
        sid = self._session_id()
        if not sid:
            return ""
        with CENTER_SESSION_LOCK:
            return CENTER_SESSION_TOKENS.get(sid, "")

    def _center_profile(self) -> Dict[str, Any] | None:
        if not CFG.get("center_auth_url"):
            return None
        token = self._center_token()
        return center_auth.account_profile(CFG["center_auth_url"], token) if token else None

    def _sync_growth_center(self, session: Dict[str, Any]) -> None:
        token = self._center_token()
        if not CFG.get("center_auth_url") or not token:
            return
        c = conn()
        try:
            account_growth.sync_center(c, CFG["center_auth_url"], token, session["user_id"])
        except Exception as error:  # noqa: BLE001 - pending local events remain durable
            log("WARN", f"中心经验同步待重试 user={session.get('user_id', '')}: {error}")
        finally:
            c.close()

    def _session(self) -> Dict[str, Any] | None:
        sid = self._session_id()
        if not sid:
            return None
        c = conn()
        try:
            if not sid.isascii():
                return None
            token_hash = hashlib.sha256(sid.encode("ascii")).hexdigest()
            row = manualdb.find_active_auth_session(
                c, token_hash, int(time.time()), require_password=not bool(CFG.get('center_auth_url')))
            if not row:
                c.execute("DELETE FROM auth_sessions WHERE token_hash=? OR expires_at<=?", (token_hash, int(time.time())))
                c.commit()
                return None
            if CFG.get("center_auth_url"):
                with CENTER_SESSION_LOCK:
                    center_token = CENTER_SESSION_TOKENS.get(sid)
                if not center_token:
                    c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
                    c.commit()
                    return None
                try:
                    access = center_auth.validate(CFG["center_auth_url"], center_token)
                    row["display_name"] = access["identity"].get("displayName") or row["display_name"] or row["name"]
                except center_auth.CenterAuthError:
                    with CENTER_SESSION_LOCK:
                        CENTER_SESSION_TOKENS.pop(sid, None)
                    c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
                    c.commit()
                    return None
            self._set_api_monitor_identity(row)
            return row
        finally:
            c.close()

    def _prospector_transport_allowed(self):
        if self._secure_transport():
            return True
        # Explicitly approved private LAN HTTP; public peers/Host and proxy spoofing stay blocked.
        try:
            host = urlparse('http://' + self.headers.get('Host', '')).hostname
        except ValueError:
            return False
        return private_http_transport(self.client_address[0], host)

    def _secure_transport(self):
        return secure_transport(
            tls=isinstance(self.connection, ssl.SSLSocket),
            peer=self.client_address[0],
            forwarded_proto=self.headers.get("X-Forwarded-Proto", ""),
            trusted_proxy_https=os.environ.get("NAVIGATOR_TRUSTED_PROXY_HTTPS") == "1",
        )

    def _bearer(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not header[7:].strip():
            raise prospector_auth.InvalidToken("login required")
        c = conn()
        try:
            account = prospector_auth.authenticate(c, header[7:].strip())
            self._set_api_monitor_identity(account)
            return account
        finally:
            c.close()

    def _session_cookie(self, sid: str, expired: bool = False) -> Dict[str, str]:
        secure = "; Secure" if self._secure_transport() else ""
        return {"Set-Cookie": f"{SESSION_COOKIE}={sid}; Path=/; Max-Age={0 if expired else SESSION_TTL_SECONDS}; HttpOnly; SameSite=Strict{secure}"}

    def _image_allowed(self, name: str, session) -> bool:
        if session["role"] == ROLE_ADMIN:
            return True
        c = conn()
        try:
            scope = ''  # Quote images are shared by all authenticated roles.
            # Bound file/media prefix narrows candidate rows; actual references are resolved below.
            prefix = name.lstrip("@")[:16]
            pattern = "%" + prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            filters = "(image_file LIKE ? ESCAPE '\\' OR image_url LIKE ? ESCAPE '\\' OR message_text LIKE ? ESCAPE '\\')"
            sql = "SELECT image_file,image_url,message_text FROM freight_items WHERE " + filters
            if scope:
                sql += " AND " + scope
            for row in c.execute(sql, (pattern, pattern, pattern)):
                if any(image["file"] == name for image in images_for(dict(row))):
                    return True
            if session["role"] == ROLE_OPERATOR:
                scope = ''
                sql = "SELECT payload FROM pending_records p WHERE payload LIKE ? ESCAPE '\\'"
                if scope:
                    sql += " AND " + scope
                for row in c.execute(sql, (pattern,)):
                    if any(image["file"] == name for image in images_for(json.loads(row["payload"]))):
                        return True
            return False
        finally:
            c.close()

    def _send_image(self, path: str, session) -> None:
        from urllib.parse import unquote
        from unicodedata import normalize
        import mimetypes

        name = normalize("NFC", unquote(path[len("/images/"):]))
        imgdir = (HERE / "images").resolve()
        fp = (imgdir / name).resolve()
        if imgdir not in fp.parents or not fp.is_file():
            return self._json(404, {"ok": False, "error": "image not found"})
        if not self._image_allowed(fp.name, session):
            return self._json(404, fail("image not found", "not_found"))
        ctype = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
        return self._send(200, fp.read_bytes(), ctype)

    def log_message(self, fmt, *args) -> None:
        pass     # 逐请求日志关掉，动作留痕走 log()

    def do_OPTIONS(self) -> None:
        self._send(204, b"", "text/plain")

    def do_HEAD(self) -> None:
        p = urlparse(self.path).path
        if self._serve_download(p, head_only=True):
            return
        self._send(404, b"", "text/plain")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        p = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self._begin_api_monitor("GET", p)
        try:
            if self._serve_download(p):
                return
            bearer_request = bool(self.headers.get("Authorization"))
            if (bearer_request or p.startswith('/api/prospector/')) and not self._prospector_transport_allowed():
                return self._json(403, fail("HTTPS required outside private LAN", "https_required"))
            if p == CENTER_NAVIGATOR_DASHBOARD_ROUTE:
                if not bearer_request:
                    return self._json(401, fail("login required", "unauthenticated"))
                return self._json(200, act_center_navigator_dashboard(self.headers.get("Authorization", "")[7:].strip()))
            if bearer_request and not prospector_read_allowed("GET", p):
                return self._json(403, fail("read-only device authorization", "forbidden"))
            if p == PROSPECTOR_AUTHORIZE:
                authorization_params(q)
                return self._send(200, (HERE / "web" / "prospector-authorize.html").read_bytes(), "text/html; charset=utf-8")
            if p in ("/", "/index.html"):
                return self._send(200, (HERE / "web" / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if p in ('/assets/admin-terminal.js','/assets/admin-terminal.css'):
                asset = HERE / 'web' / p.rsplit('/',1)[-1]
                return self._send(200,asset.read_bytes(),'application/javascript; charset=utf-8' if p.endswith('.js') else 'text/css; charset=utf-8')
            if p in ('/assets/notices.js', '/assets/notices.css', '/assets/notice-icons.svg'):
                name = p.rsplit('/', 1)[1]
                mime = 'image/svg+xml' if name.endswith('.svg') else 'text/javascript' if name.endswith('.js') else 'text/css'
                return self._send(200, (HERE / 'web' / name).read_bytes(), mime + '; charset=utf-8')
            if p.startswith('/assets/avatars/'):
                from core.contracts import ACCOUNT_AVATAR_ALL_IDS
                name = p.removeprefix('/assets/avatars/')
                if name not in {avatar_id + '.png' for avatar_id in ACCOUNT_AVATAR_ALL_IDS}:
                    return self._json(404, fail('avatar not found', 'not_found'))
                return self._send(200, (HERE / 'web' / 'avatars' / name).read_bytes(), 'image/png')
            if p == "/api/local-token":
                # Recovery credential is available only before the first administrator exists.
                if self.client_address[0] in ("127.0.0.1", "::1") and not active_admin_exists():
                    return self._json(200, {"ok": True, "token": CFG["token"]})
                return self._json(403, fail("bootstrap credential unavailable", "forbidden"))
            if p == "/api/auth/status":
                return self._json(200, auth_status(self._session()))
            if p == LIVENESS_ROUTE:
                return self._json(200, ok({"status": "ok", "backend": CFG["backend"]}))
            if p.startswith("/api/") or p.startswith("/images/"):
                session = self._bearer() if bearer_request else self._session()
                q = {k: v for k, v in q.items() if not k.startswith('_')}
                if bearer_request:
                    q['_client_app'] = 'prospector'
                    if q.get('activity') == 'intentional':
                        q['query_id'] = query_identity(q.get('query_id'))
                agent = not bearer_request and bool(CFG["token"] and self.headers.get("x-push-token", "") == CFG["token"])
                if agent:
                    self._set_api_monitor_identity({"role": "agent"})
                permission = ok({"principal": session["role"]}) if bearer_request else authorize("GET", p, session["role"] if session else None, agent)
                if not permission["ok"]:
                    return self._json(401 if permission["code"] == "unauthenticated" else 403, permission)
                principal = permission["data"]["principal"]
                if session and session.get('must_change_password'):
                    return self._json(403,fail('请先修改登录密码',PASSWORD_CHANGE_REQUIRED))
                q["_role"] = principal
                q["_client_ip"] = self.client_address[0]
                if session:
                    q['_actor'] = session['name']
                    q['_actor_id'] = session['user_id']
                    q['_actor_display'] = session.get('display_name') or session['name']
                q = scoped_sales_query(p, q, session)
                if p == "/api/master" and q.get("full") == "1" and principal not in (ROLE_ADMIN, "agent"):
                    return self._json(403, fail("admin role required", "forbidden"))
            if p == PROSPECTOR_ME:
                return self._json(200, prospector_routes.me(session))
            if p == PROSPECTOR_OPTIONS:
                c = conn()
                try:
                    master = manualdb.master_payload(c)
                    return self._json(200, ok({key: master[key] for key in ('ports','portGroups','carriers','routes')}))
                finally:
                    c.close()
            if p == ACCOUNT_PROFILE_ROUTE:
                self._sync_growth_center(session)
                shared = self._center_profile()
                return self._json(200, account_selfservice.profile(conn, session, self.client_address[0], shared))
            if p == GROWTH_ROUTE:
                if CFG.get("center_auth_url"):
                    self._sync_growth_center(session)
                    shared = self._center_profile()
                    return self._json(200, center_growth_payload(shared or {}, q))
                return self._json(200, account_growth.read(conn, session, {k: v for k, v in q.items() if not k.startswith('_')}))
            if p.startswith("/images/"):
                return self._send_image(p, session)
            fn = GET_ROUTES.get(p)
            if fn:
                result = fn(q)
                if p == DOWNLOAD_MANIFEST_ROUTE and not result.get("ok"):
                    return self._json(404 if result.get("code") == "not_found" else 503, result)
                return self._json(200, result)
            return self._json(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001 - 统一转结构化错误，别把线程搞崩
            if isinstance(e, prospector_auth.InvalidToken):
                return self._json(401, fail(str(e), "unauthenticated"))
            if p == PROSPECTOR_AUTHORIZE or (bearer_request and isinstance(e, ValueError)):
                return self._json(400, fail("invalid authorization or query request", "invalid_request"))
            log("WARN" if p == FEEDBACK_ROUTE and isinstance(e, ValueError) else "ERROR", f"GET {p}: {e}")
            code = 403 if isinstance(e, PermissionError) else 400 if isinstance(e, ValueError) else 500
            if p == FEEDBACK_ROUTE and isinstance(e, ValueError):
                code = 400
            if p in ({NOTICES_ROUTE, GROWTH_ROUTE} | NOTIFICATION_GET_ROUTES):
                code = 403 if isinstance(e, PermissionError) else 400 if isinstance(e, ValueError) else 500
            if p == COMPANY_RATE_TASK_ROUTE and isinstance(e, LookupError):
                code = 404
            public_error = str(e) if isinstance(e, (ValueError, PermissionError)) or (
                p == COMPANY_RATE_TASK_ROUTE and isinstance(e, LookupError)
            ) else "服务器内部错误"
            return self._json(code, {"ok": False, "error": public_error})

    def do_POST(self) -> None:
        p = urlparse(self.path).path
        self._begin_api_monitor("POST", p)
        authorization = self.headers.get("Authorization", "")
        agent_intake = p == AGENT_INTAKE_ROUTE
        if (authorization or p in PROSPECTOR_POST) and not self._prospector_transport_allowed():
            return self._json(403, fail("HTTPS required outside private LAN", "https_required"))
        if authorization and p != PROSPECTOR_LOGOUT and not agent_intake:
            return self._json(403, fail("read-only device authorization", "forbidden"))
        auth_routes = {*PROSPECTOR_POST, "/api/auth/login", "/api/auth/logout", "/api/auth/setup", ACCOUNT_PROFILE_ROUTE, GROWTH_SEARCH_ROUTE, *ACCOUNT_POST}
        if not browser_write_allowed(self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site"),
                                     self.headers.get("Host", ""), self._secure_transport()):
            return self._json(403, fail("request origin rejected", "origin_rejected"))
        if p not in auth_routes and p != "/api/import" and p not in POST_ROUTES:
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("content-length") or 0)
            if n < 0:
                raise ValueError("negative body length")
            if p in auth_routes and n > AUTH_REQUEST_MAX_BYTES:
                return self._json(413, fail("login request too large", "body_too_large"))
            if n > MAX_BODY:
                if p == AGENT_INTAKE_ROUTE:
                    return self._json(413, contract_failure("body_too_large", f"body too large (> {MAX_BODY} bytes)"))
                return self._json(413, {"ok": False, "error": f"body too large (> {MAX_BODY} bytes)"})
            if p == NOTICE_SAVE_ROUTE and n > NOTICE_REQUEST_MAX:
                return self._json(413, fail('公告及图片总大小不能超过 24 MB', 'body_too_large'))
            payload = json.loads(self.rfile.read(n).decode("utf-8") if n else "{}")
        except Exception as e:  # noqa: BLE001
            if p == AGENT_INTAKE_ROUTE:
                return self._json(400, contract_failure("invalid_request", "invalid JSON body"))
            return self._json(400, {"ok": False, "error": f"invalid json: {e}"})
        try:
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            if p in PROSPECTOR_POST:
                account = None
                if p == PROSPECTOR_AUTHORIZE:
                    expected_origin = ("https://" if self._secure_transport() else "http://") + self.headers.get("Host", "")
                    if self.headers.get("Origin") != expected_origin:
                        return self._json(403, fail("same-origin consent required", "origin_rejected"))
                    account = self._session()
                    if not account:
                        return self._json(401, fail("login required", "unauthenticated"))
                    if account.get('must_change_password'):
                        return self._json(403, fail('请先修改登录密码', PASSWORD_CHANGE_REQUIRED))
                elif p == PROSPECTOR_LOGOUT:
                    account = self._bearer()
                c = conn()
                try:
                    result = prospector_routes.post(c, p, payload, account)
                    return self._json(200, result)
                finally:
                    c.close()
            if p in (AUTH_REGISTER_ROUTE, AUTH_FORGOT_ROUTE):
                if p == AUTH_REGISTER_ROUTE and not active_admin_exists():
                    return self._json(403, fail("请先由管理员初始化系统", "setup_required"))
                limiter = RECOVERY_LIMITER if p == AUTH_FORGOT_ROUTE else LOGIN_LIMITER
                with limiter.admit(str(payload.get("name") or "").strip(), self.client_address[0]):
                    result = ACCOUNT_POST[p](payload)
                return self._json(200, result)
            if p == ACCOUNT_PROFILE_ROUTE:
                session = self._session()
                if not session:
                    return self._json(401, fail("请先登录", "unauthenticated"))
                if session.get('must_change_password'):
                    return self._json(403,fail('请先修改登录密码',PASSWORD_CHANGE_REQUIRED))
                save_shared = None
                if CFG.get("center_auth_url"):
                    save_shared = lambda display_name, avatar_id: center_auth.update_account_profile(
                        CFG["center_auth_url"], self._center_token(), display_name,
                        None if avatar_id == 'default' else avatar_id)
                return self._json(200, account_selfservice.save_profile(
                    conn, session, self.client_address[0], payload, save_shared))
            if p == AUTH_PASSWORD_ROUTE:
                session = self._session()
                if not session:
                    return self._json(401, fail("请先登录后修改密码", "unauthenticated"))
                with LOGIN_LIMITER.admit(session["name"], self.client_address[0]):
                    result = ACCOUNT_POST[p](payload, session)
                return self._json(200, result, self._session_cookie("", expired=True))
            if p == "/api/auth/login":
                with LOGIN_LIMITER.admit(str(payload.get("name") or "").strip(), self.client_address[0]):
                    result, sid = auth_login(payload, self.client_address[0])
                self._set_api_monitor_identity(result.get("user"))
                return self._json(200, result, self._session_cookie(sid))
            if p == "/api/auth/logout":
                self._session()
                sid = ""
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == SESSION_COOKIE:
                        sid = v
                        break
                if sid:
                    if CFG.get("center_auth_url"):
                        with CENTER_SESSION_LOCK:
                            center_token = CENTER_SESSION_TOKENS.pop(sid, "")
                        if center_token:
                            center_auth.logout(CFG["center_auth_url"], center_token)
                    c = conn()
                    try:
                        c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (hashlib.sha256(sid.encode("ascii")).hexdigest(),))
                        c.commit()
                    finally:
                        c.close()
                return self._json(200, {"ok": True}, self._session_cookie("", expired=True))
            if p == "/api/auth/setup":
                if self.headers.get("x-push-token") != CFG["token"]:
                    return self._json(401, {"ok": False, "error": "bootstrap token required"})
                return self._json(200, auth_setup(payload))
            sess = self._session()
            agent_token = self.headers.get("x-push-token", "")
            scheme, _, bearer_agent_token = authorization.partition(" ")
            bearer_agent_valid = bool(
                agent_intake and CFG.get("agent_api_token") and scheme.lower() == "bearer"
                and bearer_agent_token and secrets.compare_digest(bearer_agent_token, CFG["agent_api_token"])
            )
            if agent_intake and authorization and not bearer_agent_valid:
                return self._json(401, contract_failure("unauthorized", "Agent API token 无效"))
            token_agent = bearer_agent_valid or bool(agent_token and agent_token == CFG["token"])
            permission = authorize("POST", p, sess["role"] if sess else None, token_agent)
            if not permission["ok"]:
                if agent_intake:
                    return self._json(401, contract_failure("unauthorized", "Agent API token 无效或未配置"))
                return self._json(401 if permission["code"] == "unauthenticated" else 403, permission)
            if p == "/api/pending/delete" and payload.get("force") and (not sess or sess["role"] != ROLE_ADMIN):
                return self._json(403, fail("admin role required for force deletion", "forbidden"))
            if agent_intake:
                return self._json(200, POST_ROUTES[p](payload))
            payload.pop("_scope_viewer", None)
            if sess and sess.get('must_change_password'):
                return self._json(403,fail('请先修改登录密码',PASSWORD_CHANGE_REQUIRED))
            if p == GROWTH_SEARCH_ROUTE:
                if CFG.get("center_auth_url"):
                    if payload:
                        raise ValueError("growth award accepts no client fields")
                    occurred_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
                    day = occurred_at[:10]
                    receipt = center_auth.record_growth_event(CFG["center_auth_url"], self._center_token(), {
                        "eventId": f"daily-search:{sess['user_id']}:{day}",
                        "sourceProduct": "navigator", "eventType": "daily_effective_search",
                        "subjectKey": day, "points": 18, "ruleVersion": GROWTH_RULE_VERSION,
                        "occurredAt": occurred_at})
                    shared = self._center_profile()
                    data = center_growth_payload(shared or {}, {"limit": GROWTH_HISTORY_SIZE, "offset": 0})["data"]
                    data["awarded"] = bool(receipt.get("awarded"))
                    data["awarded_points"] = int(receipt.get("awardedPoints") or 0)
                    data["growth_award"] = center_growth_award(receipt)
                    return self._json(200, ok(data))
                return self._json(200, account_growth.award_search(conn, sess, payload))
            if p in ('/api/pending/approve_all','/api/pending/sync') and payload.get('ownership','mine') not in ('mine','all'):
                raise ValueError('ownership must be mine or all')
            if (sess and sess['role'] == ROLE_OPERATOR
                    and p in ('/api/pending/approve_all','/api/pending/sync')
                    and payload.get('ownership','mine') == 'mine'):
                # Batch selection follows the chosen display scope, not a permission boundary.
                payload["_scope_viewer"] = sess["name"]
            payload["_actor"] = sess["name"] if sess else "agent"
            payload["_actor_id"] = sess["user_id"] if sess else "agent"
            payload['_role'] = sess['role'] if sess else 'agent'
            payload['_actor_display'] = (sess.get('display_name') or sess['name']) if sess else 'agent'
            if sess and CFG.get("center_auth_url"):
                payload['_center_token'] = self._center_token()
            if p.startswith("/api/admin/company-token-refresh") or p == "/api/admin/company-credentials":
                payload['_client_ip'] = self.client_address[0]
            if p in (ACTIVITY_PAGE_OPEN_ROUTE, ACTIVITY_QUOTE_USAGE_ROUTE):
                payload['_client_ip'] = self.client_address[0]
            if p == "/api/import":
                c = conn()
                try:
                    summary = manualdb.load_snapshot(c, payload)
                finally:
                    c.close()
                log("INFO", f"导入快照 {summary}")
                return self._json(200, {"ok": True, "summary": summary})
            return self._json(200, POST_ROUTES[p](payload))
        except LoginThrottled as error:
            return self._json(429, fail(str(error), error.code), {"Retry-After": str(error.retry_seconds)})
        except Exception as e:  # noqa: BLE001
            if p == AGENT_INTAKE_ROUTE:
                code = 400 if isinstance(e, ValueError) else 500
                message = str(e) if code == 400 else "Agent intake 服务暂不可用"
                log("WARN" if code == 400 else "ERROR", f"POST {p}: {type(e).__name__}")
                return self._json(code, contract_failure("invalid_request" if code == 400 else "unavailable", message))
            if p in PROSPECTOR_POST:
                code = 401 if isinstance(e, prospector_auth.InvalidToken) else 400 if isinstance(e, ValueError) else 500
                log("WARN", f"POST {p}: {type(e).__name__}")
                return self._json(code, fail("authorization request rejected", "unauthenticated" if code == 401 else "invalid_request"))
            log("ERROR", f"POST {p}: {e}")
            code = 403 if isinstance(e, PermissionError) else 400 if isinstance(e, ValueError) else 500
            public_error = str(e) if isinstance(e, (ValueError, PermissionError)) else "服务器内部错误"
            return self._json(code, {"ok": False, "error": public_error})


def spawn_daemon(args) -> int:
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    command = [sys.executable, str(Path(__file__).resolve()),
               "--host", args.host, "--port", str(args.port), "--db", args.db,
               "--backend", args.backend, "--postgres-env-file", args.postgres_env_file,
               "--center-auth-url", args.center_auth_url]
    if args.tls_cert_file:
        command.extend(["--tls-cert-file", args.tls_cert_file, "--tls-key-file", args.tls_key_file])
    with open(args.log, "ab") as logf:
        proc = subprocess.Popen(
            command,
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            cwd=str(HERE), creationflags=flags, close_fds=True)
    pid_file = "manual.pid" if args.port == DEFAULT_PORT else f"manual-{args.port}.pid"
    (HERE / pid_file).write_text(str(proc.pid), encoding="ascii")
    print(f"[freight-manual] daemon started, pid={proc.pid}, log={args.log}")
    return proc.pid


def main() -> None:
    ap = argparse.ArgumentParser(description="人工维护台账（局域网版，独立库）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，局域网访问需 0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=str(manualdb.DEFAULT_DB))
    ap.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    ap.add_argument("--postgres-env-file", default=str(postgresdb.DEFAULT_ENV_FILE))
    ap.add_argument("--center-auth-url", default=os.environ.get("NAVIGATOR_CENTER_AUTH_URL", ""), help="中心身份 API 地址；启用后登录和每次会话校验均走中心")
    ap.add_argument("--tls-cert-file", default=os.environ.get("FREIGHT_TLS_CERT_FILE", ""), help="受信任的内网 HTTPS 证书文件")
    ap.add_argument("--tls-key-file", default=os.environ.get("FREIGHT_TLS_KEY_FILE", ""), help="仅服务账号可读的证书私钥文件")
    ap.add_argument("--daemon", action="store_true", help="后台常驻（日志写 manual.log）")
    ap.add_argument("--log", default=str(HERE / "manual.log"))
    ap.add_argument("--import", dest="import_file", help="导入整库快照 JSON 后退出")
    ap.add_argument("--init-only", action="store_true", help="只建库建表后退出")
    args = ap.parse_args()
    if bool(args.tls_cert_file) != bool(args.tls_key_file):
        ap.error("HTTPS 证书和私钥必须同时配置")
    tls_context = None
    if args.tls_cert_file:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(args.tls_cert_file, args.tls_key_file)

    agent_api_token = os.environ.get("NAVIGATOR_AGENT_API_TOKEN", "").strip()
    if agent_api_token and len(agent_api_token) < 32:
        ap.error("NAVIGATOR_AGENT_API_TOKEN 至少需要 32 个字符")
    CFG.update(db=args.db, backend=args.backend, postgres_env_file=args.postgres_env_file,
               center_auth_url=args.center_auth_url.rstrip("/"), agent_api_token=agent_api_token)

    if args.import_file:
        snap = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        c = conn()
        try:
            if args.backend == "sqlite":
                manualdb.init_schema(c)
            else:
                c.ensure_identity_schema()
                manualdb.ensure_port_route_schema(c)
                manualdb.ensure_account_schema(c)
                manualdb.ensure_review_issue_schema(c)
            print("[freight-manual] 导入:", manualdb.load_snapshot(c, snap))
        finally:
            c.close()
        return
    if args.init_only:
        c = conn()
        try:
            if args.backend == "sqlite":
                manualdb.init_schema(c)
            else:
                c.ensure_identity_schema()
                c.execute("SELECT 1 FROM ports LIMIT 1").fetchone()
                result = manualdb.ensure_port_route_schema(c)
                log("INFO", f"港口航线关系初始化：{result['data']}")
                manualdb.ensure_account_schema(c)
                manualdb.ensure_review_issue_schema(c)
        finally:
            c.close()
        print("[freight-manual] 库已就绪:", args.backend)
        return
    if port_taken(args.port):
        print(f"[freight-manual] 端口 {args.port} 已有服务在应答，本次不启动。"
              f"换端口：python manual_server.py --port {args.port + 5}")
        sys.exit(3)
    if args.daemon:
        spawn_daemon(args)
        return

    CFG["token"] = ensure_token(HERE / "token_manual.txt")
    if args.backend == "postgres":
        from ops.postgres_backup import backup
        result = backup(Path(args.postgres_env_file))
        log("INFO", f"PostgreSQL 升级前备份已校验：{result['path']}")
    c0 = conn()
    try:
        if args.backend == "sqlite":
            manualdb.init_schema(c0)
        else:
            c0.ensure_identity_schema()
            c0.execute("SELECT 1 FROM ports LIMIT 1").fetchone()
            result = manualdb.ensure_port_route_schema(c0)
            log("INFO", f"港口航线关系初始化：{result['data']}")
            account_result = manualdb.ensure_account_schema(c0)
            log("INFO", f"账号权限结构初始化：{account_result['data']}")
            manualdb.ensure_review_issue_schema(c0)
        members = publisher_members()
        created = manualdb.ensure_publisher_people(c0, PublisherPeople(members))
        removed = remove_excluded_placeholders(c0, members, manualdb.now_cn())
        log("INFO", f"发布人名册同步：新增 {created} 人，清理已排除占位账号 {removed} 人")
    finally:
        c0.close()
    if args.backend == "sqlite":
        try:
            auto_backup(args.db)
        except Exception as e:  # noqa: BLE001 - startup backup is best effort on the legacy backend
            log("WARN", f"启动自动备份失败（不阻断启动）：{e}")
    srv = FreightHTTPServer((args.host, args.port), Handler)
    if tls_context:
        srv.socket = tls_context.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    scheme = "https" if tls_context else "http"
    log("INFO", f"看板 {scheme}://127.0.0.1:{args.port}/   局域网 {scheme}://<本机IP>:{args.port}/")
    log("INFO", f"写入令牌见 token_manual.txt；数据库后端 {args.backend}")
    COMPANY_TOKEN_REFRESHER.run_periodically()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("INFO", "已停止")


if __name__ == "__main__":
    main()
