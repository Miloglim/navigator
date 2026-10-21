"""Pure validation for explicitly authorized, evidence-backed Agent repairs."""
import hashlib
import json
import re
from urllib.parse import urlparse
from core.contracts import AGENT_MASTER_ENTITIES, AGENT_PENDING_REPAIR_FIELDS


def payload_signature(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def repair_evidence(body):
    evidence = body.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("repair evidence required")
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") not in {"official_source", "group_spec", "original_image"}:
            raise ValueError("invalid evidence kind")
        if not str(item.get("reference") or "").strip() or not str(item.get("reason") or "").strip():
            raise ValueError("evidence reference and reason required")
        if item["kind"] == "official_source":
            parsed = urlparse(item["reference"])
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("official evidence requires an https source URL")
    return evidence


def master_request(body):
    evidence = repair_evidence(body)
    entity = body.get("entity")
    if entity not in AGENT_MASTER_ENTITIES:
        raise ValueError("only additive port/alias repair is allowed")
    if not str(body.get("source_group") or "").strip() or not str(body.get("message_id") or "").strip():
        raise ValueError("source group and message ID required")
    row = body.get("row")
    if not isinstance(row, dict):
        raise ValueError("row required")
    if entity == "port":
        allowed = {"name", "country", "name_en", "name_cn", "kind", "routes_list", "locode_ref"}
        if set(row) - allowed or not str(row.get("name") or "").strip():
            raise ValueError("invalid port fields")
        if not re.fullmatch(r"[A-Z]{2}", str(row.get("country") or "")):
            raise ValueError("ISO country code required")
        if row.get("kind") not in {"pol", "pod", "both"}:
            raise ValueError("port kind required")
        locode = str(row.get("locode_ref") or "")
        if locode and (not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", locode) or not locode.startswith(row["country"])):
            raise ValueError("UNLOCODE must match country")
        if not any(e["kind"] == "official_source" for e in evidence):
            raise ValueError("new ports require researched official evidence")
    else:
        if set(row) - {"alias", "kind", "target"} or row.get("kind") not in {"pol", "pod", "carrier", "route"}:
            raise ValueError("invalid scoped alias fields")
        if not str(row.get("alias") or "").strip() or not str(row.get("target") or "").strip():
            raise ValueError("alias and canonical target required")
    return entity, dict(row), evidence


def pending_request(body):
    repair_evidence(body)
    if not isinstance(body.get("retain_failed", False), bool):
        raise ValueError("retain_failed must be boolean")
    if body.get("kind") not in (None, "rates", "space"):
        raise ValueError("repair kind must be rates or space")
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields or set(fields) - AGENT_PENDING_REPAIR_FIELDS:
        raise ValueError("invalid repair fields; provenance/review decisions cannot be changed")
    if not re.fullmatch(r"[a-f0-9]{64}", str(body.get("expected_signature") or "")):
        raise ValueError("expected payload signature required")
    return int(body.get("id") or 0), fields


def pending_source_scope(records, preserve_other_sources, preserve_existing_source_rows=False):
    """Each complete source identity keeps its own pending product revisions."""
    if not isinstance(preserve_other_sources, bool):
        raise ValueError("preserve_other_sources must be boolean")
    if not isinstance(preserve_existing_source_rows, bool):
        raise ValueError("preserve_existing_source_rows must be boolean")
    if preserve_existing_source_rows and not preserve_other_sources:
        raise ValueError("preserve_existing_source_rows requires preserve_other_sources=true")
    if not preserve_other_sources:
        return None
    sources = {(str(r.get("来源群") or "").strip(), str(r.get("消息ID") or "").strip()) for r in records}
    if not sources or any(not all(source) for source in sources):
        raise ValueError("source-scoped pending submission requires nonempty group/message IDs")
    return sources
