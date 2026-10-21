"""Pure validation for administrator-confirmed external identities."""
from services.company_rates import local_mapping_profile
from core.company_rates_contracts import CompanyMappingForm, COMPANY_MAPPING_FIELD_MAX_LENGTH, COMPANY_PORT_CODE_LENGTH

def mapping_form(body, master) -> CompanyMappingForm:
    kind = body.get('kind')
    if kind not in ('port', 'carrier'):
        raise ValueError('请选择港口或船司')
    name = str(body.get('local_name') or '').strip()
    rows = master['ports' if kind == 'port' else 'carriers']
    profile = local_mapping_profile(name, kind, rows)
    primary = 'name' if kind == 'port' else 'code'
    local = next((r for r in rows if r.get(primary) == profile['canonical']), None)
    if not local:
        raise ValueError('本地名称必须属于启用的主数据')
    value = body.get('company_id')
    if isinstance(value, bool) or not str(value or '').isdigit() or int(value) <= 0:
        raise ValueError('公司编号必须为正整数')
    status = body.get('status')
    if status not in ('pending', 'confirmed', 'disabled'):
        raise ValueError('映射状态不正确')
    code = str(body.get('company_code') or '').strip()
    country = profile['country'] if kind == 'port' else ''
    if kind == 'port' and country and code and len(code) == COMPANY_PORT_CODE_LENGTH and not code.upper().startswith(country.upper()):
        raise ValueError('公司港口代码与本地国家不一致')
    fields = { 'kind': kind, 'local_name': profile['canonical'], 'company_id': int(value),
               'company_code': code, 'company_name': str(body.get('company_name') or '').strip(),
               'country': country, 'status': status, 'note': str(body.get('note') or '').strip() }
    if any(len(v) > COMPANY_MAPPING_FIELD_MAX_LENGTH for v in fields.values() if isinstance(v, str)):
        raise ValueError('映射字段过长')
    return fields
