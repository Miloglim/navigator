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
    GET  /api/local-token              只回本机回环：这台电脑上的浏览器免口令
    GET  /images/<名>                  静态原图：从本机 images/ 目录取运价截图供看板显示
    GET  /api/export                   整库快照
    GET  /api/pending                  待审核列表 ?status=awaiting|approved|rejected|all&kind=&limit=&offset=（每行附解析后的图片）
    GET  /api/audit                    全库标准化体检（列出存量不达标行）
    POST /api/push                     录入 {table, records:[中文字段], origin}；过硬标准化闸门 + 自动判新增/更新(留痕)
    POST /api/validate                 预检（只读，不写库）：{table, records} → 每行 {ok,resolved,errors}
    POST /api/pending                  待审核暂存 {table, records, origin='agent'}：机械校验 + 判新增/更新
    POST /api/pending/approve          放行一条暂存记录入库（仅 check_status=passed）
    POST /api/pending/update           编辑暂存记录 {id, fields}：重校验 + 重判新增/更新
    POST /api/pending/reject           驳回（软释放，移入驳回日志，留原因）
    POST /api/pending/restore          撤回误驳（驳回日志 → 待处理）
    POST /api/pending/delete           物理删除暂存行（仅驳回日志内触发，不可恢复）
    POST /api/record/update            就地改（不留痕）
    POST /api/record/revise            留痕更新：追加新行 + 旧行标已被覆盖 + 变动摘要
    POST /api/record/delete            删一条
    POST /api/master/save|delete       港口 / 船司 / 航线 / 发送人 / 港群 维护
    POST /api/operator/save|delete /api/chat-group/save|delete /api/chat-groups/import
    POST /api/sync                     批量导入英文列名行
    POST /api/import                   导入整库快照
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import parse_qs, urlparse

try:  # Windows 控制台默认 GBK，中文输出会乱码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 输出重定向到文件时无需处理
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import manualdb  # noqa: E402
import price_sanitize  # noqa: E402

CFG: Dict[str, str] = {"db": "", "token": ""}
DEFAULT_PORT = 8795


def log(level: str, msg: str) -> None:
    print(f"[{manualdb.now_cn()}] [{level}] [manual-board] {msg}", flush=True)


def conn():
    return manualdb.connect(CFG["db"])


def ensure_token(path: Path) -> str:
    if path.exists():
        t = path.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = "frtm_" + secrets.token_hex(16)
    path.write_text(t, encoding="utf-8")
    return t


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


def _resolve_image(name: str):
    """在 Database/images/ 里按精确名或 mediaId 前缀找真实文件，返回 (url, filename) 或 (None, None)。"""
    from urllib.parse import quote
    from unicodedata import normalize

    name = normalize("NFC", str(name))
    imgdir = HERE / "images"
    if not imgdir.exists():
        return None, None
    direct = imgdir / name
    if direct.is_file():
        return "/images/" + quote(direct.name), direct.name
    stem = name.lstrip("@")
    if stem:
        for f in imgdir.iterdir():
            fstem = f.stem.lstrip("@")
            if f.is_file() and fstem.startswith(stem[:16]):
                return "/images/" + quote(f.name), f.name
    return None, None


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
        return {"ok": True, **with_images(manualdb.query(c, manualdb.get_master(c), kind, q))}
    finally:
        c.close()


def act_grouped(kind: str, q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True, **with_images(manualdb.query_grouped(c, manualdb.get_master(c), kind, q))}
    finally:
        c.close()


def act_stats() -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True, **manualdb.stats(c)}
    finally:
        c.close()


def act_options() -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True, **manualdb.options(c)}
    finally:
        c.close()


def act_bootstrap() -> Dict[str, Any]:
    c = conn()
    try:
        return {"ok": True,
                "operators": [o for o in manualdb.list_operators(c) if o.get("active")],
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
        return {"ok": True, "rows": manualdb.list_operators(c)}
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
        res = manualdb.push_with_review(c, manualdb.get_master(c), kind, records, origin=origin)
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
    c = conn()
    try:
        res = manualdb.revise_record(c, manualdb.get_master(c), kind, key, fields,
                                     origin=body.get("origin") or "manual")
        if res.get("ok"):
            log("INFO", f"revise {kind} {str(key)[:12]} → {res['revised']} 行，"
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
    c = conn()
    try:
        return {"ok": True, **manualdb.save_operator(c, body)}
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
    c = conn()
    try:
        rows = manualdb.alias_scope_list(
            c, q.get("q") or None, q.get("group") or None, q.get("kind") or None
        )
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


def _append_spec_log(group_name: str, line: str) -> bool:
    """P3-B：候选批准后往对应群规范.md 追加登记日志（transport 层做文件 IO，core 不碰）。
    群名→目录名规则：钉钉群名里的 '/' 在目录名里是 '+'（README 表约定）。失败只 WARN 不阻断。"""
    try:
        dir_name = (group_name or "").replace('/', '+')
        spec = HERE.parent / "Skill" / "freight-rate-heartbeat" / "references" / "群聊规范" / dir_name / "规范.md"
        if not dir_name or not spec.exists():
            return False
        text = spec.read_text(encoding='utf-8')
        marker = "## 别名登记日志"
        entry = f"- {line}\n"
        if marker in text:
            text = text.rstrip() + "\n" + entry
        else:
            text = text.rstrip() + f"\n\n{marker}\n\n<!-- 候选管理页签批准 alias_scope 别名时自动追加，人工可修订 -->\n\n" + entry
        spec.write_text(text, encoding='utf-8')
        return True
    except Exception as e:
        log("WARN", f"追加规范日志失败（不阻断批准）：{e}")
        return False


def act_candidates_approve(body: Dict[str, Any]) -> Dict[str, Any]:
    cand_id = int(body.get("id") or 0)
    reviewer = str(body.get("reviewer") or "unknown").strip()
    override_group = body.get("override_scope_group") or None
    if not cand_id:
        raise ValueError("id 必填")
    c = conn()
    try:
        res = manualdb.candidates_approve(c, cand_id, reviewer, override_group)
        log("INFO", f"候选 #{cand_id} 批准 by {reviewer} → {res.get('layer_action', {}).get('action')}")
        # P3-B：alias_scope 层候选批准后往群规范.md 追加登记日志（agent 下轮读规范时可见）
        ci = res.get("candidate") or {}
        if res.get("ok") and ci.get("target_layer") == "alias_scope":
            parts = [p.strip() for p in str(ci.get("scope_key") or "").split("|")]
            group = parts[-1] if parts else ""
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            _append_spec_log(group,
                             f"{ts} [别名登记] `{ci.get('alias')}` → `{ci.get('suggested_target')}`"
                             f"（{ci.get('kind')}，候选#{cand_id} 批准 by {reviewer}）")
        return res
    finally:
        c.close()


def act_candidates_reject(body: Dict[str, Any]) -> Dict[str, Any]:
    cand_id = int(body.get("id") or 0)
    reviewer = str(body.get("reviewer") or "unknown").strip()
    note = str(body.get("note") or "").strip()
    if not cand_id:
        raise ValueError("id 必填")
    c = conn()
    try:
        res = manualdb.candidates_reject(c, cand_id, reviewer, note)
        if res.get("ok"):
            log("INFO", f"候选 #{cand_id} 驳回 by {reviewer}")
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


def act_audit_hints(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        return manualdb.audit_hints(
            c, q.get("group") or None, q.get("sender") or None, q.get("carrier") or None,
            limit=int(q.get("limit") or 500))
    finally:
        c.close()


def act_named_delete(table: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handler(body: Dict[str, Any]) -> Dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("name required")
        c = conn()
        try:
            n = manualdb.delete_named(c, table, name)
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
        res = manualdb.audit_all(c, m)
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
        ids = manualdb.stage_pending(c, manualdb.get_master(c), kind, records, origin=origin)
        log("INFO", f"待审核暂存 {kind} {len(ids)} 条 origin={origin}")
        return {"ok": True, "staged": len(ids), "ids": ids}
    finally:
        c.close()


def act_pending_list(q: Dict[str, str]) -> Dict[str, Any]:
    c = conn()
    try:
        res = manualdb.list_pending(
            c, q.get("status") or "awaiting", q.get("kind") or "",
            int(q.get("limit") or 200), int(q.get("offset") or 0), q.get("check") or "")
        # 待审核卡片要能对照原图：payload 里的图片引用走销售端同一解析（单一真源）
        for row in res.get("rows", []):
            row["images"] = images_for(row.get("payload") or {})
        return {"ok": True, **res}
    finally:
        c.close()


def act_pending_approve(body: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    c = conn()
    try:
        res = manualdb.approve_pending(c, manualdb.get_master(c), pid,
                                       str(body.get("reviewed_by") or ""))
        if res.get("ok"):
            log("INFO", f"待审核放行 id={pid} kind={res['kind']}")
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
    actor = str(body.get("reviewer") or body.get("reviewed_by") or "").strip()
    c = conn()
    try:
        res = manualdb.update_pending(c, manualdb.get_master(c), pid, fields, body.get("kind"), actor=actor)
        if res.get("ok"):
            log("INFO", f"待审核编辑 id={pid} kind={res.get('kind')} check={res.get('check_status')} "
                        f"action={res.get('action')} by={actor or 'anon'} edits={res.get('edits_recorded', 0)}")
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
                                      str(body.get("reviewed_by") or ""))
        if res.get("ok"):
            log("WARN", f"待审核驳回（移入驳回日志）id={pid}")
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
    pid = int(body.get("id") or 0)
    if not pid:
        raise ValueError("id required")
    force = bool(body.get("force"))
    c = conn()
    try:
        res = manualdb.delete_pending(c, pid, force=force)
        if res.get("ok"):
            log("WARN", f"待审核物理删除（{'force' if force else '驳回日志'}）id={pid}")
        return res
    finally:
        c.close()


GET_ROUTES: Dict[str, Callable[[Dict[str, str]], Dict[str, Any]]] = {
    "/api/stats": lambda q: act_stats(),
    "/api/options": lambda q: act_options(),
    "/api/bootstrap": lambda q: act_bootstrap(),
    "/api/master": lambda q: act_master(q.get("full") == "1"),
    "/api/operators": lambda q: act_operators(),
    "/api/chat-groups": lambda q: act_chat_groups(),
    "/api/export": lambda q: act_export(),
    "/api/rates": lambda q: act_flat("rates", q),
    "/api/space": lambda q: act_flat("space", q),
    "/api/rates_grouped": lambda q: act_grouped("rates", q),
    "/api/space_grouped": lambda q: act_grouped("space", q),
    "/api/pending": lambda q: act_pending_list(q),
    "/api/audit": lambda q: act_audit(q),
    "/api/alias": lambda q: act_alias_list(q),
    "/api/shadow": lambda q: act_shadow_list(q),
    "/api/candidates": lambda q: act_candidates_list(q),
    "/api/audit-hints": lambda q: act_audit_hints(q),
}
POST_ROUTES: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
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
    "/api/pending/approve": act_pending_approve,
    "/api/pending/update": act_pending_update,
    "/api/pending/reject": act_pending_reject,
    "/api/pending/restore": act_pending_restore,
    "/api/pending/delete": act_pending_delete,
}
# 只读但用 POST 传数组的口子（不写库），免写令牌：批量预检在没登录令牌时也能跑
PUBLIC_POST = {"/api/validate"}
# GET 读接口分档（2026-09-17 定稿：只读分享免口令、写入与敏感读出口要口令）
# PUBLIC_READ_GET：销售端查价/看图、前端渲染必需的配置——局域网免口令，点开链接就能看
PUBLIC_READ_GET = {
    "/api/rates", "/api/space", "/api/rates_grouped", "/api/space_grouped",
    "/api/master", "/api/options", "/api/bootstrap", "/api/stats",
    "/api/operators", "/api/chat-groups", "/api/audit-hints",
}
# 其余 GET（含 /api/export 整库原文、/api/pending 待审原文队列、/api/audit 体检）非回环都要口令


class Handler(BaseHTTPRequestHandler):
    server_version = "FreightManual/2.0"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-push-token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send_image(self, path: str) -> None:
        from urllib.parse import unquote
        from unicodedata import normalize
        import mimetypes

        name = normalize("NFC", unquote(path[len("/images/"):]))
        imgdir = (HERE / "images").resolve()
        fp = (imgdir / name).resolve()
        if imgdir not in fp.parents or not fp.is_file():
            return self._json(404, {"ok": False, "error": "image not found"})
        ctype = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
        return self._send(200, fp.read_bytes(), ctype)

    def log_message(self, fmt, *args) -> None:
        pass     # 逐请求日志关掉，动作留痕走 log()

    def do_OPTIONS(self) -> None:
        self._send(204, b"", "text/plain")

    def _get_authorized(self, u) -> bool:
        """GET 鉴权（2026-09-17 审查修复）：本机回环免口令；局域网必须带有效 token。
        token 可走 header x-push-token 或 query ?token=（<img> 标签无法带 header）。
        此前 GET 全口无鉴权，/api/export 整库（含消息原文/发送人）对局域网裸奔。"""
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        tok = self.headers.get("x-push-token") or (u.query and parse_qs(u.query).get("token", [""])[0])
        return bool(tok) and tok == CFG["token"]

    def do_GET(self) -> None:
        u = urlparse(self.path)
        p = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p in ("/", "/index.html"):
                return self._send(200, (HERE / "web" / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if p == "/api/local-token":
                # 只回本机回环：这台电脑免口令，同网段其他机器拿不到 → 前端退回要口令
                if self.client_address[0] in ("127.0.0.1", "::1"):
                    return self._json(200, {"ok": True, "token": CFG["token"]})
                return self._json(403, {"ok": False, "error": "token only served to localhost"})
            if p.startswith("/api/") or p.startswith("/images/"):
                # 回环（本机）全免；局域网：只读分享接口（图片 + 查价 + 主数据/配置）免口令，
                # 其余（整库导出 export、待审原文队列 pending、体检 audit、所有写口）要口令
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    is_public_read = p.startswith("/images/") or p in PUBLIC_READ_GET
                    if not is_public_read and not self._get_authorized(u):
                        return self._json(401, {"ok": False, "error": "unauthorized"})
            if p.startswith("/images/"):
                return self._send_image(p)
            fn = GET_ROUTES.get(p)
            if fn:
                return self._json(200, fn(q))
            return self._json(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001 - 统一转结构化错误，别把线程搞崩
            log("ERROR", f"GET {p}: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    def do_POST(self) -> None:
        p = urlparse(self.path).path
        if p != "/api/import" and p not in POST_ROUTES:
            return self._json(404, {"ok": False, "error": "not found"})
        if p not in PUBLIC_POST and self.headers.get("x-push-token") != CFG["token"]:
            return self._json(401, {"ok": False, "error": "unauthorized"})
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") if n else "{}")
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"ok": False, "error": f"invalid json: {e}"})
        try:
            if p == "/api/import":
                c = conn()
                try:
                    summary = manualdb.load_snapshot(c, payload)
                finally:
                    c.close()
                log("INFO", f"导入快照 {summary}")
                return self._json(200, {"ok": True, "summary": summary})
            return self._json(200, POST_ROUTES[p](payload))
        except Exception as e:  # noqa: BLE001
            log("ERROR", f"POST {p}: {e}")
            return self._json(500, {"ok": False, "error": str(e)})


def spawn_daemon(args) -> int:
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    with open(args.log, "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--host", args.host, "--port", str(args.port), "--db", args.db],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            cwd=str(HERE), creationflags=flags, close_fds=True)
    (HERE / "manual.pid").write_text(str(proc.pid), encoding="ascii")
    print(f"[freight-manual] daemon started, pid={proc.pid}, log={args.log}")
    return proc.pid


def main() -> None:
    ap = argparse.ArgumentParser(description="人工维护台账（局域网版，独立库）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，局域网访问需 0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=str(manualdb.DEFAULT_DB))
    ap.add_argument("--daemon", action="store_true", help="后台常驻（日志写 manual.log）")
    ap.add_argument("--log", default=str(HERE / "manual.log"))
    ap.add_argument("--import", dest="import_file", help="导入整库快照 JSON 后退出")
    ap.add_argument("--init-only", action="store_true", help="只建库建表后退出")
    args = ap.parse_args()

    if args.import_file:
        snap = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        c = manualdb.connect(args.db)
        try:
            print("[freight-manual] 导入:", manualdb.load_snapshot(c, snap))
        finally:
            c.close()
        return
    if args.init_only:
        manualdb.connect(args.db).close()
        print("[freight-manual] 库已就绪:", args.db)
        return
    if port_taken(args.port):
        print(f"[freight-manual] 端口 {args.port} 已有服务在应答，本次不启动。"
              f"换端口：python manual_server.py --port {args.port + 5}")
        sys.exit(3)
    if args.daemon:
        spawn_daemon(args)
        return

    CFG["db"] = args.db
    CFG["token"] = ensure_token(HERE / "token_manual.txt")
    manualdb.connect(args.db).close()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log("INFO", f"看板 http://127.0.0.1:{args.port}/   局域网 http://<本机IP>:{args.port}/")
    log("INFO", f"写入令牌见 token_manual.txt；数据库 {args.db}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("INFO", "已停止")


if __name__ == "__main__":
    main()
