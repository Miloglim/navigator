"""Pure production envelope and ledger conversions; no IO or mutable globals."""
from datetime import date, timedelta
from calendar import monthrange
import json
import re
from freight_flow.core.contracts import AI_VALIDITY_DAYS, YEAR_ROLLOVER_MONTHS
from freight_flow.services.workflow import source_row_key
MONTHS = {name: number for number, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), start=1)}
PRODUCT_FIELDS = ('起运港', '目的港', '船司', '柜型', '海运费USD', 'ETD',
                  '有效期开始', '有效期截止', 'service', 'tag', '船名航次', '目的码头')
WEEK_LAST_DAY_OFFSET = 6
RANGE_SEPARATOR = r'\s*(?:[-~–—]|至|到|\bto\b)\s*'
NUMERIC_DATE = r'(?:\d{4}[-/.])?\d{1,2}[-/.]\d{1,2}'
ENGLISH_DATE = r'(?:\d{1,2}[- /][A-Za-z]{3}|[A-Za-z]{3}[- /]\d{1,2})'
WEEK_PATTERN = (r'(?:(\d{4})\s*年?\s*)?(?:第\s*)?(\d{1,2})\s*周|'
                r'(?:(\d{4})\s*)?WK\s*(\d{1,2})')


def merge_products(records):
    """The ledger stores price/space fields as a union, one row per actual product."""
    products = {}
    for candidate in records:
        key = (*tuple(str(candidate.get(k, '')).strip() for k in PRODUCT_FIELDS), source_row_key(candidate))
        current = products.get(key)
        if (current and current['_table'] == candidate['_table'] == 'space'
                and current.get('箱量') not in ('', None) and candidate.get('箱量') not in ('', None)
                and current.get('箱型箱量') and candidate.get('箱型箱量')
                and current['箱型箱量'] != candidate['箱型箱量']
                and str(current['箱量']).strip() != str(candidate['箱量']).strip()):
            # Explicit distinct allocations are distinct source rows, not duplicate prices.
            key = (*key, '箱量', str(candidate['箱量']).strip())
        if key not in products:
            products[key] = dict(candidate)
            continue
        current = products[key]
        if current['_table'] == candidate['_table']:
            raise ValueError('同产品重复候选，不能用多余行凑覆盖')
        for field, value in candidate.items():
            if field == '_table' or value in ('', None):
                continue
            old = current.get(field)
            if field in ('备注', '证据'):
                if value != old:
                    current[field] = ';'.join(filter(None, (old, value)))
            elif old in ('', None):
                current[field] = value
            elif str(old).strip() != str(value).strip():
                raise ValueError('同报价运价/舱位字段冲突: ' + field)
        current['_table'] = 'space'
    return list(products.values())


def parse_pull(envelope):
    if (not isinstance(envelope, dict) or envelope.get('success') is False
            or envelope.get('complete') is not True or envelope.get('paginationKnown') is not True
            or envelope.get('hasMore') is not False
            or any(envelope.get(key) for key in ('partial', 'truncated', 'truncatedByPageLimit',
                   'truncatedByResultLimit', 'failedCount', 'decryptFailedCount', 'failures'))):
        raise ValueError('DWS 拉取不完整，不能推进水位')
    messages = envelope.get('messages')
    if not isinstance(messages, list) or envelope.get('count', len(messages)) != len(messages):
        raise ValueError('DWS 条数与消息列表不一致')
    if any(not isinstance(m, dict) or not (m.get('openMessageId') or m.get('messageId') or m.get('id'))
           or not m.get('createTime') for m in messages):
        raise ValueError('DWS 缺消息ID或时间')
    return messages


def source_text(message):
    text = message.get('content') if message.get('content') is not None else message.get('text', '')
    if not isinstance(text, str):
        raise ValueError('原文不是文本，需明确处理该消息类型')
    quoted = message.get('quotedMessage') or {}
    content = quoted.get('content') or quoted.get('text') or ''
    if content.strip():
        mid = quoted.get('openMessageId') or quoted.get('messageId') or quoted.get('id')
        text += '\n\n——引用 ' + str(quoted.get('createTime') or '')[:16] + ' ' + str(quoted.get('sender') or '')
        text += (' mID=' + mid if mid else '') + '——\n' + content.strip()
    return text


def source_for_message(group, message, knowledge):
    quoted_message = message.get('quotedMessage') or {}
    current_message_id = message.get('openMessageId') or message.get('messageId') or message['id']
    items = (quoted_message, message)
    verified = {}
    for item in items:
        for resource in item.get('resourceRefs') or []:
            args = (resource.get('download') or {}).get('arguments') or {}
            rid, owner = resource.get('resourceId'), args.get('message-id')
            if not rid or not owner:
                continue
            if args.get('resource-id') not in (None, rid) or args.get('open-conversation-id') not in (None, group['cid']):
                raise ValueError('图片下载元数据与当前资源/群不一致')
            if rid in verified and verified[rid] != owner:
                raise ValueError('图片真实所属消息元数据冲突')
            verified[rid] = owner
    display_media = {}
    parse_media = {}
    for item in items:
        mid = item.get('openMessageId') or item.get('messageId') or item.get('id')
        resources = [r['resourceId'] for r in item.get('resourceRefs') or []
                     if r.get('resourceId') and (r.get('resourceIdType') or r.get('type')) == 'mediaId']
        content = item.get('content') or item.get('text') or ''
        if isinstance(content, str):
            resources += re.findall(r'mediaId=(@[A-Za-z0-9_-]+)', content)
        for resource in resources:
            if not mid:
                raise ValueError('图片缺少所属消息ID，不能假定属于回复消息')
            owner = verified.get(resource) or mid
            display_media.setdefault(resource, owner)
            if owner == current_message_id:
                parse_media.setdefault(resource, owner)
    own_text = message.get('content') if message.get('content') is not None else message.get('text', '')
    quoted_text = quoted_message.get('content') or quoted_message.get('text') or ''
    quoted_media_ids = [resource for resource, owner in display_media.items()
                        if owner != current_message_id]
    return {'message_id': message.get('openMessageId') or message.get('messageId') or message['id'],
            'text': source_text(message), 'own_text': str(own_text or ''),
            'quoted_text': str(quoted_text or ''),
            'quoted_message_id': quoted_message.get('openMessageId') or quoted_message.get('messageId') or quoted_message.get('id') or '',
            'message_time': message['createTime'], 'group': group,
            'sender': message.get('sender') or '', 'sender_id': message.get('senderId') or '',
            'sender_openid': message.get('senderOpenDingTalkId') or '', 'group_knowledge': knowledge,
            'media_ids': list(display_media), 'display_media_ids': list(display_media),
            'parse_media_ids': list(parse_media), 'quoted_media_ids': quoted_media_ids,
            'media_messages': display_media,
            'media_roles': {resource: ('own' if resource in parse_media else 'quoted')
                            for resource in display_media}}


def classify_quoted_followup(source, result):
    """A short amendment to quoted prices must not regenerate the quoted products."""
    if not isinstance(result, dict) or result.get('decision') != 'quote':
        return result
    own = str(source.get('own_text') or '').strip()
    quoted = str(source.get('quoted_text') or '').strip()
    if not own or not quoted:
        return result
    explicit_price = re.search(
        r'(?i)(?:USD|US\$|\$)\s*\d{3,6}|\d{3,6}\s*/\s*\d{3,6}|\b\d{3,6}\s*(?:USD|美金)\b', own)
    amendment = re.search(r'先按|按照|合同|等修改|修改|更正|补充|调整|更新为|改为|批复', own)
    if explicit_price or not amendment:
        return result
    return {**result, 'decision': 'followup',
            'reason': '本条仅补充或修订被引用报价，不独立生成产品；引用消息=' +
                      str(source.get('quoted_message_id') or '')}


def _date(value, anchor):
    if not value:
        return ''
    value = value.strip()
    value = re.sub(r'^([A-Za-z]{3})\.\s*', r'\1 ', value)
    chinese = re.fullmatch(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日', value)
    if chinese:
        year, month, day = chinese.groups()
        if year:
            return date(int(year), int(month), int(day)).isoformat()
        value = month + '/' + day
    full = re.fullmatch(
        r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?',
        value)
    if full:
        return date(*map(int, full.groups())).isoformat()
    bare_day = re.fullmatch(r'(\d{1,2})[号日]', value)
    if bare_day:
        day = int(bare_day.group(1))
        month = anchor.month + 1 if day < anchor.day else anchor.month
        year = anchor.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        return date(year, month, day).isoformat()
    match = re.fullmatch(r'(\d{1,2})[-/.](\d{1,2})', value)
    if not match:
        english = re.fullmatch(r'([A-Za-z]{3})[- /.](\d{1,2})', value)
        reverse = re.fullmatch(r'(\d{1,2})[- /.]([A-Za-z]{3})\.?', value)
        if english or reverse:
            name, day = english.groups() if english else reverse.groups()[::-1]
            if name.lower() in MONTHS:
                value = str(MONTHS[name.lower()]) + '/' + day
                match = re.fullmatch(r'(\d{1,2})[/.](\d{1,2})', value)
    if not match:
        raise ValueError('日期无法机械转换: ' + value)
    month, day = map(int, match.groups())
    year = anchor.year + (1 if anchor.month - month > YEAR_ROLLOVER_MONTHS else
                          -1 if month - anchor.month > YEAR_ROLLOVER_MONTHS else 0)
    return date(year, month, day).isoformat()


def _validity_window(value, anchor):
    """Only a validity field authorizes interpreting a week/range as a window."""
    value = (value or '').strip()
    week = re.fullmatch(WEEK_PATTERN, value, re.I)
    if week:
        year = int(week.group(1) or week.group(3) or anchor.year)
        number = int(week.group(2) or week.group(4))
        start = date.fromisocalendar(year, number, 1)
        return start.isoformat(), (start + timedelta(days=WEEK_LAST_DAY_OFFSET)).isoformat()
    atom = '(?:' + NUMERIC_DATE + '|' + ENGLISH_DATE + ')'
    match = re.fullmatch('(' + atom + ')' + RANGE_SEPARATOR + '(' + atom + r'|\d{1,2})',
                         value, re.I)
    if not match:
        return None
    left, right = match.groups()
    start = date.fromisoformat(_date(left, anchor))
    if right.isdigit():
        end = date(start.year, start.month, int(right))
    else:
        end = date.fromisoformat(_date(right, start))
        # A missing right-hand year follows the left year, rolling only across December.
        explicit_year = re.match(r'\d{4}[-/.]', right)
        if not explicit_year:
            end = end.replace(year=start.year + (1 if end.month < start.month else 0))
    if end < start:
        raise ValueError('有效期起晚于止')
    return start.isoformat(), end.isoformat()


def _week_reference(value):
    value = (value or '').strip()
    return bool(re.fullmatch(WEEK_PATTERN, value, re.I)
                or re.fullmatch(r'(?:WK|WEEK)?\s*\d{1,2}\s*周?\s*开始', value, re.I))


def normalize_candidate_dates(candidate, anchor, *, source_text='', week_as_validity=False):
    """Return a copy with single ETD and explicit validity bounds; never infer ETD."""
    normalized = dict(candidate)
    raw_etd = (candidate.get('ETD') or '').strip()
    etd_window = _validity_window(raw_etd, anchor)
    if _week_reference(raw_etd):
        previous = str(normalized.get('备注') or '')
        note = '未绑定船期:' + raw_etd
        if note not in previous:
            normalized['备注'] = ';'.join(filter(None, (previous, note)))
        normalized['ETD'] = ''
    elif etd_window:
        # Only the source's explicit ETD label authorizes preserving a sailing window.
        if not re.search(
                r'\bETD\s*[:：]?\s*' + re.escape(raw_etd) + r'(?![\d./])', source_text, re.I):
            raise ValueError('船期区间缺少原文明示ETD证据: ' + raw_etd)
        original = 'ETD' + raw_etd
        previous = str(normalized.get('备注') or '')
        if original not in previous:
            normalized['备注'] = ';'.join(filter(None, (previous, original)))
        normalized['ETD'] = ''
    else:
        normalized['ETD'] = _date(raw_etd, anchor)
    bounds = ['', '']
    for index, field in enumerate(('有效期开始', '有效期截止')):
        value = candidate.get(field)
        if field == '有效期截止' and (value or '').strip() == '月底':
            value = date(anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1]).isoformat()
        window = _validity_window(value, anchor)
        if _week_reference(value) and window is None:
            previous = str(normalized.get('备注') or '')
            note = '未绑定有效期:' + value.strip()
            if note not in previous:
                normalized['备注'] = ';'.join(filter(None, (previous, note)))
            continue
        proposed = window or ((_date(value, anchor), '') if index == 0 else
                              ('', _date(value, anchor)))
        for position, bound in enumerate(proposed):
            if bound and bounds[position] and bound != bounds[position]:
                raise ValueError('有效期区间与明示日期冲突: ' + field)
            if bound:
                bounds[position] = bound
    if not any(bounds) and week_as_validity:
        weeks = list(re.finditer(WEEK_PATTERN, source_text, re.I))
        # A source with multiple week numbers requires explicit per-image/per-product binding.
        if len(weeks) == 1 and not re.search(r'\d{1,2}\s*/\s*\d{1,2}\s*周', source_text):
            bounds = list(_validity_window(weeks[0].group(), anchor))
    if all(bounds) and bounds[0] > bounds[1]:
        raise ValueError('有效期起晚于止')
    normalized['有效期开始'], normalized['有效期截止'] = bounds
    return normalized


def records_for_ledger(source, candidates):
    grouped = {'rates': [], 'space': []}
    anchor = date.fromisoformat(source['message_time'][:10])
    for candidate in candidates:
        candidate = normalize_candidate_dates(candidate, anchor, source_text=source['text'],
                                               week_as_validity=source.get('week_as_validity') is True)
        rec = {k: v for k, v in candidate.items() if not k.startswith('_') and k not in
               ('section_role', '证据', 'ETD', '有效期开始', '有效期截止', 'service', 'tag') and v != ''}
        currency = str(rec.pop('currency', '') or 'USD').strip().upper()
        rec['起运港'] = candidate.get('起运港') or source['group'].get('default_pol') or ''
        etd = candidate['ETD']
        start = candidate['有效期开始']
        end = candidate['有效期截止']
        if not start and not end:
            start = anchor.isoformat()
            end = etd if etd and etd >= start else (anchor + timedelta(days=AI_VALIDITY_DAYS)).isoformat()
            if not etd or etd < start:
                rec['AI判断'] = '是'
        if start and end and start > end:
            raise ValueError('有效期起晚于止')
        rec.update({'有效期起': start, '有效期止': end, '船期ETD': etd,
                    '航线服务': candidate.get('service') or '', '价格分类': candidate.get('tag') or '',
                    '币种': currency,
                    '消息原文': source['text'], '消息ID': source['message_id'],
                    '消息时间': source['message_time'], '来源群': source['group']['name'],
                    '来源群ID': source['group']['cid'], '发送人': source['sender'],
                    '发送人ID': source.get('sender_id', ''), '发送人OpenID': source.get('sender_openid', ''),
                    '图片名': ','.join(source.get('image_files') or [])})
        source_row = source_row_key(candidate)
        if source_row:
            rec['_source_row'] = source_row
        tag = candidate.get('tag') or ''
        if tag and tag not in str(rec.get('备注') or ''):
            rec['备注'] = ';'.join(filter(None, (rec.get('备注'), tag)))
        kind = 'space' if candidate['_table'] == 'space' else 'rates'
        if kind == 'space':
            rec['价格USD'] = rec.pop('海运费USD', '')
        grouped[kind].append(rec)
    return grouped


PARTIAL_META_FIELDS = (
    'partial_review', 'partial_stage', 'partial_reasons', 'partial_completed_stages',
    'partial_products_identified', 'partial_candidates_excluded', 'partial_product_status',
    'message_partial_review', 'message_partial_stage', 'message_partial_reasons',
    'message_partial_completed_stages', 'message_partial_products_identified',
    'message_partial_candidates_excluded',
)


def _invalid_product_reason(kind, record):
    """Drop confirmed non-products while retaining genuine one-field gaps for review."""
    if kind == 'rates':
        required = ('目的港', '船司', '柜型', '海运费USD')
        missing = [field for field in required if record.get(field) in (None, '')]
        if len(missing) >= 2:
            return '核心字段缺失：' + '/'.join(missing)
        price = str(record.get('海运费USD') or '').strip()
        note = str(record.get('备注') or '')
        if price in ('不收', '不接', '/', '停止接载', '暂停接载') or (
                not price and re.search(r'无确认报价|没有价格|不是海运费|原图.{0,8}不收', note)):
            return '明确不是可订舱的基础运价产品'
    return ''


def partial_records(source, candidates, failed_stage, reason, completed_stages=()):
    """Build the largest auditable subset without hiding candidates that failed conversion."""
    grouped = {'rates': [], 'space': []}
    excluded = []
    identified = 0
    normalized_input = isinstance(candidates, dict)
    if normalized_input:
        items = [(kind, candidate) for kind in ('rates', 'space') for candidate in (candidates.get(kind) or [])]
        identified = len(items)
    else:
        items = [('candidate', candidate) for candidate in (candidates or [])]
        identified = len(items)

    for expected_kind, candidate in items:
        if not isinstance(candidate, dict):
            excluded.append({'candidate': candidate, 'reason': '候选不是对象'})
            continue
        section_role = str(candidate.get('section_role') or '').strip().lower()
        if section_role and section_role != 'product':
            excluded.append({'candidate': candidate,
                             'reason': '非产品分区，仅保留证据：' + section_role})
            continue
        try:
            if normalized_input:
                kind = expected_kind
                converted = {kind: [dict(candidate)]}
            else:
                converted = records_for_ledger(source, [candidate])
        except (KeyError, TypeError, ValueError) as exc:
            excluded.append({'candidate': candidate, 'reason': str(exc)})
            continue  # One malformed candidate must not discard independently usable products.
        for kind in ('rates', 'space'):
            for record in converted.get(kind) or []:
                invalid = _invalid_product_reason(kind, record)
                if invalid:
                    excluded.append({'candidate': record, 'kind': kind, 'reason': invalid})
                else:
                    grouped[kind].append(record)

    completed = list(dict.fromkeys(str(stage) for stage in completed_stages if stage))
    metadata = {
        'partial_review': False,
        'partial_stage': str(failed_stage),
        'partial_reasons': [str(reason)],
        'partial_completed_stages': completed,
        'partial_products_identified': identified,
        'partial_candidates_excluded': excluded,
        'partial_product_status': 'pending_validation',
        'message_partial_review': True,
        'message_partial_stage': str(failed_stage),
        'message_partial_reasons': [str(reason)],
        'message_partial_completed_stages': completed,
        'message_partial_products_identified': identified,
        'message_partial_candidates_excluded': excluded,
    }
    prepared = {'rates': [], 'space': []}
    seen = set()
    for kind in ('rates', 'space'):
        for record in grouped[kind]:
            source_record = {key: value for key, value in record.items() if key not in PARTIAL_META_FIELDS}
            identity = json.dumps(source_record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if identity in seen:
                continue
            seen.add(identity)
            enriched = {**source_record, **metadata}
            enriched['备注'] = str(source_record.get('备注') or '').strip()
            prepared[kind].append(enriched)
    return {
        'records': prepared,
        'identified': identified,
        'recorded': sum(len(rows) for rows in prepared.values()),
        'excluded': excluded,
        'completed_stages': completed,
        'metadata': metadata,
    }
