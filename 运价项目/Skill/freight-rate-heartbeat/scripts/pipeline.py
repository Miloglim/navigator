# -*- coding: utf-8 -*-
"""pipeline.py — 运价心跳无人值守状态机（生产主控，代码层）。

设计：dws 命令只能由 Agent 的 Bash 工具直调（宿主执行），本脚本负责一切决策。
写入对象已迁到「运价台账」(freight_manual.db / manual_server.py)：本机 HTTP 可由 python urllib
直连（db_client），不再像旧版那样由 Agent 逐条 dws create + 断点续传，也不再镜像旧本机看板。

目录协议（.work/ 下，<D>=MMDD）：
  run_<D>/state.json          状态机
  run_<D>/raw/<name>.json     Agent 存 dws 结果
  msgs_<D>/<slug>.json        逐字消息落盘（唯一原文源）
  draft_<D>/                  record_builder.py 输出（record = 中文字段候选记录）
  llm_out_<D>/<safe_id>.json  Agent 结构化判定（契约见 scripts/llm_output_contract.md）
  ocr_cache/<mediaId>.json    识图缓存（跨群复用）

阶段：PULL → PARSED → LLM_PENDING → BUILD → SUBMIT → DONE
子命令：
  python scripts/pipeline.py status
  python scripts/pipeline.py next      # 打印当前步该做的动作（dws 命令 + 存盘路径 / 提交进度）
  python scripts/pipeline.py advance   # 解析已存 raw / 推进提交（可循环调用直到 DONE）
"""
import json, os, re, sys, glob, hashlib, subprocess, argparse
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
import paths as P
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = P.WORK
PY = sys.executable
sys.path.insert(0, HERE)
# 有效期解析口径单一事实源：record_builder.vdates = norm_v + validity_parser.parse。
from record_builder import vdates, compose_text
import ledger_client as db_client  # 台账对接层：唯一写入口 /api/pending，预检 /api/validate，主数据 /api/master
import spec_index                  # 「群聊规范」机器入口：slug→规范路径、缩写表、待补条目、可引用锚点
import check_llm_out as llm_check   # 判定文件只读自查：越权键 + 规范执行痕迹（只报告，不抢台账闸门）

SPEC = P.CONTRACT
GROUPS = P.chat_groups()
GNAME = {g["slug"]: g["name"] for g in GROUPS}
CTYPE = set(SPEC.get("ctype", []))
STYPE = set(SPEC.get("space_type_options", []))
# 提交阶段：连续多少个 advance 零进展即判定死锁（服务没起/token 失效），交人工
SUBMIT_STALL_LIMIT = 6
JQ_PULL = ('.messages[] | {t:.createTime, s:.sender, id:.openMessageId, '
           'img:[.resourceRefs[]?|.resourceId], c:.content, '
           'q:{t:.quotedMessage.createTime, s:.quotedMessage.sender, '
           'id:.quotedMessage.openMessageId, c:.quotedMessage.content}}')


def runpy():
    return '"%s" "%s"' % (PY.replace("\\", "/"), os.path.join(HERE, "pipeline.py").replace("\\", "/"))


def today():
    return datetime.now().strftime("%m%d")


def rdir(d):
    p = os.path.join(WORK, "run_%s" % d)
    os.makedirs(os.path.join(p, "raw"), exist_ok=True)
    return p


def load_state(d):
    p = os.path.join(rdir(d), "state.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {"date": d, "stage": "PULL", "retries": {}, "submit_done": []}


def save_state(d, st):
    with open(os.path.join(rdir(d), "state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def safe(name):
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name)[:60]


def parse_jsonish(text):
    text = (text or "").strip()
    if not text or text == "(empty output)":
        return [], None
    try:
        v = json.loads(text)
        return (v if isinstance(v, list) else [v]), None
    except Exception:
        pass
    dec, out, idx = json.JSONDecoder(), [], 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, idx = dec.raw_decode(text, idx)
            out.append(obj)
        except Exception as e:
            return out, "parse fail at %d: %s" % (idx, e)
    return out, None


def read_raw(d, name):
    p = os.path.join(rdir(d), "raw", name + ".json")
    if not os.path.exists(p):
        return None
    txt = open(p, encoding="utf-8").read().strip()
    try:  # 兼容 hook 包装
        w = json.loads(txt)
        if isinstance(w, dict) and w.get("type") == "dws_tool_result":
            txt = str(w.get("content", ""))
    except Exception:
        pass
    return txt


def emit(lines):
    if isinstance(lines, str):
        lines = lines.splitlines()
    print("\n".join(str(x) for x in lines))


def commit_watermark(d, st):
    """只在「台账提交已确认成功」后把暂存水位落库。拉取成功≠提交成功，防写通道故障静默丢消息。"""
    wm = st.get("pending_watermark") or {}
    if not wm:
        return
    subprocess.run([PY, os.path.join(HERE, "watermark.py"), "set", "--map",
                    json.dumps(wm, ensure_ascii=False)], check=True)
    st["pending_watermark"] = {}
    save_state(d, st)
    emit(["# 水位已推 %s（本轮提交台账确认后）" % wm])


# ---------------- PULL ----------------

def step_pull(d, st):
    wm = json.loads(subprocess.run([PY, os.path.join(HERE, "watermark.py"), "get"],
                                   capture_output=True, text=True, encoding="utf-8").stdout)
    lines = ["# 阶段 PULL：执行下列 dws 命令（每条一次 Bash 调用，不拼接不重定向；"
             "允许在同一条消息里并行发起多条以省往返），"
             "把每条的完整返回内容存到指定 raw 文件（Write 工具），全部存好后执行：",
             "# " + runpy() + " advance"]
    st.setdefault("pull_targets", {})
    for g in wm.values() if isinstance(wm, dict) else wm:
        slug = [k for k, v in wm.items() if v["cid"] == g["cid"]][0] if isinstance(wm, dict) else g["slug"]
        st["pull_targets"][slug] = g["since"]
        lines.append("\n# 存 raw/pull_%s.json" % slug)
        lines.append('dws chat message list --group "%s" --time "%s" --direction newer '
                     '--format json --jq "%s"' % (g["cid"], g["since"], JQ_PULL))
    save_state(d, st)
    emit(lines)


def advance_pull(d, st):
    targets = st.get("pull_targets", {})
    missing, errors, new_msgs = [], [], {}
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
        new_msgs[slug] = msgs
    save_state(d, st)
    if missing:
        emit(["# 还有 raw 未存盘：%s —— 存好后执行：" % missing, "# " + runpy() + " advance"])
        return
    if errors:
        emit(["# 这些群需重拉（删掉对应 raw 后 next 重取单群任务）：",
              "# " + runpy() + " next", "# 完成后执行：", "# " + runpy() + " advance",
              "# 涉及群：%s" % errors])
        return
    mdir = os.path.join(WORK, "msgs_%s" % d)
    os.makedirs(mdir, exist_ok=True)
    wm_map, total, added = {}, 0, 0
    for slug, msgs in new_msgs.items():
        if not msgs:
            continue
        p = os.path.join(mdir, slug + ".json")
        existing = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
        have = {m.get("id") for m in existing}
        newn = 0
        for m in msgs:
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
        wm_map[slug] = max(m["t"] for m in msgs)
    st["pending_watermark"] = wm_map
    st["stage"] = "PARSED"
    st["pulled_total"] = added
    save_state(d, st)
    emit(["# PULL 完成：拉取 %d 条（新增 %d 条），水位待提交成功后推 %s"
          % (total, added, wm_map or "（无新消息）")])
    if added == 0:
        st["stage"] = "DONE"
        save_state(d, st)
        commit_watermark(d, st)
        emit(["DONE: 本轮无新增消息，静默结束，不提交台账。"])
        return
    run_parse(d, st)


def run_parse(d, st):
    subprocess.run([PY, os.path.join(HERE, "record_builder.py"), "--date", d], check=True)
    dr = os.path.join(WORK, "draft_%s" % d)
    needs = json.load(open(os.path.join(dr, "needs_llm.json"), encoding="utf-8"))
    img_dir = db_client.images_dir()
    mdir = os.path.join(WORK, "msgs_%s" % d)
    queue = list(needs)
    for fn in sorted(os.listdir(mdir)):
        for m in json.load(open(os.path.join(mdir, fn), encoding="utf-8")):
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
    tips = ["# 阶段 LLM：%d 项待判定，清单见 %s" % (len(queue), qf),
            "# Agent 职责（唯一需要智能的一步）：",
            "# 1) 读 scripts/llm_output_contract.md 契约 + 每条队列项 spec_path 指向的该群规范"
            "（spec_todo 列出的条目＝尚无实测依据，禁止猜填，按全局口径留空交人工）",
            "# 2) 新图：dws 下载原图 → Read 识图 → 写 .work/ocr_cache/<mediaId>.json；"
            "并把下载到的原图放进台账 %s（文件名以 mediaId 开头，看板按 mediaId 归位取图）→ 重跑 next" % img_dir,
            "# 3) 每条判定写 .work/llm_out_%s/<safe(msg_id)>.json（严格按契约），"
            "rule_basis 必须落在真实锚点：规范.md#小节 或 extraction-rules.md#条款号" % d,
            "# 4) 全部写好后先自查：python scripts/check_llm_out.py --date %s"
            "（错误清零再推进；⚠ 可疑项交人工看，别自己放行）" % d,
            "# 5) " + runpy() + " advance"]
    if no_spec:
        tips.insert(2, "# ⚠ 这些群没定位到规范文件（先补 references/群聊规范/<群>/规范.md 并在 chat_sources 填 spec_dir）：%s"
                    % ", ".join(sorted(set(no_spec))))
    emit(tips)


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
    dr = os.path.join(WORK, "draft_%s" % d)
    return json.load(open(os.path.join(dr, "draft_%s.json" % name), encoding="utf-8"))


def step_build(d, st):
    new_ids = set(st.get("new_ids") or [])
    recs = []
    for tbl in ("rate", "space"):
        for x in _load_draft(d, tbl):
            if x["_meta"]["msg_id"] in new_ids:
                recs.append(x)
    msgs_by_id = {}
    mdir = os.path.join(WORK, "msgs_%s" % d)
    for fn in os.listdir(mdir):
        for m in json.load(open(os.path.join(mdir, fn), encoding="utf-8")):
            msgs_by_id[m["id"]] = m
    # 合并 llm_out（LLM 判定也是中文键记录，_table 决定进哪库）
    # 2026-09-17 审查修复：加 new_ids 门控（与 draft 分支一致）。llm_out_<D>/ 按天累积不清，
    # cron 同日多轮时每轮 glob 到全天判定文件，无门控会把早轮记录全部重建重投 → 重复入库
    ldir = os.path.join(WORK, "llm_out_%s" % d)
    # 规范执行自查（只报告不拦）：台账闸门负责值域，这里负责"越权键 + 依据是否真锚点 + 是否疑似没读该群规范"
    try:
        _rep = llm_check.check(d, msgs_by_id=msgs_by_id)
        if _rep["n_err"] or _rep["n_warn"]:
            emit(llm_check.summary_lines(_rep))
    except Exception as _e:
        emit(["# ⚠ 判定自查没跑成（不影响提交）：%s" % _e])
    for _p in (sorted(glob.glob(os.path.join(ldir, "*.json"))) if os.path.isdir(ldir) else []):
        _d = json.load(open(_p, encoding="utf-8"))
        for o in (_d if isinstance(_d, list) else [_d]):
            if o.get("msg_id") not in new_ids:
                continue    # 非本轮新消息：早轮已提交过，跳过
            src = msgs_by_id.get(o.get("msg_id"))
            if not src:
                emit(["ABORT: llm_out msg_id %s 不在落盘消息中，拒绝（溯源铁律）" % o.get("msg_id")])
                sys.exit(2)
            for r in o.get("records", []):
                tbl = r.pop("_table", "rate")
                rec = {k: v for k, v in r.items() if v not in (None, "")}
                rec["来源群"] = GNAME.get(o.get("group"), src.get("_slug", o.get("group")))
                rec["发送人"] = src["s"]
                rec["消息时间"] = src["t"][:16]
                rec["消息原文"] = compose_text(src)
                rec["消息ID"] = src["id"]
                vtxt = r.get("有效期船期") or ""
                vf, vt, ev, ai = vdates(vtxt, src["t"][:10])
                for _k, _v in (("有效期起", vf), ("有效期止", vt), ("船期ETD", ev)):
                    if _v:
                        rec[_k] = _v
                if ai:
                    rec["AI判断"] = "是"   # 运价/舱位都必填有效期；兜底窗打 AI判断 标签
                recs.append({"record": rec, "_meta": {"table": tbl, "group": o.get("group"),
                                                       "msg_id": o["msg_id"], "conf": o.get("confidence", "llm")}})
    # 图片引用：源消息带图的，图片名=mediaId 列表（台账看板按 mediaId 归位取 Database/images 里的图）
    for r in recs:
        src = msgs_by_id.get(r.get("_meta", {}).get("msg_id"))
        if src and src.get("img"):
            r["record"]["图片名"] = ",".join(src["img"])
    # 规整（2026-09-17 审查修复：删除 _enum_ok 本地静默丢弃。原注释"台账服务端不强校验"已过时——
    # 服务端闸门现强校验柜型三档 + 舱位类型 11 档，非法值照提交会被判 check_status='failed'
    # 进人工复核队列；本地丢弃 + 水位照推 = 该消息永久丢失、无人知道）
    clean = []
    for r in recs:
        rec = _coerce(r["record"])
        clean.append({"kind": "rates" if r["_meta"]["table"] == "rate" else "space", "record": rec})
    st["submit_queue"] = clean
    st["submit_done"] = []
    st["stage"] = "SUBMIT"
    save_state(d, st)
    n_rate = sum(1 for x in clean if x["kind"] == "rates")
    n_space = sum(1 for x in clean if x["kind"] == "space")
    emit(["# 阶段 SUBMIT：候选记录已就绪（运价 %d / 舱位 %d），执行提交：" % (n_rate, n_space),
          "# " + runpy() + " advance"])


# ---------------- SUBMIT（同步 POST /api/validate 预检 + /api/pending，成功才推水位） ----------------

def _chunk_sig(kind, part):
    basis = json.dumps([kind, part], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def _build_jobs(st):
    q = st.get("submit_queue") or []
    jobs = []
    for kind in ("rates", "space"):
        recs = [x["record"] for x in q if x["kind"] == kind]
        for part in db_client.chunks(recs):
            jobs.append((kind, part, _chunk_sig(kind, part)))
    return jobs


def advance_submit(d, st):
    jobs = _build_jobs(st)
    done = set(st.get("submit_done") or [])
    total = len(jobs)
    if total == 0:
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
    for kind, part, sig in pending:
        v = db_client.validate(kind, part)               # 预检（不写库），只为失败告警
        if not v.get("ok") and v.get("unreachable"):
            return _submit_stall(d, st, done)           # 服务没起/token 失效 → 不推水位，下轮重跑
        if v.get("ok"):
            warn += sum(1 for rr in (v.get("results") or []) if not rr.get("ok"))
        res = db_client.submit(kind, part)               # 唯一写入口：/api/pending 进待审核
        if res.get("ok"):
            ok += res.get("staged", len(part))
            done.add(sig)
            st["submit_done"] = sorted(done)
            st["records_submitted"] = ok
            st["records_check_failed"] = warn
            save_state(d, st)
        else:
            fail += 1
            print("# 提交失败 %s：%s" % (kind, res.get("error")), file=sys.stderr)
    if len(done) >= total and fail == 0:
        st["stage"] = "DONE"
        save_state(d, st)
        commit_watermark(d, st)
        return done_summary(d, st, ok)
    return _submit_stall(d, st, done)


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
    emit(["DONE: 本轮暂存待审核 %d 条（进业务表需台账「待审核」页人工放行），其中机械校验 failed %s 条，水位已推。摘要：%s"
          % (ok, st.get("records_check_failed", 0), json.dumps(
              {"pulled": st.get("pulled_total"), "staged": ok,
               "check_failed": st.get("records_check_failed", 0)}, ensure_ascii=False))])


# ---------------- advance 分发 ----------------

def advance(d, st):
    st = load_state(d)
    if st["stage"] == "PULL":
        return advance_pull(d, st)
    if st["stage"] == "PARSED":
        return run_parse(d, st)
    if st["stage"] in ("LLM_PENDING", "BUILD"):
        return step_build(d, st)
    if st["stage"] == "SUBMIT":
        return advance_submit(d, st)
    emit(["# 当前阶段 %s，无需 advance，直接执行 next" % st["stage"]])


# ---------------- next 总入口 ----------------

def next_step(d):
    st = load_state(d)
    st.pop("_slug", None)
    if st["stage"] == "PULL":
        return step_pull(d, st)
    if st["stage"] == "LLM_PENDING":
        return run_parse(d, st)
    if st["stage"] == "BUILD":
        return step_build(d, st)
    if st["stage"] == "SUBMIT":
        return advance_submit(d, st)
    if st["stage"] == "DONE":
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
    ap.add_argument("cmd", choices=["next", "advance", "status"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    d = a.date or today()
    if a.cmd == "status":
        status(d)
    elif a.cmd == "next":
        next_step(d)
    else:
        advance(d, load_state(d))
