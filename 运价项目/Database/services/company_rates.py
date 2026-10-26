"""Pure request construction and response validation for company rates."""

from core.company_rates_contracts import (
    COMPANY_RATE_MAX_PORT_PAIRS,
    COMPANY_RATE_TARGET_URL,
    COMPANY_PORT_MATCH_TARGET_URL,
    COMPANY_CARRIER_MATCH_TARGET_URL,
    COMPANY_CITY_PORTS_TARGET_URL,
    COMPANY_REALTIME_CREATE_TARGET_URL,
    COMPANY_REALTIME_POLL_TARGET_URL,
    COMPANY_REALTIME_CONTAINER_TYPES,
)
import re
import unicodedata


def _positive_int(value, label):
    if isinstance(value, bool):
        raise ValueError(label + "格式不正确")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(label + "格式不正确") from error
    if number <= 0:
        raise ValueError(label + "格式不正确")
    return number


def _gateway_request(target_url, user_id, model):
    return {
        "url": target_url,
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": {"header": {"xUserId": _positive_int(user_id, "业务用户ID")}, "model": model},
    }


def port_pairs(pol_ids, pod_ids):
    origins = list(dict.fromkeys(_positive_int(value, "起运港ID") for value in pol_ids))
    destinations = list(dict.fromkeys(_positive_int(value, "目的港ID") for value in pod_ids))
    if not origins or not destinations:
        raise ValueError("请选择起运港和目的港")
    pairs = [(pol_id, pod_id) for pol_id in origins for pod_id in destinations]
    if len(pairs) > COMPANY_RATE_MAX_PORT_PAIRS:
        raise ValueError("一次最多20个港口组合")
    return pairs


def build_port_match_request(user_id, keyword):
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("港口名称不能为空")
    return _gateway_request(COMPANY_PORT_MATCH_TARGET_URL, user_id, {"keyword": keyword, "callSource": 1})


def build_carrier_match_request(user_id, search_text):
    search_text = str(search_text or "").strip()
    if not search_text:
        raise ValueError("船司名称不能为空")
    return _gateway_request(COMPANY_CARRIER_MATCH_TARGET_URL, user_id, {"search_text": search_text})


def build_city_ports_request(user_id, city_id):
    return _gateway_request(COMPANY_CITY_PORTS_TARGET_URL, user_id, {"cityId": _positive_int(city_id, "城市ID"), "types": [11]})


def _identity(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", "", text.casefold(), flags=re.UNICODE).replace("_", "")


COMPANY_CARRIER_ALIASES = {
    "msk": "MAERSK", "马士基": "MAERSK", "maersk": "MAERSK",
    "cmacgm": "CMA", "cma": "CMA", "达飞": "CMA",
}


def lookup_variants(value, aliases=None):
    raw = str(value or "").strip()
    if not raw:
        return []
    variants = [raw]
    plain = unicodedata.normalize("NFKD", raw)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = re.sub(r"[\s\-’']+", " ", plain).strip()
    if plain and plain != raw:
        variants.append(plain)
    folded = _identity(raw)
    if aliases and folded in aliases:
        variants.append(aliases[folded])
    return list(dict.fromkeys(variants))


def select_exact_port(keyword, candidates, codes=(), country="", identities=()):
    wanted = _identity(keyword)
    if not wanted:
        raise ValueError("港口名称不能为空")
    candidates = [row for row in candidates if isinstance(row, dict) and row.get("type") == 11
                  and row.get("id") is not None]
    if country:
        # UN/LOCODE carries country identity. Unknown country is not evidence of a match.
        candidates = [row for row in candidates if (
            str(row.get("code") or "").upper().startswith(country.upper())
            or str(row.get("countryCode") or "").upper() == country.upper())]
    names = {_identity(value) for value in identities if value}
    name_matches = [row for row in candidates if names.intersection(
        _identity(row.get(key)) for key in ('enName', 'name', 'cnName'))]
    code_values = {_identity(code) for code in codes}
    code_matches = [row for row in candidates if _identity(row.get("code")) in code_values]
    if code_matches:
        # A local code only proves identity when the same company row also matches its name or alias.
        code_and_name_matches = [row for row in code_matches if row in name_matches]
        candidates = code_and_name_matches or name_matches
    elif names:
        candidates = name_matches
    exact = [row for row in candidates if wanted in {
        _identity(row.get("code")), _identity(row.get("enName")), _identity(row.get("name")), _identity(row.get("cnName"))
    }]
    if len(exact) != 1:
        raise ValueError("无法精确匹配港口：" + str(keyword))
    _positive_int(exact[0]["id"], "港口ID")
    return exact[0]


def select_exact_city(keyword, candidates):
    wanted = _identity(keyword)
    exact = [row for row in candidates if isinstance(row, dict) and row.get("type") == 4 and wanted in {
        _identity(row.get("code")), _identity(row.get("enName")), _identity(row.get("name")), _identity(row.get("cnName"))
    } and row.get("id") is not None]
    if len(exact) != 1:
        raise ValueError("无法精确匹配城市：" + str(keyword))
    _positive_int(exact[0]["id"], "城市ID")
    return exact[0]


def select_exact_carrier(search_text, candidate, identities=()):
    wanted = _identity(search_text)
    if isinstance(candidate, list):
        matches = []
        for row in candidate:
            try:
                matches.append(select_exact_carrier(search_text, row, identities))
            except ValueError:
                continue  # Fuzzy candidates do not prove carrier identity.
        if len(matches) != 1:
            raise ValueError("船司候选不唯一或未找到：" + str(search_text))
        return matches[0]
    if not wanted or not isinstance(candidate, dict) or wanted not in {
        _identity(candidate.get("match_text")), _identity(candidate.get("code")), _identity(candidate.get("name")),
    } or candidate.get("id") is None:
        raise ValueError("无法精确匹配船司：" + str(search_text))
    if candidate.get("code") and _identity(candidate["code"]) not in {
        _identity(value) for value in (search_text, *identities)
    }:
        raise ValueError("船司标准代码与本地身份不一致：" + str(search_text))
    _positive_int(candidate["id"], "船司ID")
    return candidate


def local_mapping_profile(name, kind, rows):
    """Derive lookup inputs from existing master data, without modifying canonical names."""
    primary = "name" if kind == "port" else "code"
    exact = [r for r in rows if str(r.get(primary) or "").casefold() == str(name).casefold()]
    def names(row):
        return [row.get(primary), row.get("name_en"), row.get("name_cn"), row.get("locode_ref")]
    if not exact:
        exact = [r for r in rows if _identity(name) in {_identity(v) for v in names(r) + re.split(r"[,，;；]", r.get("aliases") or "") if v}]
    if len(exact) > 1:
        raise ValueError("本地主数据名称不唯一，需要确认：" + str(name))
    row = exact[0] if exact else {}
    aliases = [v.strip() for v in re.split(r"[,，;；]", row.get("aliases") or "") if v.strip()]
    codes = [v for v in [row.get("locode_ref"), *aliases] if re.fullmatch(r"[A-Za-z]{2}[A-Za-z0-9]{3}", v or "")]
    values = ([*codes, row.get("name_en"), row.get("name"), name, row.get("name_cn"), *aliases]
              if kind == "port" else [row.get("code"), name, row.get("name_en"), *aliases, row.get("scac"), row.get("name_cn")])
    variants = []
    for value in values:
        if value:
            confirmed = COMPANY_CARRIER_ALIASES.get(_identity(value)) if kind == "carrier" else None
            variants.extend(([confirmed] if confirmed else []) + lookup_variants(value))
    identities = [value for value in (row.get('name'),row.get('name_en'),row.get('name_cn'),name)
                  if value and not re.fullmatch(r'[A-Za-z]{2}[A-Za-z0-9]{3}',value)] if kind == 'port' else []
    # A caller's code or alias is not independent evidence of the port's identity.
    if row and kind == 'port':
        aliases = [value for value in aliases if not re.fullmatch(r'[A-Za-z]{2}[A-Za-z0-9]{3}', value)]
        identities = [value for value in (row.get('name'),row.get('name_en'),row.get('name_cn'),name,*aliases) if value]
    return {"variants": list(dict.fromkeys(variants)), "codes": codes, "identities":identities,
            "country": row.get("country") or "", "canonical": row.get(primary) or name}


def build_company_rate_request(user_id, pol_id, pod_id, carrier_ids, valid_from=None, valid_to=None):
    model = {
        "pol_id": _positive_int(pol_id, "起运港ID"),
        "pod_id": _positive_int(pod_id, "目的港ID"),
        "pol_port_type": 11,
        "pod_port_type": 11,
        "carrier_id": [_positive_int(value, "船司ID") for value in carrier_ids],
        "freight_charge_sort": 2,
        "search_flag": 2,
    }
    if valid_from is not None:
        model["valid_from"] = _positive_int(valid_from, "有效期起")
    if valid_to is not None:
        model["valid_to"] = _positive_int(valid_to, "有效期止")
    return _gateway_request(COMPANY_RATE_TARGET_URL, user_id, model)


def build_realtime_create_request(user_id, pol_id, pod_id, carrier_ids, container_ids,
                                  valid_from, valid_to, weight):
    normalized_containers = [_positive_int(value, "柜型ID") for value in container_ids]
    if any(value not in COMPANY_REALTIME_CONTAINER_TYPES for value in normalized_containers):
        raise ValueError("实时查价柜型暂不支持")
    model = {
        "polId": _positive_int(pol_id, "起运港ID"),
        "podId": _positive_int(pod_id, "目的港ID"),
        "carrierIds": [_positive_int(value, "船司ID") for value in carrier_ids],
        "containerTypes": normalized_containers,
        "validFrom": _positive_int(valid_from, "有效期起"),
        "channel": 1,
        "weight": _positive_int(weight, "单箱重量"),
        "shipOwnerShippingClauseId": 1,
    }
    if valid_to is not None:
        model["validTo"] = _positive_int(valid_to, "有效期止")
    if not model["carrierIds"] or len(model["containerTypes"]) != 1:
        raise ValueError("实时价格须选择船司和一个柜型")
    return _gateway_request(COMPANY_REALTIME_CREATE_TARGET_URL, user_id, model)


def build_realtime_poll_request(user_id, tasks, container_id):
    normalized = []
    for task in tasks:
        if not isinstance(task, dict) or "carrierId" not in task or "taskId" not in task:
            raise ValueError("实时任务必须来自本次创建结果")
        normalized.append({
            "carrierId": _positive_int(task["carrierId"], "船司ID"),
            "taskId": _positive_int(task["taskId"], "实时任务ID"),
        })
    if not normalized:
        raise ValueError("实时任务必须来自本次创建结果")
    normalized_container = _positive_int(container_id, "柜型ID")
    if normalized_container not in COMPANY_REALTIME_CONTAINER_TYPES:
        raise ValueError("实时查价柜型暂不支持")
    return _gateway_request(COMPANY_REALTIME_POLL_TARGET_URL, user_id, {
        "task": normalized,
        "containerId": normalized_container,
    })


def normalize_gateway_response(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("公司接口调用失败")
    if payload.get("code", 200) != 200 or payload.get("msgCode") not in (None, "", "SUCCESS"):
        raise ValueError("公司接口业务状态失败")
    if "data" not in payload:
        raise ValueError("公司接口响应格式不正确")
    return payload["data"]

