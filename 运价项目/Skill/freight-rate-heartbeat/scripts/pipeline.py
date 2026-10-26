# -*- coding: utf-8 -*-
"""pipeline.py — 运价心跳无人值守状态机（生产主控，代码层）。

设计：dws 命令只能由 Agent 的 Bash 工具直调（宿主执行），本脚本负责一切决策。
写入对象已迁到「运价台账」(freight_manual.db / manual_server.py)：本机 HTTP 可由 python urllib
直连（db_client），不再像旧版那样由 Agent 逐条 dws create + 断点续传，也不再镜像旧本机看板。

目录协议（.work/ 下，<D>=MMDD）：
  run_<D>/state.json          状态机
  run_<D>/raw/<name>.json     Agent 存 dws 结果
  run_<D>/pull_receipt_*.json 每轮 DWS 拉取验证回执
  msgs_<D>/<slug>.json        逐字消息落盘（唯一原文源）
  draft_<D>/                  record_builder.py 输出（record = 中文字段候选记录）
  llm_out_<D>/<safe_id>.json  Agent 结构化判定（契约见 scripts/llm_output_contract.md）
  ocr_cache/<mediaId>.json    识图缓存（跨群复用，与分片无关，全局共享）

分片：<D> 后带分片后缀（--shard A → run_0920A/ 等），A/B/C/D 各跑各的、目录互不覆盖；
      ocr_cache 全局共享（识图结果跨分片/跨天复用）。不带 --shard 时后缀为空＝全量、沿用旧目录。

阶段：PULL → PARSED → LLM_PENDING → BUILD → PRECHECK → AGENT_REPAIR(最多修正一轮，重建后直接送审) → SUBMIT → DONE
子命令：
  python scripts/pipeline.py status
  python scripts/pipeline.py next      # 打印当前步该做的动作（dws 命令 + 存盘路径 / 提交进度）
  python scripts/pipeline.py advance   # 解析已存 raw / 推进提交（可循环调用直到 DONE）
"""
import json, os, re, sys, glob, hashlib, subprocess, argparse, tempfile
from urllib.parse import quote as quote_query
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
HIDDEN_SUBPROCESS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
# 分片/单群：命令行 --shard / --only 必须先落进环境变量，再 import paths（paths 导入时读它们定 sfx）。
# 用最小预扫描，不与后面的 argparse 抢；子进程（record_builder/check_llm_out/watermark）继承 env 自动同片/同群。
def _pre(flag, env, norm):
    for _i, _a in enumerate(sys.argv):
        if _a == flag and _i + 1 < len(sys.argv):
            os.environ[env] = norm(sys.argv[_i + 1])
        elif _a.startswith(flag + "="):
            os.environ[env] = norm(_a.split("=", 1)[1])
_pre("--shard", "FRT_SHARD", lambda v: v.strip().upper())
_pre("--only", "FRT_ONLY", lambda v: v.strip().lower())
import paths as P
from freight_identity.core.contracts import RAW_PULL_ARCHIVE_FIELD
from freight_identity.runtime import (snapshot as publisher_snapshot, decision_for as publisher_decision,
    load_roster, atomic_json, now as identity_now)
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = P.WORK
PY = sys.executable
sys.path.insert(0, HERE)
# 有效期解析口径单一事实源：record_builder.vdates = norm_v + validity_parser.parse。
from record_builder import vdates, compose_text
import ledger_client as db_client  # 台账对接层：唯一写入口 /api/pending，预检 /api/validate，主数据 /api/master
import spec_index                  # 「群聊规范」机器入口：slug→规范路径、缩写表、待补条目、可引用锚点
import check_llm_out as llm_check   # 判定自查：严重来源/依据错误隔离消息，普通警告继续业务预检
import knowledge_gate              # 知识文件读取闸门：按本轮 new_ids 校验并留回执

# Local desktop sessions and the Windows runner share a stable monitoring run ID.
if not os.environ.get("FRT_MONITOR_RUN_ID"):
    _desktop_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if _desktop_id:
        os.environ["FRT_MONITOR_RUN_ID"] = "desktop-" + _desktop_id
_monitor_report = lambda **kwargs: False
_monitor_checkpoint = lambda: None
if os.environ.get("FRT_MONITOR_RUN_ID"):
    import logging
    _monitor_root = os.path.join(P.PROJECT_ROOT, "ops", "agent-monitor")
    sys.path.append(_monitor_root)
    try:
        from agent_watch.transport.client import report as _monitor_report, checkpoint as _monitor_checkpoint, ensure_server as _monitor_wake
    except ImportError as exc:
        logging.warning("[MONITOR] integration unavailable (%s); collection continues", type(exc).__name__)

SPEC = P.CONTRACT
GROUPS = P.chat_groups()
GNAME = {g["slug"]: g["name"] for g in GROUPS}
DEFAULT_POL = {g["slug"]: g.get("default_pol") for g in GROUPS if g.get("default_pol")}
CTYPE = set(SPEC.get("ctype", []))
STYPE = set(SPEC.get("space_type_options", []))
# 提交阶段：连续多少个 advance 零进展即判定死锁（服务没起/token 失效），交人工
SUBMIT_STALL_LIMIT = 6
DWS_PAGE_LIMIT = 500
BATCH_RECORD_LIMIT = 1000
AGENT_REPAIR_STAGE = "AGENT_REPAIR"


def committed_index_path():
    return os.path.join(WORK, "committed_message_ids.json")

def runpy():
    base = '"%s" "%s"' % (PY.replace("\\", "/"), os.path.join(HERE, "pipeline.py").replace("\\", "/"))
    if P.SHARD:                       # 让 Agent 复制执行的下一步命令自带分片，子进程不会退回全量
        base += ' --shard %s' % P.SHARD
    if P.ONLY:                        # 单群手动模式：照抄的命令也锁定该群，避免下一轮又拉全群
        base += ' --only %s' % P.ONLY
    return base


def today():
    return datetime.now().strftime("%m%d")


def rdir(d):
    p = os.path.join(WORK, "run_%s%s" % (d, P.sfx()))
    os.makedirs(os.path.join(p, "raw"), exist_ok=True)
    return p


def _load_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def load_state(d):
    p = os.path.join(rdir(d), "state.json")
    if os.path.exists(p):
        return _load_json(p)
    # Missing state is fresh only when no artifacts exist for this exact scope.
    for directory in (rdir(d), *(os.path.join(WORK, "%s_%s%s" % (kind, d, P.sfx()))
                                 for kind in ("msgs", "draft", "llm_out"))):
        if os.path.isdir(directory) and any(files for _, _, files in os.walk(directory)):
            raise RuntimeError("state.json missing with existing run artifacts: %s" % p)
    return {"date": d, "stage": "PULL", "retries": {}, "submit_done": []}


def start_run(d):
    """Initialize a fresh scope; preserve existing state and refuse orphaned runs."""
    path = os.path.join(rdir(d), "state.json")
    existed = os.path.exists(path)
    try:
        st = load_state(d)
        if not isinstance(st, dict) or st.get("date") != d or not st.get("stage"):
            raise ValueError("invalid run state")
        if not existed:
            save_state(d, st)
        return {"ok": True, "data": {"state_file": path, "date": d,
                "shard": P.SHARD, "stage": st["stage"], "created": not existed}}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "error": "ABORT: %s" % exc}


def save_state(d, st):
    atomic_json(os.path.join(rdir(d), "state.json"), st)
    _monitor_progress(st)


def _monitor_progress(st):
    if not os.environ.get("FRT_MONITOR_RUN_ID"):
        return
    stage = st.get("stage", "UNKNOWN")
    activity = {"PULL": "拉取消息", "PARSED": "源消息解析完成", "LLM_PENDING": "等待 Agent 识图或语义处理",
                "BUILD": "整理候选 JSON", "PRECHECK": "首次预校验", "AGENT_REPAIR": "Agent 修正一轮",
                "SUBMIT": "提交待审核", "DONE": "采集状态机结束，业务结果以回执为准"}.get(stage, stage)
    if st.get("semantic_issues") or st.get("coverage_issues"):
        activity += "；部分来源隔离，水位保留"
    try:
        _monitor_report(source="pipeline", stage=stage, workflow_stage=stage, activity=activity,
                        completed=int(st.get("records_submitted") or 0), check_failed=int(st.get("records_check_failed") or 0))
    except Exception as exc:
        import logging
        logging.warning("[MONITOR] checkpoint report failed (%s); collection continues", type(exc).__name__)


def safe(name):
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name)[:60]


def _compact_dws_message(m):
    q = m.get("quotedMessage") or {}
    return {"t": m.get("createTime"), "s": m.get("sender"),
            "senderId": m.get("senderId"),
            "senderOpenDingTalkId": m.get("senderOpenDingTalkId"),
            "id": m.get("openMessageId") or m.get("messageId") or m.get("id"),
            "img": [r["resourceId"] for r in (m.get("resourceRefs") or [])
                    if isinstance(r, dict) and r.get("resourceId")
                    and (r.get("resourceIdType") or r.get("type")) == "mediaId"],
            "c": m.get("content") if m.get("content") is not None else m.get("text"),
            "q": {"t": q.get("createTime"), "s": q.get("sender"),
                  "senderId": q.get("senderId"),
                  "senderOpenDingTalkId": q.get("senderOpenDingTalkId"),
                  "id": q.get("openMessageId") or q.get("messageId") or q.get("id"),
                  "c": q.get("content") if q.get("content") is not None else q.get("text")}}


def parse_jsonish(text):
    text = (text or "").strip()
    if not text or text == "(empty output)":
        return [], "empty DWS response; cannot prove a successful pull"
    try:
        v = json.loads(text)
        if isinstance(v, dict) and ("success" in v or "messages" in v):
            # Single-page responses have success=true. DWS --page-all returns
            # the aggregate contract without a success key; its completeness
            # and failure flags are the authoritative proof.
            if v.get("success") is False:
                return [], "DWS failure: %s" % (v.get("errorCode") or v.get("errorMsg") or "unknown")
            if (v.get("complete") is not True or v.get("paginationKnown") is not True
                    or v.get("partial") is True or v.get("hasMore") is not False
                    or v.get("truncated") is True or v.get("truncatedByPageLimit") is True
                    or v.get("truncatedByResultLimit") is True or v.get("failedCount")
                    or v.get("decryptFailedCount") or v.get("failures")):
                return [], "DWS result incomplete or pagination not verified"
            msgs = v.get("messages")
            if msgs is None:
                msgs = (v.get("result") or {}).get("messages")
            if not isinstance(msgs, list):
                return [], "DWS response has no messages array"
            if v.get("count") is not None and v["count"] != len(msgs):
                return [], "DWS message count mismatch"
            if any(not isinstance(m, dict) or not (m.get("openMessageId") or m.get("messageId") or m.get("id"))
                   or not m.get("createTime") for m in msgs):
                return [], "DWS response contains malformed message"
            return [_compact_dws_message(m) for m in msgs], None
        if isinstance(v, list):
            if not v or any(not isinstance(m, dict) or not m.get("id") or not m.get("t") for m in v):
                return [], "unverified legacy DWS response; re-pull with full envelope"
            return v, None
        if not isinstance(v, dict) or not v.get("id") or not v.get("t"):
            return [], "unexpected DWS response object"
        return [v], None
    except json.JSONDecodeError:
        pass
    dec, out, idx = json.JSONDecoder(), [], 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, idx = dec.raw_decode(text, idx)
            if not isinstance(obj, dict) or not obj.get("id") or not obj.get("t"):
                return out, "unexpected DWS message object"
            out.append(obj)
        except Exception as e:
            return out, "parse fail at %d: %s" % (idx, e)
    return out, None


def read_raw(d, name):
    p = os.path.join(rdir(d), "raw", name + ".json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as stream:
        txt = stream.read().strip()
    try:  # 兼容 hook 包装
        w = json.loads(txt)
        if isinstance(w, dict) and w.get("type") == "dws_tool_result":
            txt = str(w.get("content", ""))
    except Exception:
        pass
    return txt


def archive_pull_response(d, slug, raw, expected_sha256):
    """Retain full successful pull evidence across overwrites of raw/pull files."""
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_sha256:
        raise RuntimeError("DWS raw changed before receipt archival: %s" % slug)
    archive_dir = os.path.join(rdir(d), "raw_archive")
    os.makedirs(archive_dir, exist_ok=True)
    archive_file = os.path.join(archive_dir, "pull_%s__%s.json" % (slug, expected_sha256))
    if os.path.exists(archive_file):
        with open(archive_file, encoding="utf-8") as stream:
            existing = stream.read()
        if hashlib.sha256(existing.encode("utf-8")).hexdigest() != expected_sha256:
            raise RuntimeError("DWS archived evidence hash mismatch: %s" % slug)
        return os.path.relpath(archive_file, rdir(d)).replace("\\", "/")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=archive_dir, delete=False) as stream:
            temp_name = stream.name
            stream.write(raw)
        os.replace(temp_name, archive_file)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.remove(temp_name)
    return os.path.relpath(archive_file, rdir(d)).replace("\\", "/")


def write_pull_receipt(d, st, receipts):
    """持久化每个群已验证的 DWS 响应摘要，供运行日志和故障追溯使用。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(rdir(d), "pull_receipt_%s.json" % stamp)
    for slug, receipt in receipts.items():
        if receipt.get("status") == "ok" and receipt.get("raw_sha256"):
            raw = read_raw(d, "pull_" + slug)
            if raw is None:
                raise RuntimeError("DWS raw missing before receipt archival: %s" % slug)
            receipt[RAW_PULL_ARCHIVE_FIELD] = archive_pull_response(d, slug, raw, receipt["raw_sha256"])
    payload = {"created_at": datetime.now().isoformat(timespec="seconds"),
               "date": d, "shard": P.SHARD, "only": P.ONLY,
               "groups": receipts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    st["pull_receipt_file"] = path
    st["pull_receipt"] = receipts
    save_state(d, st)
    return path


def emit(lines):
    if isinstance(lines, str):
        lines = lines.splitlines()
    print("\n".join(str(x) for x in lines))


def commit_watermark(d, st, excluded_groups=()):
    """只在「台账提交已确认成功」后把暂存水位落库。

    解析不完整的消息现在也会以 evidence-only 待审核行提交，因此不能再因
    semantic/coverage 问题把群水位留在等待状态；只有明确传入的外部排除范围
    才能阻止推进。
    """
    excluded_groups = set(excluded_groups)
    pending_wm = st.get("pending_watermark") or {}
    wm = {slug: value for slug, value in pending_wm.items() if slug not in excluded_groups}
    if not wm:
        return
    subprocess.run([PY, os.path.join(HERE, "watermark.py"), "set", "--map",
                   json.dumps(wm, ensure_ascii=False)], check=True, **HIDDEN_SUBPROCESS)
    update_committed_message_index(d, st, wm)
    receipts = st.get("pull_receipt") or {}
    for slug, receipt in receipts.items():
        if slug in wm:
            receipt["watermark_after"] = wm[slug]
            receipt["watermark_advanced"] = True
    receipt_file = st.get("pull_receipt_file")
    if receipts and receipt_file and os.path.exists(receipt_file):
        with open(receipt_file, encoding="utf-8") as f:
            payload = json.load(f)
        payload["groups"] = receipts
        payload["watermark_committed_at"] = datetime.now().isoformat(timespec="seconds")
        with open(receipt_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    st["pending_watermark"] = {slug: value for slug, value in pending_wm.items() if slug not in wm}
    save_state(d, st)
    emit(["# 水位已推 %s（本轮提交台账确认后）" % wm])


# ---------------- PULL ----------------

def refresh_incomplete_pull(d, st):
    """Fetch later source evidence without closing or resetting an incomplete run."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    snapshot = os.path.join(rdir(d), "coverage_before_pull_%s.json" % stamp)
    atomic_json(snapshot, st)
    # Keep new_ids, submission signatures, blockers and pending cursors intact.
    st["coverage_refresh"] = {"snapshot": snapshot, "previous_ids": list(st.get("new_ids") or [])}
    st["stage"] = "PULL"
    save_state(d, st)
    return step_pull(d, st)


def step_pull(d, st):
    wm = json.loads(subprocess.run([PY, os.path.join(HERE, "watermark.py"), "get"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   **HIDDEN_SUBPROCESS).stdout)
    if P.ONLY:                        # 单群手动模式：只拉这一个群（watermark.json 本身按群存，天然各推各的）
        if P.ONLY not in wm:
            emit(["ABORT: --only %s 不在 chat_sources 群清单里（可用 slug：%s）"
                  % (P.ONLY, ", ".join(sorted(wm)))])
            sys.exit(2)
        wm = {P.ONLY: wm[P.ONLY]}
    lines = ["# 阶段 PULL：执行下列 dws 命令（每条一次 Bash 调用，不拼接不重定向；"
             "允许在同一条消息里并行发起多条以省往返），"
             "把每条的完整返回内容存到指定 raw 文件（Write 工具），全部存好后执行：",
             "# " + runpy() + " advance"]
    st.setdefault("pull_targets", {})
    for g in wm.values() if isinstance(wm, dict) else wm:
        slug = [k for k, v in wm.items() if v["cid"] == g["cid"]][0] if isinstance(wm, dict) else g["slug"]
        st["pull_targets"][slug] = g["since"]
        lines.append("\n# 存 %s" % os.path.join(rdir(d), "raw", "pull_%s.json" % slug))
        lines.append('dws chat message list --group "%s" --time "%s" --direction newer '
                     '--page-all --page-limit %d --format json' % (g["cid"], g["since"], DWS_PAGE_LIMIT))
    save_state(d, st)
    emit(lines)


def committed_message_ids(slug, through):
    """Only suppress cached messages covered by the persisted committed cursor.

    A previous day's USER_REVIEW may have cached uncommitted messages; those
    must remain eligible for the next pull. The inclusive DWS cursor otherwise
    replays the final already-committed message every time.
    """
    index_path = committed_index_path()
    if os.path.exists(index_path):
        try:
            index = _load_json(index_path)
            if isinstance(index, dict) and slug in index:
                return set(index.get(slug) or [])
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("committed message index unreadable: %s" % index_path) from exc
    seen = set()
    for path in glob.glob(os.path.join(WORK, "msgs_*", slug + ".json")):
        try:
            for m in _load_json(path):
                if m.get("id") and str(m.get("t") or "") <= through:
                    seen.add(m["id"])
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("cached messages unreadable: %s" % path) from exc
    index = {}
    if os.path.exists(index_path):
        try:
            index = _load_json(index_path)
        except (OSError, ValueError, TypeError):
            index = {}
    index[slug] = sorted(seen)
    atomic_json(index_path, index)
    return seen


def update_committed_message_index(d, st, committed_groups):
    """Add only messages whose watermark was actually committed this run."""
    if not committed_groups or not st.get("new_ids"):
        return
    index = {}
    index_path = committed_index_path()
    if os.path.exists(index_path):
        try:
            index = _load_json(index_path)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("committed message index unreadable: %s" % index_path) from exc
    new_ids = set(st.get("new_ids") or [])
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    for slug in committed_groups:
        path = os.path.join(mdir, slug + ".json")
        if not os.path.exists(path):
            continue
        messages = _load_json(path)
        existing = set(index.get(slug) or [])
        existing.update(m.get("id") for m in messages
                        if isinstance(m, dict) and m.get("id") in new_ids)
        index[slug] = sorted(x for x in existing if x)
    atomic_json(index_path, index)


def advance_pull(d, st):
    targets = st.get("pull_targets", {})
    missing, errors, new_msgs, receipts = [], [], {}, {}
    for slug in targets:
        raw = read_raw(d, "pull_" + slug)
        if raw is None:
            missing.append(slug)
            continue
        if "not logged in" in raw or "DingTalk connector" in raw:
            emit(["ABORT: 钉钉未登录。水位线未推进，登录后下一轮自动补拉。"])
            sys.exit(2)
        if "pagination_fetch_error" in raw or '"retryable": true' in raw:
            n = st["retries"].get(slug, 0)
            if n < 2:
                st["retries"][slug] = n + 1
                errors.append(slug)
                continue
            emit(["ABORT: %s 连续分页失败，保留现场等人工。水位未推进。" % slug])
            sys.exit(2)
        msgs, err = parse_jsonish(raw)
        if err:
            emit(["ABORT: pull_%s 结果解析失败(%s)。水位未推进。" % (slug, err)])
            sys.exit(2)
        returned = len(msgs)
        msg_times = [str(m.get("t") or "") for m in msgs if isinstance(m, dict) and m.get("t")]
        committed = committed_message_ids(slug, targets[slug])
        new_msgs[slug] = [m for m in msgs if m.get("id") not in committed]
        receipts[slug] = {"status": "ok", "watermark_before": targets[slug],
                          "watermark_after": targets[slug], "watermark_advanced": False,
                          "returned": returned, "new": 0,
                          "latest_message_time": max(msg_times) if msg_times else "",
                          "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    save_state(d, st)
    if missing:
        emit(["# 还有 raw 未存盘：%s —— 存好后执行：" % missing, "# " + runpy() + " advance"])
        return
    if errors:
        emit(["# 这些群需重拉（删掉对应 raw 后 next 重取单群任务）：",
              "# " + runpy() + " next", "# 完成后执行：", "# " + runpy() + " advance",
              "# 涉及群：%s" % errors])
        return
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    os.makedirs(mdir, exist_ok=True)
    wm_map, total, added = {}, 0, 0
    for slug, msgs in new_msgs.items():
        if not msgs:
            continue
        p = os.path.join(mdir, slug + ".json")
        existing = _load_json(p) if os.path.exists(p) else []
        have = {m.get("id") for m in existing}
        newn = 0
        for m in msgs:
            if not isinstance(m, dict):
                continue
            m.pop("_slug", None)
            if m.get("id") not in have:
                existing.append(m)
                newn += 1
                if m.get("id") not in st.get("new_ids", []):
                    st.setdefault("new_ids", []).append(m.get("id"))
        with open(p, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=1)
        total += len(msgs)
        added += newn
        receipts[slug]["new"] = newn
        wm_map[slug] = max(m["t"] for m in msgs)
    if st.get("coverage_refresh"):
        wm_map = {**(st.get("pending_watermark") or {}), **wm_map}
    st["pending_watermark"] = wm_map
    st["stage"] = "PARSED"
    st["pulled_total"] = added
    receipt_file = write_pull_receipt(d, st, receipts)
    emit(["# PULL 完成：拉取 %d 条（新增 %d 条），水位待提交成功后推 %s"
          % (total, added, wm_map or "（无新消息）"),
          "# DWS 拉取回执：%s" % receipt_file])
    if st.get("coverage_refresh") and added == 0:
        st["stage"] = "COVERAGE_PENDING"
        save_state(d, st)
        emit(["COVERAGE_PENDING: 补拉无新增；原来源、提交索引及阻塞保留，未推进水位。"])
        return
    if st.pop("coverage_refresh", None):
        # New sources need their own first precheck. Already submitted rows
        # remain protected by submit_done_records, including final failures.
        st.pop("agent_repair_completed", None)
        st.pop("repair_submission", None)
        st.pop("precheck_validated", None)
    # Replayed raw files must be parsed again; do not advance their watermark.
    if added == 0 and not any(new_msgs.values()):
        st["stage"] = "DONE"
        save_state(d, st)
        commit_watermark(d, st)
        emit(["DONE: 本轮无新增消息，静默结束，不提交台账。"])
        return
    if added == 0:
        st["new_ids"] = sorted({m.get("id") for ms in new_msgs.values() for m in ms if isinstance(m, dict) and m.get("id")})
        st["pulled_total"] = 0
    run_parse(d, st)


def refresh_publisher_policy(d, st):
    identity = publisher_snapshot(WORK, groups=GROUPS)
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    facts, excluded = [], set()
    for fn in sorted(os.listdir(mdir)):
        if not fn.endswith(".json") or fn[:-5] not in GNAME:
            continue
        with open(os.path.join(mdir, fn), encoding="utf-8") as stream:
            messages = json.load(stream)
        for message in messages:
            if message.get("id") not in st.get("new_ids", []):
                continue
            decision = publisher_decision(identity, fn[:-5], message)
            facts.append(dict(decision, group=fn[:-5], message_id=message["id"],
                              sender=message.get("s", ""), time=message.get("t", "")))
            if decision["action"] == "exclude":
                excluded.add(message["id"])
    revision = identity["data"]["roster_revision"]
    audit = {"ok": True, "data": {"generated_at": identity_now(), "roster_revision": revision,
             "mode": identity["data"]["mode"], "messages": facts,
             "counts": {"total": len(facts), "excluded": len(excluded),
                        "recognized": sum(f["status"] == "publisher" and f["identity_ready"] for f in facts),
                        "identity_review": sum(f["shadow_action"] == "identity_review" and f["action"] != "exclude" for f in facts)}}}
    audit_name = "identity_decisions_r%s_%s.json" % (revision, datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    atomic_json(os.path.join(rdir(d), audit_name), audit)
    # Derived observation queue, separate from quote approval/check conclusions.
    atomic_json(os.path.join(rdir(d), "identity_review_queue.json"), {"ok": True, "data": {
        "roster_revision": revision, "messages": [f for f in facts if f["shadow_action"] == "identity_review" and f["action"] != "exclude"]}})
    st["publisher_revision"] = revision
    st["publisher_excluded_ids"] = sorted(excluded)
    st["publisher_audit_file"] = audit_name
    save_state(d, st)
    emit(["# 航管账号名单 r%s：识别 %d，观察 %d，人工排除 %d；依据 %s" % (
        revision, audit["data"]["counts"]["recognized"], audit["data"]["counts"]["identity_review"], len(excluded), audit_name)])
    return identity


def verified_publisher_exclusions(d, st):
    excluded = set(st.get("publisher_excluded_ids") or [])
    if not excluded:
        return excluded
    folder = os.path.abspath(rdir(d))
    path = os.path.abspath(os.path.join(folder, st.get("publisher_audit_file") or ""))
    if os.path.commonpath([folder, path]) != folder:
        raise RuntimeError("航管排除审计路径越界，水位不推进")
    with open(path, encoding="utf-8") as stream:
        evidence = json.load(stream)["data"]
    if evidence.get("roster_revision") != st.get("publisher_revision"):
        raise RuntimeError("航管排除审计修订号不一致，水位不推进")
    proven = {fact["message_id"] for fact in evidence["messages"]
              if fact.get("action") == "exclude"}
    if not excluded.issubset(proven):
        raise RuntimeError("航管排除缺少持久化身份审计，水位不推进")
    return excluded


def publisher_policy_changed(st):
    return st.get("publisher_revision") != load_roster().get("revision", 0)


def _build_fingerprint(d, st, new_ids):
    """Hash only inputs that can change record_builder output for this run."""
    h = hashlib.sha256()
    for path in (os.path.join(HERE, "record_builder.py"), P.CHAT_SOURCES_F, P.CONTRACT_F):
        with open(path, "rb") as stream:
            h.update(stream.read())
    h.update(json.dumps(sorted(new_ids), ensure_ascii=False).encode("utf-8"))
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    media_ids = set()
    for fn in sorted(os.listdir(mdir)) if os.path.isdir(mdir) else []:
        if not fn.endswith(".json"):
            continue
        messages = _load_json(os.path.join(mdir, fn))
        for message in messages:
            if message.get("id") in new_ids:
                h.update(json.dumps(message, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                media_ids.update(message.get("img") or [])
    for media_id in sorted(media_ids):
        cache = os.path.join(WORK, "ocr_cache", media_id.lstrip("@") + ".json")
        h.update(media_id.encode("utf-8"))
        if os.path.exists(cache):
            with open(cache, "rb") as stream:
                h.update(stream.read())
    h.update(json.dumps(sorted(st.get("publisher_excluded_ids") or []), ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def _run_builder_if_needed(d, st, new_ids, quiet=False):
    manifest_path = os.path.join(rdir(d), "build_manifest.json")
    expected = {"fingerprint": _build_fingerprint(d, st, new_ids),
                "message_ids": sorted(new_ids)}
    draft_dir = os.path.join(WORK, "draft_%s%s" % (d, P.sfx()))
    outputs = [os.path.join(draft_dir, name)
               for name in ("draft_rate.json", "draft_space.json", "needs_llm.json")]
    if os.path.exists(manifest_path) and all(os.path.exists(path) for path in outputs):
        try:
            if _load_json(manifest_path) == expected:
                return False
        except (OSError, ValueError, TypeError):
            pass
    builder_cmd = [PY, os.path.join(HERE, "record_builder.py"), "--date", d]
    for message_id in sorted(new_ids):
        builder_cmd.extend(["--message-id", message_id])
    subprocess.run(builder_cmd, check=True, stdout=subprocess.DEVNULL if quiet else None,
                   **HIDDEN_SUBPROCESS)
    atomic_json(manifest_path, expected)
    return True


def run_parse(d, st):
    identity = refresh_publisher_policy(d, st)
    new_ids = set(st.get("new_ids") or [])
    if new_ids and not _ensure_knowledge(d, st, new_ids, "PARSE"):
        return
    _run_builder_if_needed(d, st, new_ids)
    dr = os.path.join(WORK, "draft_%s%s" % (d, P.sfx()))
    needs = _load_json(os.path.join(dr, "needs_llm.json"))
    img_dir = db_client.images_dir()
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    excluded = set(st.get("publisher_excluded_ids") or [])
    queue = [q for q in needs if q["id"] not in excluded]
    for fn in sorted(os.listdir(mdir)):
        for m in _load_json(os.path.join(mdir, fn)):
            if m.get("id") not in new_ids:
                continue
            if publisher_decision(identity, fn[:-5], m)["action"] == "exclude":
                continue
            for mid in m.get("img") or []:
                cache = os.path.join(WORK, "ocr_cache", mid.lstrip("@") + ".json")
                need_ocr = not os.path.exists(cache)
                # 台账看板从 Database/images/<mediaId>* 取图；缺图则待下载归位
                need_file = not _image_present(img_dir, mid)
                if need_ocr or need_file:
                    queue.append({"group": fn[:-5], "t": m["t"], "s": m["s"], "id": m["id"],
                                  "reason": ("需识图并写ocr_cache " if need_ocr else "")
                                            + ("需下载原图存台账images目录" if need_file else ""),
                                  "img": [mid], "need_ocr": need_ocr, "need_image": need_file})
    queue = list({q["id"] + "|" + q.get("reason", ""): q for q in queue}.values())
    # 规范路由数据化：队列项直接带"该读哪份规范 + 有几条待补"，Agent 不用再人肉对 README 的映射表
    no_spec = []
    for q in queue:
        b = spec_index.brief(q.get("group"))
        q.update(b)
        if not b["spec_path"]:
            no_spec.append(str(q.get("group") or "?"))
    st["stage"] = "LLM_PENDING" if queue else "BUILD"
    save_state(d, st)
    if not queue:
        step_build(d, st)
        return
    qf = os.path.join(rdir(d), "llm_queue.json")
    with open(qf, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=1)
    _sh = (" --shard %s" % P.SHARD if P.SHARD else "") + (" --only %s" % P.ONLY if P.ONLY else "")
    tips = ["# 阶段 LLM：%d 项待判定，清单见 %s" % (len(queue), qf),
            "# Agent 职责（唯一需要智能的一步）：",
            "# 1) 读 scripts/llm_output_contract.md 契约 + 每条队列项 spec_path 指向的该群规范"
            "（若该项有 spec_base_path，先读共享基础再读本群差异；"
            "若有 spec_knowledge_path，必须继续读取该群业务知识沉淀后再判定）"
            "（spec_todo 列出的条目＝尚无实测依据，禁止猜填，按全局口径留空交人工）",
            "# 2) 新图：dws 下载原图 → Read 识图 → 写 .work/ocr_cache/<mediaId>.json；"
            "并把下载到的原图放进台账 %s（文件名以 mediaId 开头，看板按 mediaId 归位取图）→ 重跑 next" % img_dir,
            "# 3) 每条判定写 .work/llm_out_%s%s/<safe(msg_id)>.json（严格按契约），"
            "rule_basis 必须落在真实锚点：规范.md#小节 或 extraction-rules.md#条款号" % (d, P.sfx()),
            "# 4) 先做证据回读：原文明确出现的船司、港口、价格、箱型、船名航次、ETD，"
            "必须逐字段写入 records；不得因为格式混排就产出空 space。任何带价格条目先产 rate，带价现舱再额外产同源 space（extraction-rules.md#27）；金额只写 rate 的海运费USD，space 的价格USD留空；价格USD、舱位类型、箱量等只留在 space，不得混入 rate。",
            "# 5) 全部写好后先自查：python scripts/check_llm_out.py --date %s%s"
            "（错误清零再推进；可疑项交人工看；若原文有价格/舱位事实而 records 全空，机器闸门会阻止推进）" % (d, _sh),
            "# 6) " + runpy() + " advance"]
    if no_spec:
        tips.insert(2, "# ⚠ 这些群没定位到规范文件（先补 references/群聊规范/<群>/规范.md 并在 chat_sources 填 spec_dir）：%s"
                    % ", ".join(sorted(set(no_spec))))
    emit(tips)


def _current_message_groups(d, message_ids):
    """Resolve current message IDs to their persisted source groups."""
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    found = {}
    if os.path.isdir(mdir):
        for fn in sorted(os.listdir(mdir)):
            if not fn.endswith(".json") or fn[:-5] not in GNAME:
                continue
            path = os.path.join(mdir, fn)
            with open(path, encoding="utf-8") as stream:
                messages = json.load(stream)
            for message in messages:
                if message.get("id") in message_ids:
                    found[message["id"]] = fn[:-5]
    missing = sorted(set(message_ids) - set(found))
    if missing:
        raise knowledge_gate.KnowledgeGateError(
            "本轮消息未找到落盘来源，拒绝继续：%s" % ", ".join(missing))
    return sorted(set(found.values()))


def _ensure_knowledge(d, st, message_ids, phase):
    """Fail closed when required global or group knowledge is unavailable."""
    try:
        groups = _current_message_groups(d, message_ids)
        receipt = knowledge_gate.ensure(rdir(d), groups, message_ids, phase)
    except (knowledge_gate.KnowledgeGateError, OSError, ValueError, TypeError) as exc:
        st["knowledge_gate"] = {"ok": False, "phase": phase, "error": str(exc),
                                "message_ids": sorted(message_ids)}
        st["stage"] = "LLM_PENDING"
        save_state(d, st)
        emit(["ABORT: 知识文件读取闸门失败，相关消息隔离，水位未推进：%s" % exc,
              "修复知识文件或群路由后继续：%s advance" % runpy()])
        return False
    st["knowledge_gate"] = {"ok": True, "phase": phase,
                            "receipt": os.path.join(rdir(d), knowledge_gate.MANIFEST_NAME),
                            "groups": receipt["groups"], "file_count": len(receipt["files"])}
    save_state(d, st)
    return True


def _image_present(img_dir, media_id):
    if not img_dir or not os.path.isdir(img_dir):
        return False
    stem = (media_id or "").lstrip("@")[:16]
    return any(f.startswith(stem) for f in os.listdir(img_dir)) if stem else False


# ---------------- BUILD（合并 draft + llm_out，产中文键记录） ----------------

def _to_int(v):
    try:
        if v in (None, ""):
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return v  # 交给 /api/validate 判非整数


def _coerce(rec):
    """整数列规整 + 去掉空值；港群/多港不拆（交台账 Master 归一）。"""
    out = {}
    for k, v in rec.items():
        if v in (None, "", []):
            continue
        if k in ("海运费USD", "价格USD", "目免", "亏舱费", "T/T航程", "箱量"):
            iv = _to_int(v)
            if iv is not None:
                out[k] = iv
        else:
            out[k] = v
    return out


COMBO_CTYPE_PARTS = {
    "20GP/40HQ/HC": ("20GP", "40HQ/HC"),
    "20GP/40HQ": ("20GP", "40HQ/HC"),
    "20GP/HC": ("20GP", "40HQ/HC"),
    "20GP/40HQ/HC/40NOR": ("20GP", "40HQ/HC", "40NOR"),
    "20GP/40HQ/HC/40NOR/45HQ/HC": ("20GP", "40HQ/HC", "40NOR", "45HQ/HC"),
}
COUNTRY_SUFFIX_RE = re.compile(
    r",\s*(Argentina|Uruguay|Israel|Greece|Turkey|Bulgaria|Egypt|Romania|Ukraine|Georgia|Germany|Belgium|Netherlands|United Kingdom|France|Spain|Italy|Portugal|Poland|Finland|Sweden|Denmark)\s*$",
    re.I,
)
LONG_PAREN_NOTE_RE = re.compile(r"\s*\(([^)]+)\)")


def _price_parts(v):
    text = str(v or "").strip()
    if not text or "/" not in text:
        return ()
    parts = [p.strip() for p in text.split("/")]
    out = []
    for part in parts:
        if not re.fullmatch(r"\d+(?:\.0+)?", part):
            return ()
        out.append(int(float(part)))
    return tuple(out)


def _expand_combo_rate(rec):
    ctype = str(rec.get("柜型") or "").strip()
    ctypes = COMBO_CTYPE_PARTS.get(ctype)
    prices = _price_parts(rec.get("海运费USD"))
    if not ctypes or len(ctypes) != len(prices):
        return [rec]
    out = []
    for one_ctype, one_price in zip(ctypes, prices):
        one = dict(rec)
        one["柜型"] = one_ctype
        one["海运费USD"] = one_price
        out.append(one)
    return out


def _split_pod(rec):
    pod = str(rec.get("目的港") or "").strip()
    if not pod or "/" not in pod:
        return [rec]
    parts = [p.strip() for p in re.split(r"\s*/\s*", pod) if p.strip()]
    if len(parts) <= 1:
        return [rec]
    out = []
    for one_pod in parts:
        one = dict(rec)
        one["目的港"] = one_pod
        out.append(one)
    return out


def _normalize_pol_name(value):
    """Normalize known origin spelling variants before master-data validation."""
    pol = str(value or "").strip()
    if pol.casefold() == "hongkong":
        return "Hong Kong"
    return pol


def _split_pol(rec):
    """One rate record must represent exactly one origin port or origin group."""
    pol = str(rec.get("起运港") or "").strip()
    if not pol or not re.search(r"[/、，,+]", pol):
        if pol:
            rec["起运港"] = _normalize_pol_name(pol)
        return [rec]
    parts = [_normalize_pol_name(p) for p in re.split(r"\s*[/、，,+]\s*", pol) if p.strip()]
    if len(parts) <= 1:
        rec["起运港"] = parts[0] if parts else pol
        return [rec]
    out = []
    for one_pol in parts:
        one = dict(rec)
        one["起运港"] = one_pol
        out.append(one)
    return out


def _append_remark(rec, text):
    text = str(text or "").strip()
    if not text:
        return
    old = str(rec.get("备注") or "").strip()
    if text in old:
        return
    rec["备注"] = (old + "；" + text) if old else text


def _normalize_pod(rec):
    pod = str(rec.get("目的港") or "").strip()
    if not pod:
        return rec
    original = pod
    notes = []

    def _paren(m):
        inner = m.group(1).strip()
        if re.fullmatch(r"[A-Z]{2,3}", inner):
            return m.group(0)
        notes.append(inner)
        return ""

    pod = LONG_PAREN_NOTE_RE.sub(_paren, pod).strip()
    m = COUNTRY_SUFFIX_RE.search(pod)
    if m:
        notes.append(m.group(1))
        pod = COUNTRY_SUFFIX_RE.sub("", pod).strip()
    if pod.lower() == "ambarli port istanbul":
        notes.append("原目的港写法：Ambarli Port Istanbul")
        pod = "Ambarli"

    if pod != original:
        rec["目的港"] = pod
        if notes:
            _append_remark(rec, "目的港说明：" + " / ".join(notes))
    return rec


def _apply_default_pol(rec, group):
    if not rec.get("起运港") and DEFAULT_POL.get(group):
        rec["起运港"] = DEFAULT_POL[group]
    return rec


def _enum_ok(kind, rec):
    if kind == "rate" and rec.get("柜型") and rec["柜型"] not in CTYPE:
        return False
    if kind == "space":
        if rec.get("舱位类型") and rec["舱位类型"] not in STYPE:
            return False
        if rec.get("柜型") and rec["柜型"] not in CTYPE:
            return False
    return True


def _load_draft(d, name):
    dr = os.path.join(WORK, "draft_%s%s" % (d, P.sfx()))
    return _load_json(os.path.join(dr, "draft_%s.json" % name))


def _report_blocked_issues(blocked, msgs_by_id):
    issues = []
    for mid, reasons in sorted(blocked.items()):
        src = msgs_by_id.get(mid)
        if not src or not src.get("_slug"):
            emit(["ABORT: 待核问题缺少来源消息：%s" % mid])
            sys.exit(2)
        issues.append({"source_group": src["_slug"], "message_id": mid,
                       "message_text": compose_text(src), "media_ids": src.get("img") or [],
                       "reasons": list(dict.fromkeys(reasons))})
    for offset in range(0, len(issues), db_client.REVIEW_ISSUES_BATCH_LIMIT):
        batch = issues[offset:offset + db_client.REVIEW_ISSUES_BATCH_LIMIT]
        result = db_client.report_review_issues(batch)
        if not result.get("ok") or result.get("data", {}).get("count") != len(batch):
            emit(["ABORT: 待核问题未进入航管界面；保持水位，稍后重试：%s" % result.get("error", result)])
            sys.exit(2)


def _evidence_only_record(source, reason):
    """为无法完成结构化解析的消息保留一条可审核的来源证据行。

    该行故意不猜港口、船司、柜型或价格；/api/pending 会把它标为 failed/awaiting，
    但消息原文、消息 ID、来源和失败原因均进入待审核库，定时任务可以继续收尾。
    """
    group = source.get("_slug") or ""
    return {
        "来源群": GNAME.get(group, group),
        "来源群ID": next((g.get("cid", "") for g in GROUPS if g.get("slug") == group), ""),
        "发送人": source.get("s", ""),
        "发送人ID": source.get("senderId") or "",
        "发送人OpenID": source.get("senderOpenDingTalkId") or "",
        "消息时间": str(source.get("t") or "")[:16],
        "消息原文": compose_text(source),
        "消息ID": source.get("id", ""),
        "备注": "自动收录待补解析；" + str(reason or "本轮未生成可确认的结构化记录"),
        "解析状态": "待补解析",
    }


def _is_surcharge_only_notice(text: str) -> bool:
    """Recognize explicit surcharge updates without mistaking a full quote for noise."""
    text = (text or "").strip()
    surcharge_update = re.search(
        r"(?i)(?:超重费|附加费)\s*(?:调整|更新)|OVERWEIGHT\s+SURCHARGE(?:\s*\(OWS\))?\s+UPDATE",
        text,
    )
    if not surcharge_update:
        return False
    base_quote = re.search(
        r"(?i)(?<![\d.])\d{3,5}\s*/\s*\d{3,5}(?![\d.])|"
        r"(?:FAK|SPOT|运价|海运费|卖价|成本价|包舱价)\s*(?:更新|报价|价格)?",
        text,
    )
    return base_quote is None


def classify_message_intent(text: str) -> str:
    """Classify current-message intent; mentions and quoted text are not intent."""
    text = (text or "").strip()
    if not text:
        return "unknown"
    inquiry_words = ("能走吗", "能接吗", "还有吗", "还能做吗", "有位置吗", "好放不", "放舱吗", "报一下", "多少钱")
    if any(word in text for word in inquiry_words) or re.search(r"(?i)\b(?:plz|please)\s+quote\b", text):
        return "inquiry"
    if _is_surcharge_only_notice(text):
        return "surcharge_notice"
    price_pattern = r"(?i)(?<![\d.])\d{3,5}\s*/\s*\d{3,5}(?![\d.])|(?:USD|US\$|\$)\s*\d{3,5}"
    quote_words = ("价格表", "价卡", "指导卖价", "运价更新", "SPOT LEVEL", "FAK成本")
    if re.search(price_pattern, text) or any(word in text for word in quote_words):
        return "quote"
    operation_words = ("开舱", "开港", "截关", "截单", "催单", "放舱", "通知", "退关", "转船改配")
    if any(word in text for word in operation_words):
        return "op_notice"
    return "unknown"


def _is_obvious_sales_chat(source) -> bool:
    """Identify chat candidates; callers must not discard them without semantic review."""
    if not isinstance(source, dict) or source.get("img") or source.get("q"):
        return False
    text = str(source.get("c") or "").strip()
    if not text or len(text) > 80:
        return False
    if re.search(r"(?i)(?:\d{3,5}\s*/\s*\d{3,5}|USD\s*\d+|\$\s*\d+|\b(?:ETD|FAK|NOR|HQ|GP)\b)", text):
        return False
    chat_words = (
        "麻烦做", "麻烦看", "看一下", "看下", "这个呢", "还有吗", "有价格吗",
        "多少钱", "能走吗", "能接吗", "有位置吗", "报一下", "收到", "好的",
        "谢谢", "辛苦", "怎么做", "可以吗",
    )
    return text.startswith("@") or any(word in text for word in chat_words) or text.endswith(("吗", "呢", "？", "?"))


def _non_ledger_notice_ids(sources, message_ids):
    """Return only explicit surcharge-only notices; chat candidates need LLM review."""
    message_ids = set(message_ids)
    result = set()
    for mid in message_ids:
        source = sources.get(mid) or {}
        if classify_message_intent(source.get("c") or "") != "surcharge_notice":
            continue
        result.add(mid)
        quoted_id = str((source.get("q") or {}).get("id") or "")
        if quoted_id in message_ids:
            result.add(quoted_id)
    return result


def _intent_blocked_messages(sources, candidates):
    """Check assembled records per source, allowing reviewed noise and space facts."""
    tables_by_source = {}
    for candidate in candidates:
        meta = candidate.get("_meta") or {}
        tables_by_source.setdefault(meta.get("msg_id"), set()).add(meta.get("table"))
    blocked = {}
    for mid, tables in tables_by_source.items():
        intent = classify_message_intent((sources.get(mid) or {}).get("c") or "")
        if intent == "inquiry" or (intent == "op_notice" and "rate" in tables):
            blocked[mid] = ["消息意图=%s，不应生成新报价" % intent]
    return blocked


def step_build(d, st):
    repair_completed = st.get("stage") in (AGENT_REPAIR_STAGE, "USER_REVIEW") or st.get("agent_repair_completed", False)
    if publisher_policy_changed(st):
        return run_parse(d, st)
    # OCR can arrive after the initial parse. Rebuild templates from the
    # latest cache before deciding that a message produced no records.
    source_ids = set(st.get("new_ids") or [])
    new_ids = source_ids - set(st.get("publisher_excluded_ids") or [])
    if new_ids and not _ensure_knowledge(d, st, new_ids, "BUILD"):
        return
    _run_builder_if_needed(d, st, new_ids, quiet=True)
    ldir = os.path.join(WORK, "llm_out_%s%s" % (d, P.sfx()))
    replace_template = set()
    for path in (glob.glob(os.path.join(ldir, "*.json")) if os.path.isdir(ldir) else []):
        try:
            decision = _load_json(path)
        except (ValueError, OSError):
            continue  # The structured checker below isolates unreadable decision files.
        for item in (decision if isinstance(decision, list) else [decision]):
            if isinstance(item,dict) and item.get("msg_id") in new_ids and item.get("replace_template") is True:
                replace_template.add(item["msg_id"])
    recs = []
    for tbl in ("rate", "space"):
        for x in _load_draft(d, tbl):
            if x["_meta"]["msg_id"] in new_ids and x["_meta"]["msg_id"] not in replace_template:
                recs.append(x)
    msgs_by_id = {}
    mdir = os.path.join(WORK, "msgs_%s%s" % (d, P.sfx()))
    for fn in os.listdir(mdir):
        for m in _load_json(os.path.join(mdir, fn)):
            msgs_by_id[m["id"]] = dict(m, _slug=fn[:-5])
    non_ledger_ids = _non_ledger_notice_ids(msgs_by_id, new_ids)
    # Independent source-price/equipment and image checks below use both drafts
    # and decisions. Do not scan a second hardcoded port list or voyage numbers.
    blocked = {}
    # 合并 llm_out（LLM 判定也是中文键记录，_table 决定进哪库）
    # 2026-09-17 审查修复：加 new_ids 门控（与 draft 分支一致）。llm_out_<D>/ 按天累积不清，
    # cron 同日多轮时每轮 glob 到全天判定文件，无门控会把早轮记录全部重建重投 → 重复入库
    ldir = os.path.join(WORK, "llm_out_%s%s" % (d, P.sfx()))
    # Hard provenance/rule errors isolate a source message; ordinary warnings remain advisory.
    try:
        _rep = llm_check.check(d, work=WORK, msgs_by_id=msgs_by_id,
                               message_ids=sorted(new_ids))
        if _rep["n_err"] or _rep["n_warn"]:
            emit(llm_check.summary_lines(_rep))
    except Exception as _e:
        _rep = {'blocked_messages':{mid:['判定自查失败：'+str(_e)] for mid in new_ids},'unreadable_files':[]}
        emit(["# 判定自查失败：隔离本轮消息，保留水位：%s" % _e])
    for mid, errors in _rep.get('blocked_messages', {}).items():
        if mid in new_ids:
            blocked.setdefault(mid, []).extend(errors)
    from freight_images.services.completeness import matrix_errors
    for mid in new_ids:
        source = msgs_by_id.get(mid, {})
        for media_id in source.get("img") or []:
            cache_path = os.path.join(WORK, "ocr_cache", str(media_id).lstrip("@") + ".json")
            if not os.path.exists(cache_path):
                blocked.setdefault(mid, []).append("图片尚无OCR缓存，不能仅凭文字判定继续提交")
                continue
            try:
                with open(cache_path, encoding="utf-8") as stream:
                    cache_errors = matrix_errors(json.load(stream), source.get("_slug") or "")
            except (OSError, ValueError, TypeError, AttributeError) as error:
                cache_errors = ["图片缓存无法核对：" + str(error)]
            if cache_errors:
                blocked.setdefault(mid, []).extend(cache_errors)
    draft_ids = {r['_meta']['msg_id'] for r in recs}
    for mid in new_ids - draft_ids - set(_rep.get('checked_message_ids',[])):
        blocked.setdefault(mid,[]).append('本轮消息没有模板结果或有效判定，不能按无报价跳过')
    if _rep.get('unreadable_files'):
        for mid in new_ids - draft_ids - set(_rep.get('checked_message_ids',[])):
            blocked.setdefault(mid,[]).append('判定文件损坏，无法确认来源：'+','.join(_rep['unreadable_files']))
    st['semantic_issues'] = [{'message_id':mid,'errors':errors} for mid,errors in blocked.items()]
    if _rep.get('unreadable_files'):
        st['semantic_issues'].append({'message_id':'','errors':['无法归属的损坏文件：'+','.join(_rep['unreadable_files'])]})
    st['review_blocked_groups'] = sorted(set(st.get('coverage_blocked_groups') or []) |
        {msgs_by_id[mid]['_slug'] for mid in blocked if mid in msgs_by_id})
    recs = [r for r in recs if r['_meta']['msg_id'] not in blocked]
    merged_decisions = set()
    for _p in (sorted(glob.glob(os.path.join(ldir, "*.json"))) if os.path.isdir(ldir) else []):
        if os.path.basename(_p) in _rep.get('unreadable_files',[]):
            continue
        _d = _load_json(_p)
        for o in (_d if isinstance(_d, list) else [_d]):
            if not isinstance(o,dict) or o.get('msg_id') in blocked:
                continue
            if o.get('decision') == 'template' and o.get('msg_id') not in draft_ids:
                blocked.setdefault(o.get('msg_id'), []).append('template判定没有对应模板候选，不能用空记录代替识别')
                st['semantic_issues'].append({'message_id':o.get('msg_id'),'errors':blocked[o.get('msg_id')]})
                st['review_blocked_groups'] = sorted(set(st['review_blocked_groups']) | {o.get('group')})
                continue
            if o.get('msg_id') in merged_decisions:
                continue
            merged_decisions.add(o.get('msg_id'))
            if o.get("msg_id") not in new_ids:
                continue    # 非本轮新消息：早轮已提交过，跳过
            src = msgs_by_id.get(o.get("msg_id"))
            group = o.get("group")
            if not src:
                emit(["ABORT: llm_out msg_id %s 不在落盘消息中，拒绝（溯源铁律）" % o.get("msg_id")])
                sys.exit(2)
            for r in o.get("records", []):
                tbl = r.pop("_table", "rate")
                rec = {k: v for k, v in r.items() if v not in (None, "")}
                if not rec.get("起运港") and DEFAULT_POL.get(group):
                    rec["起运港"] = DEFAULT_POL[group]
                rec["来源群"] = GNAME.get(group, src.get("_slug", group))
                rec["发送人"] = src["s"]
                rec["来源群ID"] = next((g.get("cid", "") for g in GROUPS if g.get("slug") == group), "")
                rec["发送人ID"] = src.get("senderId") or ""
                rec["发送人OpenID"] = src.get("senderOpenDingTalkId") or ""
                rec["消息时间"] = src["t"][:16]
                rec["消息原文"] = compose_text(src)
                rec["消息ID"] = src["id"]
                vtxt = r.get("有效期船期") or ""
                vf, vt, ev, ai = vdates(vtxt, src["t"][:10], group)
                for _k, _v in (("有效期起", vf), ("有效期止", vt), ("船期ETD", ev)):
                    if _v:
                        rec[_k] = _v
                if ai:
                    rec["AI判断"] = "是"   # 运价/舱位都必填有效期；兜底窗打 AI判断 标签
                recs.append({"record": rec, "_meta": {"table": tbl, "group": o.get("group"),
                                                       "msg_id": o["msg_id"], "conf": o.get("confidence", "llm")}})
    for mid, errors in _intent_blocked_messages(msgs_by_id, recs).items():
        blocked.setdefault(mid, []).extend(errors)
    for mid in non_ledger_ids:
        blocked.pop(mid, None)
    st['semantic_issues'] = [{'message_id': mid, 'errors': errors}
                             for mid, errors in blocked.items()]
    if _rep.get('unreadable_files'):
        st['semantic_issues'].append({'message_id': '', 'errors': [
            '无法归属的损坏文件：' + ','.join(_rep['unreadable_files'])]})
    st['review_blocked_groups'] = sorted(set(st.get('coverage_blocked_groups') or []) |
        {msgs_by_id[mid]['_slug'] for mid in blocked if mid in msgs_by_id})
    recs = [item for item in recs
            if item['_meta']['msg_id'] not in blocked
            and item['_meta']['msg_id'] not in non_ledger_ids]
    # 图片引用：源消息带图的，图片名=mediaId 列表（台账看板按 mediaId 归位取 Database/images 里的图）
    for r in recs:
        src = msgs_by_id.get(r.get("_meta", {}).get("msg_id"))
        if src and src.get("img"):
            r["record"]["图片名"] = ",".join(src["img"])
    # 规整（2026-09-17 审查修复：删除 _enum_ok 本地静默丢弃。原注释"台账服务端不强校验"已过时——
    # 服务端闸门现强校验柜型四档 + 舱位类型 11 档，非法值照提交会被判 check_status='failed'
    # 进人工复核队列；本地丢弃 + 水位照推 = 该消息永久丢失、无人知道）
    clean = []
    for r in recs:
        kind = "rates" if r["_meta"]["table"] == "rate" else "space"
        base_record = _apply_default_pol(dict(r["record"]), r.get("_meta", {}).get("group"))
        if kind == "rates":
            base_record = _normalize_pod(base_record)
        records = _expand_combo_rate(base_record) if kind == "rates" else [base_record]
        records = [p for rec in records for p in _split_pol(rec)]
        records = [p for rec in records for p in _split_pod(rec)]
        for raw_rec in records:
            rec = _coerce(raw_rec)
            clean.append({"kind": kind, "record": rec,
                          "confidence": r["_meta"].get("conf", "high")})
    # 防呆闸门：判定目录为空时仍要保留来源证据，不能让定时任务停在等待态。
    # 这里不猜业务字段，而是生成 evidence-only 行；它会以 failed/awaiting 进入待审核。
    _report_blocked_issues(blocked, msgs_by_id)
    covered_ids = {item["record"].get("消息ID") for item in clean}
    evidence_only_ids = sorted(new_ids - covered_ids - non_ledger_ids)
    for mid in evidence_only_ids:
        source = msgs_by_id.get(mid)
        if not source:
            continue
        reasons = blocked.get(mid) or ["本轮没有生成可确认的结构化记录"]
        clean.append({"kind": "rates", "record": _evidence_only_record(source, "；".join(reasons)),
                      "confidence": "evidence_only", "force_pending": True})
    st["evidence_only_message_ids"] = evidence_only_ids
    st["non_ledger_message_ids"] = sorted(non_ledger_ids)
    submitted = set(st.get("submit_done_records") or [])
    st["expected_submission"] = clean
    clean = [item for item in clean if _record_sig(item["kind"], item["record"]) not in submitted]
    st["submit_queue"] = clean
    st["submit_done"] = []
    if repair_completed:
        st["agent_repair_completed"] = True
        st["repair_submission"] = sorted(_record_sig(item["kind"], item["record"]) for item in clean)
        st["repair_issues"] = []
        st["precheck_issues"] = 0
    st["stage"] = "SUBMIT" if repair_completed else "PRECHECK"
    save_state(d, st)
    n_rate = sum(1 for x in clean if x["kind"] == "rates")
    n_space = sum(1 for x in clean if x["kind"] == "space")
    action = "Agent已完成一轮修正，直接提交pending，最终未过交人工" if repair_completed else "先执行无写入预检"
    emit(["# 阶段 %s：候选记录已就绪（运价 %d / 舱位 %d），%s：" % (st["stage"], n_rate, n_space, action),
          "# " + runpy() + " advance"])


# ---------------- SUBMIT（同步 POST /api/validate 预检 + /api/pending，成功才推水位） ----------------

def _chunk_sig(kind, part):
    basis = json.dumps([kind, part], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def _write_submit_receipt(d, kind, items, sig, staged, coverage=None):
    """Keep a separate, immutable coverage proof across per-shard state resets."""
    folder = os.path.join(WORK, "submit_receipts")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s%s-%s.json" % (d, P.sfx(), sig))
    payload = {"submitted_at": datetime.now().isoformat(timespec="seconds"),
               "kind": kind, "staged": staged, "requested": len(items), "message_ids": sorted({
                   str(item["record"].get("消息ID")) for item in items
                   if item["record"].get("消息ID")}), "signature": sig, "coverage": coverage}
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(temp, path)

def _record_sig(kind, record):
    return hashlib.sha256(json.dumps([kind, record], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _build_jobs(st):
    submitted = set(st.get("submit_done_records") or [])
    q = [x for x in st.get("submit_queue") or [] if _record_sig(x["kind"], x["record"]) not in submitted]
    jobs = []
    for kind in ("rates", "space"):
        items = [x for x in q if x["kind"] == kind]
        for start in range(0, len(items), BATCH_RECORD_LIMIT):
            part = items[start:start + BATCH_RECORD_LIMIT]
            jobs.append((kind, part, _chunk_sig(kind, [x["record"] for x in part])))
    return jobs


def _review_path(d):
    return os.path.join(rdir(d), "precheck_review.json")


def _repair_dossier(d, st, issues):
    """Give the running Agent errors, canonical master data and business-spec paths."""
    master = db_client.master()
    if not master.get("ok"):
        raise RuntimeError("Cannot load authoritative masters for Agent repair: " + str(master.get("error")))
    master_path = os.path.join(rdir(d), "repair_master.json")
    with open(master_path, "w", encoding="utf-8") as file:
        json.dump(master, file, ensure_ascii=False, indent=1)
    for issue in issues:
        group = issue["record"].get("来源群")
        slug = next((s for s, name in GNAME.items() if name == group), "")
        issue["group"] = slug
        issue.update(spec_index.brief(slug))
    with open(_review_path(d), "w", encoding="utf-8") as file:
        json.dump({"date": d, "stage": AGENT_REPAIR_STAGE, "master_path": master_path,
                   "issues": issues, "instructions": [
                       "Read the source and inherited group specifications, original message and image.",
                       "Resolve against canonical masters. A known port with missing shorthand needs a scoped alias.",
                       "Search official sources for genuinely absent ports; ensure_master adds only verified ports/aliases.",
                       "Attempt one evidence-backed correction of OCR/llm_out, then rebuild and submit directly to pending without another precheck.",
                       "Pending performs final validation. Remaining failures stay awaiting with errors for human review; do not repair them again.",
                       "No routine user confirmation. Preserve unknown fields rather than guessing; only unrecoverable source/transport errors ABORT."
                   ]}, file, ensure_ascii=False, indent=1)


def _precheck_issues(jobs, validated):
    issues = []
    for kind, items, _ in jobs:
        result = db_client.validate(kind, [x["record"] for x in items])
        if not result.get("ok"):
            return None
        checks = result.get("results") or []
        if len(checks) != len(items):
            raise RuntimeError("预检返回条数不匹配：%s %d/%d" % (kind, len(checks), len(items)))
        for item, check in zip(items, checks):
            rec = item["record"]
            if check.get("ok"):
                validated.add(_record_sig(kind, rec))
            else:
                issues.append({"reason": "validation", "kind": kind,
                    "message_id": str(rec.get("消息ID") or ""), "record": rec,
                    "errors": check.get("errors") or [], "resolved": check.get("resolved") or {}})
    return issues


def advance_precheck(d, st):
    if st.get("agent_repair_completed"):
        return advance_submit(d, st)
    validated = set()
    issues = _precheck_issues(_build_jobs(st), validated)
    if issues is None:
        st["stage"] = "PRECHECK"
        save_state(d, st)
        return _submit_stall(d, st, set(st.get("submit_done") or []))
    st["precheck_validated"] = sorted(validated)
    st["precheck_issues"] = len(issues)
    st["repair_issues"] = issues
    if issues:
        # evidence-only rows are deliberately incomplete and must be visible in
        # pending immediately. They are not a reason to wait for another Agent
        # repair cycle; ordinary structured failures retain the one repair pass.
        force_sigs = {_record_sig(kind, item["record"])
                      for kind, items, _ in _build_jobs(st)
                      for item in items if item.get("force_pending")}
        repairable = [issue for issue in issues
                      if _record_sig(issue["kind"], issue["record"]) not in force_sigs]
        if repairable:
            _repair_dossier(d, st, repairable)
            st["stage"] = AGENT_REPAIR_STAGE
        else:
            st["agent_repair_completed"] = True
            # These rows are intentionally incomplete evidence records. Their
            # validation errors belong to the pending reviewer, not a second
            # Agent-repair loop after they have been submitted.
            st["repair_issues"] = []
            st["repair_submission"] = sorted(
                _record_sig(kind, item["record"])
                for kind, items, _ in _build_jobs(st) for item in items)
            st["stage"] = "SUBMIT"
    else:
        if os.path.exists(_review_path(d)):
            os.remove(_review_path(d))
        st["stage"] = "SUBMIT"
    save_state(d, st)
    return advance_submit(d, st)


def approve_review(d, message_ids):
    # Compatibility only: old callers cannot bypass failed validation by confirming IDs.
    st = load_state(d)
    st.pop("review_approved_ids", None)
    st["stage"] = "PRECHECK"
    save_state(d, st)
    emit(["用户确认旁路已取消；首次预检失败由Agent修正一轮后直接送审。", "# " + runpy() + " advance"])


def advance_submit(d, st):
    if publisher_policy_changed(st):
        return run_parse(d, st)
    all_jobs = _build_jobs(st)
    # Migrate already-staged failures from the previous automatic repair loop.
    legacy_failures = st.pop("postcheck_issues", [])
    if legacy_failures:
        st["human_review_issues"] = list(st.get("human_review_issues") or []) + legacy_failures
        st["repair_issues"] = [issue for issue in st.get("repair_issues") or []
                              if issue.get("reason") != "pending_postcheck"]
    repaired = st.get("agent_repair_completed", False)
    eligible = set((st.get("repair_submission") if repaired else st.get("precheck_validated")) or [])
    if not repaired and "precheck_validated" not in st:
        # Upgrade a persisted legacy SUBMIT state without allowing unchecked records through.
        return advance_precheck(d, st)
    repair = st.get("repair_issues") or []
    known_failures = {_record_sig(issue["kind"], issue["record"]) for issue in repair}
    if any(_record_sig(kind, item["record"]) not in eligible | known_failures
           for kind, items, _ in all_jobs for item in items):
        return step_build(d, st) if repaired else advance_precheck(d, st)
    jobs = []
    for kind, items, _ in all_jobs:
        safe_items = [x for x in items if _record_sig(kind, x["record"]) in eligible]
        if safe_items:
            jobs.append((kind, safe_items, _chunk_sig(kind, [x["record"] for x in safe_items])))
    done = set(st.get("submit_done") or [])
    total = len(jobs)
    if total == 0 and repair:
        return _emit_agent_repair(d, st)
    if total == 0:
        if st.get("submit_done_records"):
            st["stage"] = "DONE"
            save_state(d, st)
            commit_watermark(d, st)
            return done_summary(d, st, st.get("records_submitted", 0))
        if st.get("new_ids"):
            # Noise-only pulls have no candidate rows, but their decision files
            # still resolve the messages and must allow the watermark to move.
            ldir = os.path.join(WORK, "llm_out_%s%s" % (d, P.sfx()))
            resolved_noise = set()
            for _p in (sorted(glob.glob(os.path.join(ldir, "*.json")))
                       if os.path.isdir(ldir) else []):
                _out = _load_json(_p)
                for _item in (_out if isinstance(_out, list) else [_out]):
                    if (_item.get("msg_id") in st["new_ids"]
                            and _item.get("decision") == "noise"):
                        resolved_noise.add(_item["msg_id"])
            non_ledger = set(st.get("non_ledger_message_ids") or [])
            if resolved_noise | non_ledger | verified_publisher_exclusions(d, st) == set(st["new_ids"]):
                st["stage"] = "DONE"
                save_state(d, st)
                commit_watermark(d, st)
                return done_summary(d, st, 0)
            st["stage"] = "LLM_PENDING"
            save_state(d, st)
            emit(["ABORT: 本轮有消息但没有候选记录，水位未推进；请补齐判定文件后重跑 advance。"])
            return
        st["stage"] = "DONE"
        save_state(d, st)
        commit_watermark(d, st)
        return done_summary(d, st, 0)
    pending = [j for j in jobs if j[2] not in done]
    if not pending:
        st["stage"] = "DONE"
        save_state(d, st)
        commit_watermark(d, st)
        return done_summary(d, st, st.get("records_submitted", 0))
    ok, fail, warn = int(st.get("records_submitted") or 0), 0, int(st.get("records_check_failed") or 0)
    for kind, items, sig in pending:
        if publisher_policy_changed(st):
            return run_parse(d, st)
        part = [x["record"] for x in items]
        res = db_client.submit(kind, part)               # 唯一写入口：/api/pending 进待审核
        if res.get("ok"):
            ok += res.get("staged", len(part))
            failures = res.get("validation_failures") or []
            warn += int(res.get("check_failed", len(failures)))
            if failures:
                st["human_review_issues"] = list(st.get("human_review_issues") or []) + failures
            # The server has already staged these rows. Failed final validation
            # belongs to human review, never another Agent repair or resubmit.
            coverage = res.get('coverage')
            if coverage and not coverage.get('ok'):
                st['coverage_issues'] = list(st.get('coverage_issues') or []) + coverage.get('missing', [])
                affected = {r.get('来源群') for r in coverage.get('missing', [])}
                st['coverage_blocked_groups'] = sorted(set(st.get('coverage_blocked_groups') or []) |
                                                       {slug for slug,name in GNAME.items() if name in affected})
                st['review_blocked_groups'] = sorted(set(st.get('review_blocked_groups') or []) |
                                                     {slug for slug,name in GNAME.items() if name in affected})
            _write_submit_receipt(d, kind, items, sig, res.get("staged", len(part)), coverage)
            done.add(sig)
            submitted_records = set(st.get("submit_done_records") or [])
            submitted_records.update(_record_sig(kind, item["record"]) for item in items)
            st["submit_done_records"] = sorted(submitted_records)
            st["submit_done"] = sorted(done)
            st["records_submitted"] = ok
            st["records_check_failed"] = warn
            save_state(d, st)
        else:
            fail += 1
            print("# 提交失败 %s：%s" % (kind, res.get("error")), file=sys.stderr)
    if repair:
        if fail:
            return _submit_stall(d, st, done)
        return _emit_agent_repair(d, st)
    if len(done) >= total and fail == 0:
        st["stage"] = "DONE"
        save_state(d, st)
        commit_watermark(d, st)
        return done_summary(d, st, ok)
    return _submit_stall(d, st, done)


def _emit_agent_repair(d, st):
    if st.get("postcheck_issues") and not any(issue.get("reason") != "pending_postcheck"
                                            for issue in st.get("repair_issues") or []):
        st["stage"] = "SUBMIT"
        return advance_submit(d, st)
    st["stage"] = AGENT_REPAIR_STAGE
    save_state(d, st)
    emit(["AGENT_REPAIR: %d 条需Agent修正一轮；已通过记录可独立暂存。" % st.get("precheck_issues", 0),
          "读取 %s 及其中主数据快照、群规范、原文/原图，修复源OCR或llm_out；未知港搜索官方依据后调用ensure_master。" % _review_path(d),
          "只修正一轮，无依据字段保留未知。完成后 %s advance 重建，再 advance 直接提交pending；最终校验未过留在待审核交人工，不继续Agent修复。" % runpy()])


def _submit_stall(d, st, done):
    last = int(st.get("_last_submitted") or 0)
    n = len(done)
    st["submit_stall"] = 0 if n > last else int(st.get("submit_stall") or 0) + 1
    st["_last_submitted"] = max(last, n)
    save_state(d, st)
    if int(st["submit_stall"]) > SUBMIT_STALL_LIMIT:
        emit(["ABORT: 提交连续 %d 轮零进展（已暂存 %d 批，状态保留可续）。"
              "请检查台账服务是否在运行、token_manual.txt 是否有效，然后 advance 继续。"
              % (st["submit_stall"], n)])
        sys.exit(2)
    emit(["# 提交进度：%d 批已暂存待审核；仍有未完成批次（多为台账服务未起/校验拒绝），修好后：" % n,
          "# " + runpy() + " advance"])


def done_summary(d, st, ok):
    # All pulled messages have now been represented in pending, including
    # evidence-only rows. Review issues remain visible, but they no longer
    # leave the scheduler in COVERAGE_PENDING or hold the source watermark.
    st['stage'] = 'DONE'
    save_state(d, st)
    if 'actual_waiting_records' in st:
        ok = st['actual_waiting_records']
    emit(["DONE: 本轮已将 %d 条记录暂存待审核（含 evidence-only 待补解析行；进业务表需台账「待审核」页人工放行），其中机械校验 failed %s 条，水位已推。摘要：%s"
              % (ok, st.get("records_check_failed", 0), json.dumps(
              {"pulled": st.get("pulled_total"), "staged": ok,
               "check_failed": st.get("records_check_failed", 0),
               "evidence_only": len(st.get("evidence_only_message_ids") or []),
               "semantic_issues": len(st.get("semantic_issues") or []),
               "coverage_issues": len(st.get("coverage_issues") or []),
               "pull_receipt_file": st.get("pull_receipt_file", "")}, ensure_ascii=False))])


def reconcile_pending_coverage(d, st):
    """Verify the latest manifest against live rows before resolving old receipts."""
    sys.path.append(os.path.join(P.PROJECT_ROOT, 'Database'))
    from services.submission_coverage import compare_submission
    expected = st.get('expected_submission')
    if expected is None:
        # Legacy runs need a fresh manifest, without re-submitting completed rows.
        step_build(d, st)
        expected = st.get('expected_submission')
    if expected is None:
        st['stage'] = 'SUBMIT'
        save_state(d, st)
        emit(['SUBMIT: 当前缺少覆盖清单，先由本轮 evidence-only 记录补齐后再结束。'])
        return
    current_ids = set(st.get('new_ids') or [])
    if any(item['record'].get('消息ID') not in current_ids for item in expected):
        raise RuntimeError('Coverage manifest contains sources outside this run')
    actual = []
    for mid in sorted({item['record']['消息ID'] for item in expected}):
        offset = 0
        while True:
            result = db_client._request('GET', '/api/pending?status=all&limit=200&offset=%d&message_id=%s'
                                        % (offset, quote_query(mid, safe='')))
            if not result.get('ok'):
                raise RuntimeError('Coverage read failed: %s' % result.get('error'))
            page = result['rows']
            actual.extend(page)
            offset += len(page)
            if offset >= result['total']:
                break
            if not page:
                raise RuntimeError('Coverage pagination is incomplete')
    proofs = {}
    missing = []
    for kind in ('rates', 'space'):
        records = [item['record'] for item in expected if item['kind'] == kind]
        canonical_expected = []
        for part in db_client.chunks(records):
            response = db_client.validate(kind, part)
            if not response.get('ok') or len(response.get('results', [])) != len(part):
                raise RuntimeError('Coverage normalization failed')
            canonical_expected.extend(dict(record, **(check.get('resolved') or {}))
                                      for record,check in zip(part,response['results']))
        rows = [row for row in actual if row['kind'] == kind]
        for start in range(0, len(rows), BATCH_RECORD_LIMIT):
            part = rows[start:start+BATCH_RECORD_LIMIT]
            response = db_client.validate(kind, [row['payload'] for row in part])
            if not response.get('ok') or len(response.get('results', [])) != len(part):
                raise RuntimeError('Live-row normalization failed')
            for row,check in zip(part,response['results']):
                row['comparison_payload'] = dict(row['payload'], **(check.get('resolved') or {}))
        proofs[kind] = compare_submission(kind, canonical_expected, rows)
        missing.extend(proofs[kind]['missing'])
    proof = {'ok':not missing, 'proofs':proofs, 'actual_rows':len(actual),
             'checked_at':datetime.now().isoformat(timespec='seconds')}
    path = os.path.join(rdir(d), 'coverage_reconciliation.json')
    atomic_json(path, proof)
    st['coverage_reconciliation_file'] = path
    if missing:
        st['coverage_issues'] = missing
        st['stage'] = 'COVERAGE_PENDING'
        save_state(d, st)
        emit(['COVERAGE_PENDING: %d 条实际报价仍缺覆盖；读取 %s，按本轮证据补缺，不重送最终failed或覆盖人工修改。'
              % (len(missing), path)])
        return
    st.setdefault('resolved_coverage_history', []).append({
        'issues':st.get('coverage_issues', []), 'proof_file':path, 'resolved_at':proof['checked_at']})
    previous = set(st.get('coverage_blocked_groups') or [])
    st['review_blocked_groups'] = sorted(set(st.get('review_blocked_groups') or []) - previous)
    st['coverage_blocked_groups'] = []
    st['coverage_issues'] = []
    st['stage'] = 'DONE'
    st['actual_source_rows'] = len(actual)
    st['actual_waiting_records'] = sum(row.get('review_status') == 'awaiting' for row in actual)
    st['records_check_failed'] = sum(row.get('review_status') == 'awaiting' and
                                   row.get('check_status') == 'failed' for row in actual)
    save_state(d, st)
    commit_watermark(d, st)
    done_summary(d, st, st.get('records_submitted', 0))


# ---------------- advance 分发 ----------------

def advance(d, st):
    st = load_state(d)
    if st['stage'] == 'DONE' and (st.get('coverage_issues') or st.get('semantic_issues')):
        done_summary(d, st, st.get('records_submitted', 0))
        return
    if st['stage'] == 'COVERAGE_PENDING':
        return reconcile_pending_coverage(d, st)
    if st["stage"] == "PULL":
        return advance_pull(d, st)
    if st["stage"] == "PARSED":
        return run_parse(d, st)
    if st["stage"] in ("LLM_PENDING", "BUILD"):
        return step_build(d, st)
    if st["stage"] == "PRECHECK":
        return advance_precheck(d, st)
    if st["stage"] == "SUBMIT":
        return advance_submit(d, st)
    if st["stage"] in ("USER_REVIEW", AGENT_REPAIR_STAGE):
        return step_build(d, st)
    emit(["# 当前阶段 %s，无需 advance，直接执行 next" % st["stage"]])


# ---------------- next 总入口 ----------------

def next_step(d):
    st = load_state(d)
    st.pop("_slug", None)
    if st['stage'] == 'DONE' and st.get('pending_watermark'):
        return refresh_incomplete_pull(d, st)
    if st['stage'] == 'COVERAGE_PENDING':
        return refresh_incomplete_pull(d, st)
    if st["stage"] == "PULL":
        return step_pull(d, st)
    if st["stage"] == "LLM_PENDING":
        return run_parse(d, st)
    if st["stage"] == "BUILD":
        return step_build(d, st)
    if st["stage"] == "PRECHECK":
        return advance_precheck(d, st)
    if st["stage"] == "SUBMIT":
        return advance_submit(d, st)
    if st["stage"] in ("USER_REVIEW", AGENT_REPAIR_STAGE):
        return _emit_agent_repair(d, st)
    if st["stage"] == "DONE":
        if st.get("coverage_issues") or st.get("semantic_issues"):
            return done_summary(d, st, st.get("records_submitted", 0))
        st2 = {"date": d, "stage": "PULL", "retries": {}, "submit_done": []}
        save_state(d, st2)
        return step_pull(d, st2)
    emit(["# 阶段 %s：执行 advance 推进" % st["stage"]])


def status(d):
    st = load_state(d)
    emit(json.dumps({k: st.get(k) for k in ("date", "stage", "pulled_total", "records_submitted")},
                    ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "next", "advance", "status", "approve-review"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--message-id", action="append", default=[],
                    help="用户确认的低置信度消息 ID；可重复传入或用逗号分隔")
    ap.add_argument("--shard", default=None,
                    help="分片大组（A/B/C/D）。顶层已在 import paths 前预扫描写入 FRT_SHARD，这里仅登记以免 argparse 报未知参数")
    ap.add_argument("--only", default=None,
                    help="单群手动模式：只跑该 slug 的群，中间目录带 _<slug> 隔离。顶层已预扫描写入 FRT_ONLY")
    a = ap.parse_args()
    d = a.date or today()
    if a.cmd == "start":
        result = start_run(d)
        emit(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["ok"] else 2)
    if a.cmd == "advance" and not os.path.isfile(os.path.join(rdir(d), "state.json")):
        emit(json.dumps({"ok": False, "error": "ABORT: state.json missing; advance refused"}))
        sys.exit(2)
    if a.cmd != "status":
        if os.environ.get("FRT_MONITOR_RUN_ID"):
            try:
                _monitor_wake()
                _monitor_report(source="pipeline",shard=P.SHARD,control_ready=True,activity="流水线已连接实时监控")
            except Exception as exc:
                import logging
                logging.warning("[MONITOR] wake failed (%s); collection continues",type(exc).__name__)
        _monitor_checkpoint()
        _monitor_progress(load_state(d))
    if a.cmd == "status":
        status(d)
    elif a.cmd == "approve-review":
        approve_review(d, ",".join(a.message_id).split(","))
    elif a.cmd == "next":
        next_step(d)
    else:
        advance(d, load_state(d))
