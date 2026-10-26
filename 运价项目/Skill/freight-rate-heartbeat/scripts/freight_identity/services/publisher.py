"""Pure, explainable publisher inference from message and collection evidence."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import re
from statistics import median, pstdev

from freight_identity.core.contracts import (
    PUBLISHER_RULE_VERSION, PUBLISHER, EXCLUDED, CANDIDATE, INSUFFICIENT,
    MENTION_ALL_MARKER, QUOTED_TEXT_SEPARATOR, LOOKBACK_DAYS,
    MIN_COVERED_WORKDAYS, MIN_ACTIVE_WORKDAY_RATIO,
    MAX_DAILY_COEFFICIENT_VARIATION, MIN_VOLUME_RATIO,
    MIN_WEEKDAY_MESSAGES, MIN_PEER_IDENTITIES, PEER_BASELINE_DIVISOR, WEEKEND_START,
    BUSINESS_TIMEZONE_HOURS, MESSAGE_EVIDENCE_LIMIT,
    PublisherMessage, PublisherAssessment, PublisherConfirmation,
)

BUSINESS_TIMEZONE = timezone(timedelta(hours=BUSINESS_TIMEZONE_HOURS))
MENTION_INSTRUCTION = re.compile(r'(?:请|麻烦|记得|不要|别|禁止|需要)\s*@所有人')


def message_day(value: str) -> date | None:
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        return stamp.astimezone(BUSINESS_TIMEZONE).date() if stamp.tzinfo else stamp.date()
    except (ValueError, TypeError):
        return None  # Missing/invalid dates cannot provide daily-frequency evidence.


def covered_days_from_intervals(intervals: list[tuple[str, str]], *, as_of: str) -> dict:
    """Derive complete weekdays from verified, overlapping collection intervals."""
    end = message_day(as_of)
    if not end:
        return {'ok': False, 'error': 'as_of must be an ISO business date'}
    normalized = []
    try:
        for first, last in intervals:
            stamps = []
            for value in (first, last):
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                stamps.append(stamp.replace(tzinfo=BUSINESS_TIMEZONE) if stamp.tzinfo is None
                              else stamp.astimezone(BUSINESS_TIMEZONE))
            if stamps[0] < stamps[1]:
                normalized.append(tuple(stamps))
    except (ValueError, TypeError, AttributeError):
        return {'ok': False, 'error': 'collection intervals must contain ISO timestamps'}
    merged = []
    for first, last in sorted(normalized):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    covered = set()
    for first, last in merged:
        current = max(first.date(), end - timedelta(days=LOOKBACK_DAYS))
        while current < min(last.date(), end):
            midnight = datetime.combine(current, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
            if first <= midnight and midnight + timedelta(days=1) <= last and current.weekday() < WEEKEND_START:
                covered.add(current.isoformat())
            current += timedelta(days=1)
    return {'ok': True, 'data': {'days': sorted(covered)}}


def direct_mention_all(body: str) -> bool:
    # Legacy composed originals may append quotation; it never grants authority.
    own_body = str(body or '').split(QUOTED_TEXT_SEPARATOR, 1)[0]
    for line in own_body.splitlines():
        if line.lstrip().startswith('>'):
            continue
        line = MENTION_INSTRUCTION.sub('', line)
        if MENTION_ALL_MARKER in line:
            return True
    return False


def identity_key(message: PublisherMessage) -> str:
    if message.get('sender_open_id'):
        return 'open:' + message['sender_open_id']
    if message.get('sender_id'):
        return 'sender:' + message['sender_id']
    return 'name:' + str(message.get('sender') or '')


def identity_aliases(messages: list[PublisherMessage], *, group_id: str = "",
                     confirmations: list[PublisherConfirmation] = ()) -> dict[str, str]:
    """Link exact same-name history to the sole account seen in this group.

    More than one distinct account key stays separate until identity is resolved.
    No message or saved roster entry is rewritten by this derived mapping.
    """
    by_name: dict[str, set[str]] = {}
    for message in messages:
        name = str(message.get('sender') or '')
        key = identity_key(message)
        if name and not key.startswith('name:'):
            by_name.setdefault(name, set()).add(key)
    for item in confirmations:
        if not group_id or item.get('group_cid') != group_id:
            continue
        name = str(item.get('name') or '')
        key = ('open:' + item['sender_open_id'] if item.get('sender_open_id')
               else 'sender:' + item['sender_id'] if item.get('sender_id') else '')
        if name and key:
            by_name.setdefault(name, set()).add(key)
    return {'name:' + name: next(iter(keys)) for name, keys in by_name.items() if len(keys) == 1}


def assess_group(messages: list[PublisherMessage], *, as_of: str,
                 covered_workdays: list[str] = (), group_id: str = "",
                 confirmations: list[PublisherConfirmation] = ()) -> dict:
    """Group-local identities; output is derived and never updates business state.

    Coverage must be supplied by a transport/ops reader with successful full-pull
    evidence. Unknown dates are null in the chart, never zero. Today is excluded
    from frequency/volume decisions because its collection is not yet complete.
    """
    end = message_day(as_of)
    if not end:
        return {'ok': False, 'error': 'as_of must be an ISO business date'}
    start = end - timedelta(days=LOOKBACK_DAYS)
    covered = {d for value in covered_workdays if (d := message_day(value))
               and start <= d < end and d.weekday() < WEEKEND_START}
    accepted, seen = [], set()
    for message in messages:
        day = message_day(message.get('time', ''))
        mid = str(message.get('message_id') or '')
        if not day or not start <= day <= end or not mid or mid in seen:
            continue
        seen.add(mid)
        accepted.append((message, day))
    aliases = identity_aliases([message for message, _ in accepted],
                               group_id=group_id, confirmations=confirmations)
    members = {}
    for message, day in accepted:
        key = identity_key(message)
        members.setdefault(aliases.get(key, key), []).append((message, day))
    results: list[PublisherAssessment] = []
    for key, samples in members.items():
        counts = Counter(day for _, day in samples if day < end and day.weekday() < WEEKEND_START)
        all_ids = [m['message_id'] for m, _ in samples if direct_mention_all(m.get('content', ''))]
        daily = [counts[d] for d in sorted(covered)]
        active = sum(n > 0 for n in daily)
        total = sum(daily)
        mean = total / len(daily) if daily else 0
        cv = pstdev(daily) / mean if mean else None
        ratio = active / len(daily) if daily else None
        frequency_ready = len(covered) >= MIN_COVERED_WORKDAYS
        stable = bool(frequency_ready and ratio is not None and cv is not None
                      and ratio >= MIN_ACTIVE_WORKDAY_RATIO
                      and cv <= MAX_DAILY_COEFFICIENT_VARIATION)
        results.append({
            'identity_key': key, 'status': INSUFFICIENT, 'basis': 'insufficient',
            'reasons': [], 'identity_ready': not key.startswith('name:'),
            'mention_all_count': len(all_ids),
            'mention_all_message_ids': all_ids[:MESSAGE_EVIDENCE_LIMIT],
            'message_count': len(samples), 'historical_message_count': sum(
                identity_key(m).startswith('name:') for m, _ in samples), 'weekday_messages': total,
            'observed_volume_ratio': None, 'observed_volume_gap': False,
            'sample_share': len(samples) / len(seen) if seen else 0,
            'volume_ratio': None, 'volume_gap': False,
            'covered_workdays': len(covered), 'observed_workdays': len(counts),
            'active_workdays': active, 'activity_ratio': ratio,
            'daily_cv': cv, 'frequency_ready': frequency_ready,
            'stable_frequency': stable,
            'daily_counts': [{'date': d.isoformat(), 'count': counts[d] if d in covered else None,
                              'observed': counts[d], 'covered': d in covered}
                             for d in (start + timedelta(days=i) for i in range(LOOKBACK_DAYS))
                             if d.weekday() < WEEKEND_START],
        })
    # Lower-half peer median supports several high-volume publishers in one group.
    # Unlinked names still do not count as independent peers for an account.
    names = {key: {str(m.get('sender') or '') for m, _ in samples}
             for key, samples in members.items()}
    for result in results:
        key = result['identity_key']
        peers = [r['weekday_messages'] for r in results if r['identity_key'] != key
                 and not (names[key] & names[r['identity_key']])]
        observed_peers = [r['message_count'] for r in results if r['identity_key'] != key
                          and not (names[key] & names[r['identity_key']])]
        if len(observed_peers) >= MIN_PEER_IDENTITIES:
            lower_size = (len(observed_peers) + 1) // PEER_BASELINE_DIVISOR
            baseline = max(median(sorted(observed_peers)[:lower_size]), 1)
            result['observed_volume_ratio'] = result['message_count'] / baseline
            result['observed_volume_gap'] = (result['message_count'] >= MIN_WEEKDAY_MESSAGES
                                            and result['observed_volume_ratio'] >= MIN_VOLUME_RATIO)
        if len(peers) >= MIN_PEER_IDENTITIES and covered:
            lower_half_size = (len(peers) + 1) // PEER_BASELINE_DIVISOR
            baseline = max(median(sorted(peers)[:lower_half_size]), 1)
            result['volume_ratio'] = result['weekday_messages'] / baseline
            result['volume_gap'] = (result['weekday_messages'] >= MIN_WEEKDAY_MESSAGES
                                   and result['volume_ratio'] >= MIN_VOLUME_RATIO)
        reasons = result['reasons']
        if result['historical_message_count'] and not key.startswith('name:'):
            reasons.append(f"同群同名历史消息 {result['historical_message_count']} 条已并入行为统计；原消息账号字段不变")
        confirmed = next((item for item in confirmations
                          if ((item.get('scope') == 'global' and item.get('decision') == EXCLUDED)
                              or (group_id and item.get('group_cid') == group_id))
                          and item.get('source') == 'user'
                          and ((item.get('sender_open_id') and key == 'open:' + item['sender_open_id'])
                               or (item.get('sender_id') and key == 'sender:' + item['sender_id']))), None)
        name_confirmed = next((item for item in confirmations
                               if group_id and item.get('group_cid') == group_id
                               and item.get('source') == 'user' and item.get('decision', 'publisher') == PUBLISHER and key.startswith('name:')
                               and item.get('name') and item['name'] in names[key]), None)
        name_excluded = next((item for item in confirmations
                              if group_id and item.get('group_cid') == group_id
                              and item.get('source') == 'user' and item.get('decision') == EXCLUDED
                              and item.get('scope') == 'group' and key.startswith('name:')
                              and item.get('name') and item['name'] in names[key]), None)
        if confirmed and confirmed.get('decision') == EXCLUDED:
            result.update(status=EXCLUDED, basis='user_excluded_account')
            reasons.append('管理员已全局排除该人员；所有来源群均跳过识别')
        elif confirmed:
            result.update(status=PUBLISHER, basis='user_confirmed_account')
            reasons.append('用户已确认该群航管，按DWS账号匹配；保留其他航管')
        elif name_excluded:
            result.update(status=EXCLUDED, basis='user_excluded_name', identity_ready=False)
            reasons.append('管理员已按完整姓名排除当前来源群；其他群不受影响')
        elif name_confirmed:
            result.update(status=PUBLISHER, basis='user_confirmed_name', identity_ready=False)
            reasons.append('用户已确认该群业务姓名；历史姓名记录未自动合并到账号')
        elif result['mention_all_count']:
            result.update(status=PUBLISHER, basis='direct_mention_all')
            reasons.append('本条正文含@所有人群发标记（正文代理证据，未计引用）')
        elif result['volume_gap'] and result['stable_frequency']:
            result.update(status=PUBLISHER, basis='volume_and_frequency')
            reasons.append('消息量明显高于普通成员且工作日活跃稳定')
        elif result['observed_volume_gap'] or result['volume_gap'] or result['stable_frequency']:
            result.update(status=CANDIDATE, basis='partial_behavior')
            reasons.append('采集样本中存在消息量差距或部分稳定特征，继续观察')
        else:
            reasons.append('尚未命中航管特征，不能据此认定非航管')
        if not result['frequency_ready']:
            reasons.append(f'完整工作日样本{len(covered)}/{MIN_COVERED_WORKDAYS}，频率依据不足')
        if not result['identity_ready']:
            reasons.append('仅姓名证据，需补发送人账号ID后才能用于账号过滤')
    return {'ok': True, 'data': {'rule_version': PUBLISHER_RULE_VERSION,
                                'as_of': end.isoformat(), 'members': results, 'aliases': aliases}}
