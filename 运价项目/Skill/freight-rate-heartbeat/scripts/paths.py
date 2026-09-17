# -*- coding: utf-8 -*-
"""paths.py —— 全 skill 的路径单一真源。

只有这一个文件知道"代码在哪、配置在哪、运行态(爬取水位/scratch/ocr缓存)落在哪"。
其它脚本都从这里取路径常量，避免各算各的导致漂移。

关键设计：运行态目录 RUNTIME_DIR **移出 skill 包**（默认 skill 同级的 `heartbeat-data/`），
这样打包/安装不会把 .work 缓存、watermark.json、ocr_cache 一并带进去。
优先级：环境变量 FRT_RUNTIME_DIR > config/ledger_contract.json 的 runtime_dir > 默认同级目录。
"""
import os, json

HERE = os.path.dirname(os.path.abspath(__file__))                 # .../freight-rate-heartbeat/scripts
PKG_ROOT = os.path.dirname(HERE)                                   # .../freight-rate-heartbeat
CONFIG_DIR = os.path.join(HERE, "config")
CONTRACT_F = os.path.join(CONFIG_DIR, "ledger_contract.json")
CHAT_SOURCES_F = os.path.join(CONFIG_DIR, "chat_sources.json")

CONTRACT = json.load(open(CONTRACT_F, encoding="utf-8"))
_db = CONTRACT.get("db_conn", {})


# 项目根锚点：env FRT_PROJECT_ROOT > config project_root > 由源副本位置推断（<root>/Skill/freight-rate-heartbeat → <root>）
def _guess_root():
    cand = os.path.dirname(os.path.dirname(PKG_ROOT))
    return cand if os.path.isdir(os.path.join(cand, "Database")) else ""


PROJECT_ROOT = (os.environ.get("FRT_PROJECT_ROOT") or CONTRACT.get("project_root")
                or _guess_root() or "")


def resolve(p):
    """相对 project_root 的片段 → 绝对路径；已是绝对路径或无锚点则原样返回。"""
    if not p:
        return p
    if os.path.isabs(p) or not PROJECT_ROOT:
        return p
    return os.path.join(PROJECT_ROOT, p)


# 运行态（scratch / 水位 / 识图缓存）落在包外，相对 project_root；images_dir 属台账，由 db_conn 决定（ledger_client 里 resolve）
RUNTIME_DIR = (os.environ.get("FRT_RUNTIME_DIR") or resolve(CONTRACT.get("runtime_dir"))
               or os.path.join(os.path.dirname(PKG_ROOT), "heartbeat-data"))
WORK = os.path.join(RUNTIME_DIR, ".work")            # 每轮 run_<D>/msgs_<D>/draft_<D>/llm_out_<D> 的父目录
OCR_CACHE = os.path.join(WORK, "ocr_cache")           # 识图缓存（跨群复用），mediaId 为键
WATERMARK_F = os.path.join(RUNTIME_DIR, "watermark.json")


def ensure(path):
    """确保目录存在并返回它。"""
    os.makedirs(path, exist_ok=True)
    return path


def chat_groups():
    """要爬取的钉钉群清单（config/chat_sources.json 的 groups 数组）。"""
    return json.load(open(CHAT_SOURCES_F, encoding="utf-8"))["groups"]


if __name__ == "__main__":
    print("HERE        =", HERE)
    print("PKG_ROOT    =", PKG_ROOT)
    print("PROJECT_ROOT=", PROJECT_ROOT or "(未解析！设 FRT_PROJECT_ROOT 或 config.project_root)")
    print("RUNTIME_DIR =", RUNTIME_DIR, "(exists=%s)" % os.path.isdir(RUNTIME_DIR))
    print("WORK        =", WORK)
    print("WATERMARK_F =", WATERMARK_F)
    print("token_file  =", resolve(_db.get("token_file")))
    print("images_dir  =", resolve(_db.get("images_dir")))
