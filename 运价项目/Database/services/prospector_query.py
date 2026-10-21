"""Pure Prospector filter SQL, applied before counting or paging; values always bound."""
import json
MAX_FILTER_ITEMS = 64
MAX_FILTER_LENGTH = 512
RATE_TERM_COLUMNS = ('route','pod','pol')
SPACE_TERM_COLUMNS = (*RATE_TERM_COLUMNS,'container_type','vessel_voyage')

def filter_values(query, name):
    if not query.get(name): return []
    try: values = json.loads(query[name])
    except (TypeError, ValueError) as error: raise ValueError('invalid ' + name) from error
    if not isinstance(values,list) or len(values) > MAX_FILTER_ITEMS or any(not isinstance(v,str) or len(v)>MAX_FILTER_LENGTH for v in values): raise ValueError('invalid ' + name)
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))

def clauses(query, kind):
    remaining = dict(query)
    fragments, args = [], []
    def like(column, value):
        # literal substring, including user-provided percent/underscore
        value = str(value).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        return "LOWER(" + column + ") LIKE LOWER(?) ESCAPE '\\'", '%' + value + '%'
    terms = filter_values(query,'terms')
    if kind == 'space': terms += [str(query.get(k) or '').strip() for k in ('pod','lane') if query.get(k)]
    for term in terms:
        columns = SPACE_TERM_COLUMNS if kind == 'space' else RATE_TERM_COLUMNS
        entries = [like(column,term) for column in columns]
        fragments.append('(' + ' OR '.join(e[0] for e in entries) + ')'); args.extend(e[1] for e in entries)
    for field in ('pol','pod'):
        value, extras = query.get(field), filter_values(query,field+'Extra')
        entries=[]
        if value and not (kind == 'space' and field == 'pod'): entries.append(like(field,value))
        entries.extend((field + ' = ?',v) for v in extras)
        if entries:
            fragments.append('('+' OR '.join(e[0] for e in entries)+')'); args.extend(e[1] for e in entries)
        remaining.pop(field,None); remaining.pop(field+'Extra',None)
    lanes = filter_values(query,'lanes')
    if lanes:
        fragments.append('route IN ('+','.join('?' for _ in lanes)+')');args.extend(lanes)
    for key,column in (('lane','route'),('carrier','carrier')):
        if query.get(key) and not (key == 'lane' and kind == 'space'):
            fragment,value=like(column,query[key]);fragments.append(fragment);args.append(value)
        remaining.pop(key,None)
    remaining.pop('terms',None);remaining.pop('lanes',None)
    return remaining,fragments,args
