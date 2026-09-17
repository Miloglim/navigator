# -*- coding: utf-8 -*-
"""db_client.py — 运价心跳对「运价台账」(freight_manual.db / manual_server.py) 的唯一对接层。

对齐 docs/agent-input-contract.md：
  · 写：只 POST /api/pending（进待审核，人工放行才落业务表）。绝不碰 /api/push、/api/record/*、/api/master/save。
  · 预校验：POST /api/validate（公开只读、不写库），传前跑一次，拿每条 {ok, resolved, errors}。
  · 主数据：GET /api/master?full=1，港口/船司/航线/港群合法值以它为准（skill 不自拼第二套归一）。

manual_server 是本机 HTTP（默认 127.0.0.1:8795），python urllib 可直连，无需像 dws 那样由宿主逐条跑。
连接参数读 heartbeat/spec.json 的 db_conn；可被环境变量 FRT_DB_URL / FRT_DB_TOKEN 覆盖。
"""
import json
import os
import urllib.error
import urllib.request
import paths as P

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = P.CONTRACT
_CONN = _spec.get("db_conn", {})
BATCH_LIMIT = int(_spec.get("batch_limit", 1000))


def _base():
    return (os.environ.get("FRT_DB_URL") or _CONN.get("server_url") or "http://127.0.0.1:8795").rstrip("/")


def _token():
    t = os.environ.get("FRT_DB_TOKEN")
    if t:
        return t.strip()
    p = P.resolve(_CONN.get("token_file"))
    if p and os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    return ""


def origin():
    return _CONN.get("origin") or "agent"


def images_dir():
    return P.resolve(_CONN.get("images_dir")) or ""


def _request(method, path, body=None, token=True, timeout=120):
    url = _base() + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("x-push-token", _token())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 - 非 JSON 错误体也归一成结构化
            return {"ok": False, "error": raw or ("HTTP %d" % e.code), "code": e.code}
    except Exception as e:  # noqa: BLE001 - 连不上服务等情况
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "unreachable": True}


def master(full=True):
    """港口/船司/航线/港群主数据。归一以它为准，skill 不再自拼映射。"""
    return _request("GET", "/api/master" + ("?full=1" if full else ""), token=False)


def validate(kind, records):
    """服务端权威预校验（不写库）。返回 {ok, results:[{ok,resolved,errors}, ...]} 或结构化错误。"""
    return _request("POST", "/api/validate", {"table": kind, "records": records}, token=False)


def submit(kind, records):
    """唯一写入口：POST /api/pending，进待审核。返回 {ok, staged, ids}。"""
    return _request("POST", "/api/pending",
                    {"table": kind, "origin": origin(), "records": records}, token=True)


def chunks(records):
    """按台账单次上限（1000 条）切片，供上层逐批 submit。"""
    n = max(1, min(BATCH_LIMIT, BATCH_LIMIT))
    for i in range(0, len(records), n):
        yield records[i:i + n]


if __name__ == "__main__":  # 冒烟：python db_client.py 打一发主数据 + 一条预校验（不写库）
    m = master()
    print("master ok=%s ports=%d carriers=%d routes=%d groups=%d" % (
        m.get("ok"), len(m.get("ports") or []), len(m.get("carriers") or []),
        len(m.get("routes") or []), len(m.get("portGroups") or [])))
    probe = validate("rates", [{"起运港": "青岛", "目的港": "Santos", "船司": "MSK",
                                "柜型": "40HQ/HC", "海运费USD": 2850,
                                "有效期起": "2026-09-10", "有效期止": "2026-09-16",
                                "消息原文": "db_client smoke", "消息时间": "2026-09-10 10:00"}])
    print("validate:", json.dumps(probe, ensure_ascii=False)[:300])
