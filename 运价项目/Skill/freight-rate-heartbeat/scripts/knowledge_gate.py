# -*- coding: utf-8 -*-
"""Knowledge loading gate for heartbeat runs.

The gate verifies the files required by the current messages before OCR,
template acceptance, or LLM decisions. It records content hashes so later
stages can reuse a verified receipt without injecting duplicate full text.
"""
import hashlib
import json
import os
from datetime import datetime

import paths
import spec_index


GLOBAL_FILES = (
    os.path.join(paths.PKG_ROOT, "SKILL.md"),
    os.path.join(paths.HERE, "Instruction.md"),
    os.path.join(paths.PKG_ROOT, "references", "Instruction.md"),
    os.path.join(paths.PKG_ROOT, "references", "extraction-rules.md"),
    os.path.join(paths.PKG_ROOT, "references", "learnings.md"),
    os.path.join(paths.HERE, "llm_output_contract.md"),
)
MANIFEST_NAME = "knowledge_read_receipt.json"


class KnowledgeGateError(RuntimeError):
    """Required knowledge could not be routed or read."""


def _sha256(path):
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except (OSError, TypeError) as exc:
        raise KnowledgeGateError("知识文件不可读：%s（%s）" % (path, exc)) from exc
    if size == 0:
        raise KnowledgeGateError("知识文件为空：%s" % path)
    return size, digest.hexdigest()


def _required_files(message_groups):
    files = list(GLOBAL_FILES)
    group_files = {}
    for slug in sorted(set(message_groups)):
        spec = spec_index.spec_for(slug)
        if not spec.get("path") or not spec.get("exists"):
            raise KnowledgeGateError("群 %s 未定位到规范.md" % (slug or "?"))
        scoped = [spec["path"]]
        if spec.get("base_path"):
            scoped.append(spec["base_path"])
        if spec.get("knowledge_path"):
            scoped.append(spec["knowledge_path"])
        group_files[slug] = scoped
        files.extend(scoped)
    return files, group_files


def _load_manifest(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def ensure(run_dir, message_groups, message_ids, phase):
    """Verify current-run knowledge and return a persisted read receipt.

    The receipt is reused when the exact file path, size, and mtime are
    unchanged. A changed file is hashed again before the stage can continue.
    """
    os.makedirs(run_dir, exist_ok=True)
    receipt_path = os.path.join(run_dir, MANIFEST_NAME)
    previous = _load_manifest(receipt_path) or {}
    previous_files = previous.get("files") if isinstance(previous.get("files"), dict) else {}
    files, group_files = _required_files(message_groups)
    records = {}
    for path in sorted(set(files)):
        absolute = os.path.abspath(path)
        if not os.path.isfile(absolute):
            raise KnowledgeGateError("知识文件不存在：%s" % absolute)
        stat = os.stat(absolute)
        old = previous_files.get(absolute) or {}
        if (old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns
                and old.get("sha256")):
            size, sha256, reused = old["size"], old["sha256"], True
        else:
            size, sha256 = _sha256(absolute)
            reused = False
        records[absolute] = {"size": size, "mtime_ns": stat.st_mtime_ns,
                             "sha256": sha256, "reused": reused}
    receipt = {
        "ok": True,
        "phase": phase,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "message_ids": sorted(set(message_ids)),
        "groups": sorted(set(message_groups)),
        "group_files": {k: [os.path.abspath(p) for p in v] for k, v in group_files.items()},
        "files": records,
    }
    with open(receipt_path, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=1)
    return receipt
