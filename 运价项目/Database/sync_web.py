# -*- coding: utf-8 -*-
"""把云端版前端同步到局域网版（保持 1:1），只重新打上本地那一处差异：本机回环免口令。

    python sync_web.py            # 覆盖 local_manual_board/web/index.html
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "cloud_board_blank" / "web" / "index.html"
DST = Path(__file__).resolve().parent / "web" / "index.html"

# 云端：一律要口令；本地：先问服务端要不要令牌（本机回环直接给）
OLD = ("async function ensureToken() {\n"
       "  if (S.token) return S.token;\n"
       "  let saved = '';\n")
NEW = ("async function ensureToken() {\n"
       "  if (S.token) return S.token;\n"
       "  try {\n"
       "    const d = await (await fetch('/api/local-token')).json();\n"
       "    if (d && d.ok && d.token) { S.token = d.token; return d.token; }\n"
       "  } catch (e) { /* 服务没起或没这个路由，走口令流程 */ }\n"
       "  let saved = '';\n")
OLD_C = ("/* 写入口令：云上不分本机/外机，首次点「录入」时输一次（口令本身就是 x-push-token），\n"
         "   之后缓存在浏览器里；填错被服务端拒了会清掉重填 */")
NEW_C = ("/* 写入口令：本机回环由服务端直接发令牌（这台电脑免口令）；同网段其他机器仍要口令，\n"
         "   输一次后缓存在浏览器里，填错被服务端拒了会清掉重填 */")

s = SRC.read_text(encoding="utf-8")
if "--force" not in sys.argv:
    raise SystemExit("局域网版已是唯一演进方向，本地前端不再被云端覆盖。"
                     "确要从云端重灌请加 --force（本地独有改动会被冲掉）")
for old, new, name in ((OLD, NEW, "ensureToken 本机令牌优先"), (OLD_C, NEW_C, "注释")):
    if s.count(old) != 1:
        raise SystemExit("锚点不对（%s 命中 %d 次），云端前端改过了，先看清再同步" % (name, s.count(old)))
    s = s.replace(old, new)

# 写盘前先真解析一遍：上次就是注释只换了一半，留下孤儿 */ 让整段脚本失效
try:
    import esprima
    body = s[s.index("<script>") + len("<script>"):s.rindex("</script>")]
    esprima.parseScript(body.replace("?.", ".").replace("??", "||"))
    print("语法校验：通过")
except ImportError:
    print("语法校验：跳过（没装 esprima，先 python -m pip install --user esprima）")
except Exception as e:
    raise SystemExit("语法校验失败，拒绝写入：%s" % e)

DST.write_text(s, encoding="utf-8")
print("已同步 →", DST)
print("字节:", len(s), "| 批量导入:", "btParse" in s, "| 候选列表:", s.count("data-ac="), "处")
