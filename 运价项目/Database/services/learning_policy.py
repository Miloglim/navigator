"""Pure scope parsing and application; no database or filesystem access."""
from core.contracts import CONTEXT_ALIAS_SCOPE

FIELDS = {'pol':'起运港','pod':'目的港','carrier':'船司','route':'航线'}


def candidate_scope(candidate):
    parts = [p.strip() for p in candidate['scope_key'].split('|')]
    scope = candidate['scope_kind']
    if scope == CONTEXT_ALIAS_SCOPE:
        if len(parts) != len(('sender','carrier','group')) or not all(parts) or '*' in parts:
            raise ValueError('发送人、船司和群必须完整，不能扩大范围')
        return tuple(parts)
    if scope == 'group+column':
        if len(parts) != len(('column','group')) or parts[0] != candidate['kind'] or not parts[-1]:
            raise ValueError('群列作用域必须为 列类型|群名')
        return ('','',parts[-1])
    if scope == 'group' and len(parts) == 1 and parts[0] and parts[0] != '*':
        return ('','',parts[0])
    raise ValueError('不支持该别名作用域；禁止自动扩大到全群或全局')


def normalized_scope(candidate, normalize_carrier):
    sender,carrier,group = candidate_scope(candidate)
    return sender, normalize_carrier(carrier), group


def apply_context_aliases(record, rules, fold, normalize_carrier=lambda value:value):
    result = dict(record)
    context = tuple(str(record.get(k) or '').strip() for k in ('发送人','船司','来源群'))
    for kind, field in FIELDS.items():
        targets = {r['suggested_target'] for r in rules
                   if r['kind'] == kind and normalized_scope(r,normalize_carrier) == context
                   and fold(r['alias']) == fold(record.get(field))}
        if len(targets) > 1:
            raise ValueError('上下文规则冲突：'+field)
        if targets:
            result[field] = next(iter(targets))
    return result
