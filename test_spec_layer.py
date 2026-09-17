# -*- coding: utf-8 -*-
"""规范执行层的离线测试：不碰生产库、不连服务、不发台账。

    python test_spec_layer.py

覆盖：spec_index 路由与锚点解析、check_llm_out 的正例与六类问题、
spec_alias_diff 的差集与断链、pipeline 的导入完整性。
夹具建在临时目录，靠 FRT_RUNTIME_DIR 把运行态指过去（paths.py 本来就支持）。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

SKILL = pathlib.Path(r"C:\Users\YQN\Documents\正式版运价库\运价项目\Skill\freight-rate-heartbeat")
SCRIPTS = SKILL / "scripts"
PY = sys.executable
PYX = [PY, "-X", "utf8"]        # 子进程必须强制 UTF-8 输出，否则父进程按 GBK 解码直接崩
work = pathlib.Path(tempfile.mkdtemp(prefix="speclayer-"))
W = work / ".work"          # paths.WORK = RUNTIME_DIR/.work，夹具必须建在这层下面
W.mkdir()
(W / "msgs_0999").mkdir()
(W / "llm_out_0999").mkdir()

(W / "msgs_0999" / "ningbo_space.json").write_text(json.dumps([
    {"id": "m1", "t": "2026-09-17 10:00", "s": "俞文清", "c": "HMM 现舱 加勒比 5X40HQ 9.20 USD2000", "q": None, "img": []},
], ensure_ascii=False), encoding="utf-8")

# 正例：依据引用本群规范缩写表小节
(W / "llm_out_0999" / "a_good.json").write_text(json.dumps({
    "msg_id": "m1", "group": "ningbo_space", "decision": "records", "confidence": "high",
    "rule_basis": "规范.md#缩写表（作用域＝本群 + 该列）",
    "records": [{"_table": "space", "目的港": "加勒比", "舱位类型": "现舱",
                 "船名航次": "HMM ROTTERDAM 1234W", "箱量": 5}]}, ensure_ascii=False), encoding="utf-8")
# 反例集合：假 msg_id / 禁填键 / 非法 decision / 未知 slug
(W / "llm_out_0999" / "b_bad.json").write_text(json.dumps({
    "msg_id": "nope", "group": "not_a_slug", "decision": "maybe",
    "rule_basis": "extraction-rules.md#7",
    "records": [{"_table": "rate_space", "来源群": "宁波舱位滚动更新群", "目的港": "Santos",
                 "舱位类型": "报备", "柜型": "40GP"}]}, ensure_ascii=False), encoding="utf-8")
# 可疑：用了本群缩写 NB 却只引用全局条款、柜型多档只给一个价、锚点号不存在
(W / "llm_out_0999" / "c_susp.json").write_text(json.dumps({
    "msg_id": "m1", "group": "ningbo_space", "decision": "records", "confidence": "med",
    "rule_basis": "extraction-rules.md#999",
    "records": [{"_table": "rate", "起运港": "NB", "目的港": "Santos", "船司": "HMM",
                 "柜型": "20GP/40HQ/HC/40NOR", "海运费USD": "100/200"}]}, ensure_ascii=False), encoding="utf-8")
# 缺 rule_basis → 错误
(W / "llm_out_0999" / "d_nobasis.json").write_text(json.dumps({
    "msg_id": "m1", "group": "ningbo_space", "decision": "noise", "records": []}, ensure_ascii=False),
    encoding="utf-8")

env = dict(os.environ, FRT_RUNTIME_DIR=str(work))
fails = []


def run(name, args, expect=None):
    r = subprocess.run(PYX + args, cwd=str(SCRIPTS), env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    out = (r.stdout + r.stderr)
    ok = True if expect is None else (r.returncode == expect)
    print("  %-4s %s (rc=%d)" % ("OK" if ok else "FAIL", name, r.returncode))
    if not ok or "-v" in sys.argv:
        print("       " + out.strip().replace("\n", "\n       ")[:900])
    fails.append(name) if not ok else None
    return out


def jrun(args):
    r = subprocess.run(PYX + args, cwd=str(SCRIPTS), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(r.stdout or "{}"), r.returncode
    except Exception as e:
        print("   [!] %s 输出不是 JSON：%s" % (args[0], e))
        print("       " + (r.stdout + r.stderr).strip().replace("\n", " | ")[:700])
        return {}, r.returncode


print("=== 1. 模块导入完整性（pipeline 会 import 新层）===")
run("import pipeline + spec_index + check_llm_out", ["-c", "import pipeline, spec_index, check_llm_out"])
run("py_compile 全部脚本", ["-c", "import compileall,sys;sys.exit(0 if compileall.compile_dir(r'%s', quiet=2) else 1)" % str(SCRIPTS)])

print("\n=== 2. spec_index：路由与锚点解析 ===")
sys.path.insert(0, str(SCRIPTS))
import spec_index as SI                      # 本进程直接调，省掉 -c 拼串的引号/编码坑
idx = SI.index(refresh=True)
rep = {"all_config": all(v["route"] == "config" for v in idx.values()),
       "nb_abbrev": len(SI.spec_for("ningbo_space")["abbrev"]),
       "nb_todo": len(SI.spec_for("ningbo_space")["todo"]),
       "ok_group": SI.resolve_basis("ningbo_space", "规范.md#缩写表（作用域＝本群 + 该列）"),
       "bad_head": SI.resolve_basis("ningbo_space", "规范.md#不存在的小节"),
       "ok_rule": SI.resolve_basis("ningbo_space", "extraction-rules.md#19"),
       "bad_rule": SI.resolve_basis("ningbo_space", "extraction-rules.md#999")}
for k in ("all_config", "nb_abbrev", "nb_todo", "ok_group", "bad_head", "ok_rule", "bad_rule"):
    print("   %-10s %s" % (k, rep.get(k)))
for k, want in (("all_config", True), ("nb_abbrev", 6), ("nb_todo", 3),
                ("ok_group", (True, "")), ("bad_head", (False, rep.get("bad_head", ["", ""])[0] if isinstance(rep.get("bad_head"), list) else "")),
                ("ok_rule", (True, "")), ("bad_rule", (False, rep.get("bad_rule", ["", ""])[0] if isinstance(rep.get("bad_rule"), list) else ""))):
    if rep.get(k) is None:
        print("  FAIL 断言 %s → 没取到值（检查上一步是否报错）" % k)
        fails.append("spec_index." + k)
        continue
    got = tuple(rep.get(k)) if isinstance(rep.get(k), list) else rep.get(k)
    if k == "bad_head":
        ok = got[0] is False and "不存在" in str(got[1])
    elif k == "bad_rule":
        ok = got[0] is False and "999" in str(got[1])
    else:
        ok = got == want
    print("  %-4s 断言 %s → %s" % ("OK" if ok else "FAIL", k, got))
    if not ok:
        fails.append("spec_index." + k)

print("\n=== 3. check_llm_out：正例干净、反例全被抓 ===")
rep, rc = jrun(["check_llm_out.py", "--date", "0999", "--json"])
print("   文件 %d · 记录 %d · 错误 %d · 可疑 %d (rc=%d)" % (rep["n_obj"], rep["n_rec"], rep["n_err"], rep["n_warn"], rc))
for e in rep["errors"]:
    print("   ✗ " + e[:100])
for w in rep["warnings"]:
    print("   ⚠ " + w[:100])
want_err = ("不在落盘消息", "不在 chat_sources", "decision", "禁填键", "缺 rule_basis", "非法（rate|space）")
for pat in want_err:
    hit = any(pat in e for e in rep["errors"])
    print("  %-4s 错误应含「%s」" % ("OK" if hit else "FAIL", pat))
    fails.append("err:" + pat) if not hit else None
want_warn = ("rule_basis 锚点存疑", "柜型", "舱位类型", "缩写")
for pat in want_warn:
    hit = any(pat in w for w in rep["warnings"])
    print("  %-4s 可疑应含「%s」" % ("OK" if hit else "FAIL", pat))
    fails.append("warn:" + pat) if not hit else None
if rc != 1:
    print("  FAIL 有错误时退出码应为 1（实为 %d）" % rc)
    fails.append("rc")
else:
    print("  OK   有错误时退出码 1")
if any("a_good" in x for x in rep["errors"] + rep["warnings"]):
    print("  FAIL 正例被判有问题（误报）")
    fails.append("误报正例")
else:
    print("  OK   正例零误报（引用本群规范小节 + 合法十一档）")

print("\n=== 4. 干净夹具应 0 错误：删掉反例文件再跑 ===")
for f in ("b_bad.json", "c_susp.json", "d_nobasis.json"):
    (W / "llm_out_0999" / f).unlink()
rep2, rc2 = jrun(["check_llm_out.py", "--date", "0999", "--json"])
print("   错误 %d · 可疑 %d (rc=%d)" % (rep2["n_err"], rep2["n_warn"], rc2))
ok = rep2["n_err"] == 0 and rc2 == 0
print("  %-4s 全干净时退出码 0" % ("OK" if ok else "FAIL"))
fails.append("clean") if not ok else None

print("\n=== 5. spec_alias_diff 对生产库（只读）===")
rep3, rc3 = jrun(["spec_alias_diff.py", "--json", "--strict"])
print("   规范行 %d · 库里 %d · 待登记 %d · 库里未写 %d · 冲突 %d · 断链 %d (rc=%d)"
      % (rep3["n_spec_rows"], rep3["n_alias_rows"], len(rep3["only_in_spec"]), len(rep3["only_in_db"]),
         len(rep3["target_mismatch"]), len(rep3["broken_links"]), rc3))
for x in rep3["only_in_spec"]:
    print("   待登记：%-20s %-9s %-10s %s" % (x["group"][:20], x["kind"], x["alias"][:10], x["target"][:30]))
for x in rep3["broken_links"]:
    print("   断链：" + x)
ok = rc3 == 0 and rep3["n_spec_rows"] > 40
print("  %-4s 无断链且解析到规范行" % ("OK" if ok else "FAIL"))
fails.append("alias_diff") if not ok else None

print("\n结果：", "全部通过" if not fails else "失败 %d 项 -> %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
