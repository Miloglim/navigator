"""Shared IO boundary for roster and immutable DWS evidence.

Both dashboard and collector call this module; services stay IO-free.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from freight_identity.core.contracts import (ROSTER_SCHEMA_VERSION, ROSTER_ACTIONS,
    ROSTER_LOCK_TIMEOUT_SECONDS, ROSTER_LOCK_RETRY_SECONDS, BUSINESS_TIMEZONE_HOURS,
    RAW_PULL_ARCHIVE_FIELD, MESSAGE_EVIDENCE_LIMIT, SHADOW_MODE, PUBLISHER_RULE_VERSION, MIN_COVERED_WORKDAYS)
from freight_identity.services.publisher import assess_group, identity_key, covered_days_from_intervals
from freight_identity.services.collection import collection_decision

BUSINESS_ZONE = timezone(timedelta(hours=BUSINESS_TIMEZONE_HOURS))
CONFIG_DIR = Path(__file__).resolve().parents[1] / 'config'
ROSTER_PATH = CONFIG_DIR / 'publisher_roster.json'
GROUPS_PATH = CONFIG_DIR / 'chat_sources.json'
EXCLUSION_AUDIT_NOTE = '管理员直接排除'
CONFIRMATION_AUDIT_NOTE = '管理员直接确认'
_EVIDENCE_CACHE_LOCK = threading.Lock()
_EVIDENCE_CACHE = {'key': None, 'value': None}


def now():
    return datetime.now(BUSINESS_ZONE).isoformat(timespec='seconds')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def load_roster(path=ROSTER_PATH):
    data = read_json(path)  # Missing/corrupt roster is an error, never an empty allowlist.
    if not isinstance(data, dict) or not isinstance(data.get('members'), list):
        raise ValueError('航管账号名单格式错误')
    if data.get('version') not in ('1.0.0', '1.1.0', '1.2.0', ROSTER_SCHEMA_VERSION):
        raise ValueError('航管账号名单版本不支持')
    revision = data.get('revision', 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError('航管账号名单修订号错误')
    for item in data['members']:
        name_exclusion = (item.get('decision') == 'excluded' and item.get('scope') == 'group'
                          and bool(str(item.get('name') or '').strip())) if isinstance(item, dict) else False
        if (not isinstance(item, dict) or not item.get('group_cid')
                or not (item.get('sender_open_id') or item.get('sender_id') or name_exclusion)
                or item.get('decision', 'publisher') not in ('publisher', 'excluded')):
            raise ValueError('航管账号名单条目错误')
    exclusions = data.get('exclusions', [])
    if not isinstance(exclusions, list):
        raise ValueError('全局排除名单格式错误')
    for item in exclusions:
        if (not isinstance(item, dict) or not (item.get('sender_open_id') or item.get('sender_id'))
                or item.get('decision', 'excluded') != 'excluded'):
            raise ValueError('全局排除名单条目错误')
    if not isinstance(data.get('audit', []), list):
        raise ValueError('航管账号变更历史格式错误')
    return normalize_roster(data)


def person_keys(item):
    keys = set()
    if item.get('sender_open_id'):
        keys.add('open:' + str(item['sender_open_id']))
    if item.get('sender_id'):
        keys.add('sender:' + str(item['sender_id']))
    return keys


def normalize_roster(data):
    """Upgrade group-scoped exclusions in memory without changing business revision."""
    roster = deepcopy(data)
    publishers, raw_exclusions = [], list(roster.get('exclusions', []))
    for item in roster['members']:
        if item.get('decision', 'publisher') == 'excluded' and person_keys(item):
            raw_exclusions.append(item)
        else:
            publishers.append(item)
    merged = []
    for raw in raw_exclusions:
        keys = person_keys(raw)
        current = next((item for item in merged if keys & person_keys(item)), None)
        groups = set(raw.get('source_groups') or [])
        if raw.get('group'):
            groups.add(str(raw['group']))
        if current is None:
            current = {'name': str(raw.get('name') or ''),
                       'sender_id': str(raw.get('sender_id') or ''),
                       'sender_open_id': str(raw.get('sender_open_id') or ''),
                       'source': 'user', 'scope': 'global', 'decision': 'excluded',
                       'excluded_on': str(raw.get('excluded_on') or raw.get('confirmed_on') or ''),
                       'note': str(raw.get('note') or EXCLUSION_AUDIT_NOTE),
                       'source_groups': sorted(groups)}
            merged.append(current)
        else:
            current['sender_id'] = current.get('sender_id') or str(raw.get('sender_id') or '')
            current['sender_open_id'] = current.get('sender_open_id') or str(raw.get('sender_open_id') or '')
            current['source_groups'] = sorted(set(current.get('source_groups') or []) | groups)
    roster.update(version=ROSTER_SCHEMA_VERSION, members=publishers, exclusions=merged)
    return roster


@contextmanager
def roster_lock(path):
    lock_path = Path(str(path) + '.lock')
    with open(lock_path, 'a+b') as stream:
        deadline = time.monotonic() + ROSTER_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('航管账号名单正在更新，请刷新重试')
                time.sleep(ROSTER_LOCK_RETRY_SECONDS)
        try:
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'0')
                stream.flush()
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def save_decision(request, candidate, actor, actor_id, path=ROSTER_PATH):
    expected = request.get('expected_revision')
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return {'ok': False, 'error': '名单修订号无效，请刷新后重试', 'code': 'invalid_revision'}
    action = request.get('action')
    if action not in ROSTER_ACTIONS:
        return {'ok': False, 'error': '不支持的账号操作', 'code': 'invalid_action'}
    key = str(candidate.get('identity_key') or '')
    stable_identity = ((key.startswith('open:') and bool(candidate.get('sender_open_id')))
                       or (key.startswith('sender:') and bool(candidate.get('sender_id'))))
    name_identity = (key.startswith('name:') and bool(str(candidate.get('name') or '').strip())
                     and bool(str(candidate.get('cid') or '').strip()))
    if candidate.get('identity_conflict') or (action == 'confirm' and not stable_identity) \
            or (action in ('exclude', 'reset') and not (stable_identity or name_identity)):
        return {'ok': False, 'error': '需要无冲突的群聊账号ID，不能仅凭姓名操作', 'code': 'identity_required'}
    note = str(request.get('note') or '').strip()
    if not note:
        note = CONFIRMATION_AUDIT_NOTE if action == 'confirm' else EXCLUSION_AUDIT_NOTE if action == 'exclude' else ''
    if not note:
        return {'ok': False, 'error': '请填写确认或变更依据', 'code': 'reason_required'}
    cid = str(candidate.get('cid') or '')
    if action in ('confirm', 'exclude') and not cid:
        return {'ok': False, 'error': '缺少来源群，不能执行账号决定', 'code': 'group_required'}
    with roster_lock(path):
        roster = load_roster(path)
        revision = roster.get('revision', 0)
        if request.get('expected_revision') != revision:
            return {'ok': False, 'error': '名单已更新，请刷新后重试', 'code': 'revision_conflict'}
        candidate_keys = person_keys(candidate)
        def same_person(item):
            return bool(candidate_keys & person_keys(item))
        def same_group_person(item):
            if item.get('group_cid') != cid:
                return False
            if stable_identity:
                return same_person(item)
            return (not person_keys(item) and str(item.get('name') or '').strip()
                    == str(candidate.get('name') or '').strip())
        group_before = [m for m in roster['members'] if same_group_person(m)]
        publisher_before = [m for m in roster['members'] if same_person(m)]
        exclusion_before = [m for m in roster['exclusions'] if same_person(m)]
        before = (exclusion_before + publisher_before) if action == 'exclude' and stable_identity else group_before
        unchanged = ((action == 'exclude' and stable_identity and len(exclusion_before) == 1 and not publisher_before)
                     or (action == 'exclude' and name_identity and len(group_before) == 1
                         and group_before[0].get('decision') == 'excluded')
                     or (action == 'confirm' and not exclusion_before and len(group_before) == 1)
                     or (action == 'reset' and not exclusion_before and not group_before))
        if unchanged:
            return {'ok': True, 'data': {'revision': revision, 'action': action, 'unchanged': True}}
        after = None
        if action == 'exclude' and stable_identity:
            source_groups = {str(candidate.get('group') or '')}
            for item in publisher_before + exclusion_before:
                source_groups.update(item.get('source_groups') or [])
                if item.get('group'):
                    source_groups.add(str(item['group']))
            source_groups.discard('')
            roster['members'] = [m for m in roster['members'] if not same_person(m)]
            roster['exclusions'] = [m for m in roster['exclusions'] if not same_person(m)]
            after = {'name': candidate['name'], 'sender_id': candidate.get('sender_id', ''),
                     'sender_open_id': candidate.get('sender_open_id', ''), 'source': 'user',
                     'scope': 'global', 'decision': 'excluded', 'excluded_on': now(),
                     'source_groups': sorted(source_groups), 'note': note}
            roster['exclusions'].append(after)
        elif action == 'exclude':
            roster['members'] = [m for m in roster['members'] if not same_group_person(m)]
            after = {'group_cid': cid, 'group': candidate['group'], 'name': candidate['name'],
                     'sender_id': '', 'sender_open_id': '', 'source': 'user',
                     'scope': 'group', 'decision': 'excluded', 'excluded_on': now(), 'note': note}
            roster['members'].append(after)
        elif action == 'confirm':
            roster['exclusions'] = [m for m in roster['exclusions'] if not same_person(m)]
            roster['members'] = [m for m in roster['members'] if not same_group_person(m)]
            after = {'group_cid': cid, 'group': candidate['group'], 'name': candidate['name'],
                     'sender_id': candidate.get('sender_id', ''),
                     'sender_open_id': candidate.get('sender_open_id', ''),
                     'source': 'user', 'decision': 'publisher',
                     'confirmed_on': now(), 'note': note}
            roster['members'].append(after)
        elif exclusion_before:
            roster['exclusions'] = [m for m in roster['exclusions'] if not same_person(m)]
        else:
            roster['members'] = [m for m in roster['members'] if not same_group_person(m)]
        roster.update(version=ROSTER_SCHEMA_VERSION, revision=revision + 1, updated_at=now())
        roster.setdefault('audit', []).append({'at': now(), 'actor': actor, 'actor_id': actor_id,
            'group_cid': cid, 'group': candidate.get('group') or '全部来源群', 'identity_key': key, 'name': candidate['name'],
            'action': action, 'note': note, 'before': before, 'after': after, 'revision': revision + 1})
        atomic_json(path, roster)
    return {'ok': True, 'data': {'revision': revision + 1, 'action': action}}


def normalize_message(m):
    return {'message_id': str(m.get('openMessageId') or m.get('id') or ''),
            'sender': str(m.get('sender') or m.get('s') or ''),
            'sender_id': str(m.get('senderId') or ''),
            'sender_open_id': str(m.get('senderOpenDingTalkId') or ''),
            'time': str(m.get('createTime') or m.get('t') or ''),
            'content': str(m.get('content') or m.get('c') or '')}


def decode_messages(text):
    # DWS may emit several JSON envelopes (one per page). Non-JSON legacy output
    # is reported as unusable evidence; it is never counted as an empty pull.
    decoder, offset, messages, found = json.JSONDecoder(), 0, [], False
    while offset < len(text):
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        if offset >= len(text):
            break
        obj, offset = decoder.raw_decode(text, offset)
        found = True
        if isinstance(obj, list):
            messages.extend(m for m in obj if isinstance(m, dict))
        elif isinstance(obj, dict) and isinstance(obj.get('messages'), list):
            messages.extend(obj['messages'])
        elif isinstance(obj, dict) and (obj.get('openMessageId') or obj.get('id')):
            messages.append(obj)
        else:
            raise ValueError('消息证据不是DWS消息列表')
    if not found:
        raise ValueError('消息证据为空')
    return messages


def evidence_files(work):
    work = Path(work)
    paths = (list(work.glob('run_*/raw/pull_*.json'))
             + list(work.glob('run_*/raw_archive/pull_*.json'))
             + list(work.glob('msgs_*/*.json'))
             + list(work.glob('run_*/pull_receipt_*.json')))
    return sorted(set(paths), key=lambda path: str(path).lower())


def evidence_cache_key(work, groups, as_of):
    fingerprints = []
    for path in evidence_files(work):
        try:
            stat = path.stat()
            fingerprints.append((str(path.relative_to(work)), stat.st_mtime_ns, stat.st_size))
        except OSError:
            fingerprints.append((str(path), -1, -1))
    group_key = tuple(sorted((str(group['cid']), str(group['slug'])) for group in groups))
    return str(Path(work).resolve()), group_key, str(as_of)[:10], tuple(fingerprints)


def read_evidence(work, groups, as_of):
    work = Path(work)
    cache_key = evidence_cache_key(work, groups, as_of)
    with _EVIDENCE_CACHE_LOCK:
        if _EVIDENCE_CACHE['key'] == cache_key:
            return deepcopy(_EVIDENCE_CACHE['value'])
    by_slug = {g['slug']: g for g in groups}
    messages, conflicts, warnings, intervals = {}, set(), [], {g['cid']: [] for g in groups}
    files = list(work.glob('run_*/raw/pull_*.json')) + list(work.glob('run_*/raw_archive/pull_*.json')) + list(work.glob('msgs_*/*.json'))
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        group = by_slug.get(path.stem.split('__', 1)[0].removeprefix('pull_'))
        if not group:
            continue
        try:
            values = decode_messages(path.read_text(encoding='utf-8-sig'))
            for raw in values:
                message = normalize_message(raw)
                if not message['message_id']:
                    continue
                key = (group['cid'], message['message_id'].removeprefix('msg//').rstrip('='))
                old = messages.get(key)
                if old:
                    for field in ('sender_id', 'sender_open_id'):
                        if old[field] and message[field] and old[field] != message[field]:
                            conflicts.add(key)
                        if not message[field]:
                            message[field] = old[field]
                    for field in ('sender', 'content'):
                        if '\ufffd' in message[field] and '\ufffd' not in old[field]:
                            message[field] = old[field]
                message.update(cid=group['cid'], slug=group['slug'])
                messages[key] = message
        except (OSError, ValueError, TypeError) as error:
            warnings.append({'source': str(path.relative_to(work)), 'reason': str(error)})
    for path in work.glob('run_*/pull_receipt_*.json'):
        try:
            receipt = read_json(path)
            for slug, fact in receipt.get('groups', {}).items():
                if slug not in by_slug or fact.get('status') != 'ok' or not fact.get('raw_sha256'):
                    continue
                raw_path = path.parent / fact.get(RAW_PULL_ARCHIVE_FIELD, 'raw/pull_' + slug + '.json')
                if not raw_path.resolve().is_relative_to(path.parent.resolve()):
                    raise ValueError('拉取归档路径越界')
                if not raw_path.exists():
                    continue
                text = raw_path.read_text(encoding='utf-8-sig')
                if not fact.get(RAW_PULL_ARCHIVE_FIELD):
                    text = text.strip()
                if hashlib.sha256(text.encode()).hexdigest() != fact['raw_sha256']:
                    continue
                try:
                    raw = json.loads(text)
                except ValueError:
                    continue  # Legacy flat/empty output cannot prove coverage.
                if (not isinstance(raw, dict) or not isinstance(raw.get('messages'), list)
                        or raw.get('complete') is not True or raw.get('paginationKnown') is not True
                        or raw.get('hasMore') is not False or raw.get('success') is False
                        or raw.get('count') != len(raw['messages'])
                        or any(raw.get(k) for k in ('partial', 'truncated', 'truncatedByPageLimit',
                            'truncatedByResultLimit', 'failedCount', 'decryptFailedCount', 'failures'))):
                    continue
                intervals[by_slug[slug]['cid']].append((fact['watermark_before'], receipt['created_at']))
        except (OSError, ValueError, TypeError, KeyError) as error:
            warnings.append({'source': str(path.relative_to(work)), 'reason': str(error)})
    coverage = {}
    for cid, pairs in intervals.items():
        result = covered_days_from_intervals(pairs, as_of=as_of)
        if not result['ok']:
            raise ValueError(result['error'])
        coverage[cid] = result['data']['days']
    result = messages, conflicts, coverage, warnings
    with _EVIDENCE_CACHE_LOCK:
        _EVIDENCE_CACHE.update(key=cache_key, value=deepcopy(result))
    return result


def snapshot(work, groups=None, roster_path=ROSTER_PATH, as_of=None):
    stamp = as_of or now()
    groups = groups if groups is not None else read_json(GROUPS_PATH)['groups']
    roster = load_roster(roster_path)
    messages, conflicts, coverage, warnings = read_evidence(work, groups, stamp)
    candidates, decisions = [], {}
    for group in groups:
        samples = [m for m in messages.values() if m['cid'] == group['cid']]
        # Explicitly managed accounts remain visible without recent evidence.
        # No synthetic messages are added to statistical counts.
        managed = [m for m in roster['members'] if m['group_cid'] == group['cid']]
        decisions_source = roster['members'] + roster['exclusions']
        response = assess_group(samples, as_of=stamp, group_id=group['cid'],
                                covered_workdays=coverage[group['cid']], confirmations=decisions_source)
        if not response['ok']:
            raise ValueError(response['error'])
        assessments = {a['identity_key']: a for a in response['data']['members']}
        aliases = response['data']['aliases']
        member_keys = set(assessments)
        for item in managed:
            key = ('open:' + item['sender_open_id'] if item.get('sender_open_id')
                   else 'sender:' + item['sender_id'] if item.get('sender_id') else 'name:' + item['name'])
            member_keys.add(key)
            if key not in assessments:
                name_exclusion = key.startswith('name:') and item.get('decision') == 'excluded'
                assessments[key] = {'identity_key': key, 'identity_ready': not name_exclusion,
                    'status': item.get('decision', 'publisher'),
                    'basis': ('user_excluded_name' if name_exclusion else
                              'user_excluded_account' if item.get('decision') == 'excluded'
                              else 'user_confirmed_account'),
                    'reasons': [('管理员已按完整姓名排除当前来源群，当前观察窗口无消息'
                                 if name_exclusion else '管理员已登记该群账号，当前观察窗口无消息')],
                    'message_count': 0, 'historical_message_count': 0, 'mention_all_count': 0, 'covered_workdays': len(coverage[group['cid']]),
                    'daily_counts': [], 'activity_ratio': None, 'observed_volume_ratio': None}
        for key in member_keys:
            assessment = assessments[key]
            own = sorted([m for m in samples if aliases.get(identity_key(m), identity_key(m)) == key],
                         key=lambda m: m['time'], reverse=True)
            # Filter samples to the same observation window used by the service.
            from freight_identity.services.publisher import message_day
            from freight_identity.core.contracts import LOOKBACK_DAYS
            end = message_day(stamp)
            own = [m for m in own if message_day(m['time']) and end - timedelta(days=LOOKBACK_DAYS) <= message_day(m['time']) <= end]
            matching = next((m for m in managed + roster['exclusions'] if (key == 'open:' + str(m.get('sender_open_id'))
                            or key == 'sender:' + str(m.get('sender_id'))
                            or (key.startswith('name:') and m.get('group_cid') == group['cid']
                                and key == 'name:' + str(m.get('name') or '')))), {})
            first = own[0] if own else {}
            identified = next((m for m in own if identity_key(m) == key), {})
            candidate = {'candidate_id': hashlib.sha256((group['cid'] + '\0' + key).encode()).hexdigest(),
                'group': group['name'], 'slug': group['slug'], 'cid': group['cid'], 'identity_key': key,
                'name': first.get('sender') or matching.get('name') or key.removeprefix('name:'),
                'names': sorted({m['sender'] for m in own}),
                'sender_id': identified.get('sender_id') or matching.get('sender_id', ''),
                'sender_open_id': identified.get('sender_open_id') or matching.get('sender_open_id', ''),
                'last': first.get('time', ''), 'assessment': assessment,
                'identity_conflict': any((group['cid'], m['message_id'].removeprefix('msg//').rstrip('=')) in conflicts for m in own),
                'samples': own[:MESSAGE_EVIDENCE_LIMIT]}
            if candidate['identity_conflict']:
                assessment.update(status='insufficient', basis='identity_conflict', identity_ready=False,
                                  reasons=['同一消息的账号证据冲突，需人工核查'])
            candidates.append(candidate)
            account_message = identified or {'sender_open_id': candidate['sender_open_id'],
                'sender_id': candidate['sender_id'], 'sender': candidate['name']}
            decisions[(group['slug'], key)] = collection_decision(
                account_message, assessment, roster.get('revision', 0))['data']
            for old_key, linked_key in aliases.items():
                if linked_key == key:
                    historical = next((m for m in own if identity_key(m) == old_key), {})
                    decisions[(group['slug'], old_key)] = collection_decision(
                        historical, dict(assessment, identity_ready=False), roster.get('revision', 0))['data']
    candidates.sort(key=lambda c: (c['group'], c['name'], c['identity_key']))
    collection_runs = []
    for run in sorted(Path(work).glob('run_*')):
        files = sorted(run.glob('identity_decisions_r*.json'))
        if not files:
            continue
        try:
            fact = max(((read_json(p)['data'], p.name) for p in files), key=lambda pair: pair[0]['generated_at'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            warnings.append({'source': run.name, 'reason': str(error)})
            continue
        data, filename = fact
        collection_runs.append({'run': run.name, 'file': filename, 'generated_at': data['generated_at'],
            'roster_revision': data['roster_revision'], 'counts': data['counts'],
            'current': data['roster_revision'] == roster.get('revision', 0)})
    collection_runs.sort(key=lambda r: r['generated_at'], reverse=True)
    exclusions = []
    exclusion_items = roster['exclusions'] + [item for item in roster['members']
                                               if item.get('decision') == 'excluded']
    for item in exclusion_items:
        group_scope = item.get('scope') == 'group'
        exclusion_key = ('open:' + item['sender_open_id'] if item.get('sender_open_id')
                         else 'sender:' + item['sender_id'] if item.get('sender_id')
                         else 'name:' + str(item.get('name') or ''))
        observed_groups = ({str(item.get('group') or '')} if group_scope else
                           {candidate['group'] for candidate in candidates
                            if person_keys(candidate) & person_keys(item)})
        observed_groups.discard('')
        observed_groups.update(item.get('source_groups') or [])
        candidate_match = next((candidate for candidate in candidates
                                if group_scope and candidate['cid'] == item.get('group_cid')
                                and candidate['identity_key'] == exclusion_key), None)
        candidate_id = (candidate_match['candidate_id'] if candidate_match else
                        hashlib.sha256(('global-exclusion\0' + exclusion_key).encode()).hexdigest())
        exclusions.append({'candidate_id': candidate_id,
            'identity_key': exclusion_key, 'name': item.get('name') or exclusion_key,
            'names': [item.get('name')] if item.get('name') else [],
            'cid': item.get('group_cid', '') if group_scope else '',
            'group': item.get('group', '') if group_scope else '全部来源群',
            'slug': '', 'sender_id': item.get('sender_id', ''),
            'sender_open_id': item.get('sender_open_id', ''),
            'scope': 'group' if group_scope else 'global',
            'excluded_on': item.get('excluded_on', ''), 'note': item.get('note', ''),
            'observed_groups': sorted(observed_groups)})
    exclusions.sort(key=lambda item: (item['name'], item['identity_key']))
    return {'ok': True, 'data': {'rule_version': PUBLISHER_RULE_VERSION, 'generated_at': stamp,
        'roster_revision': roster.get('revision', 0), 'mode': SHADOW_MODE,
        'minimum_workdays': MIN_COVERED_WORKDAYS,
        'groups': [{'cid': g['cid'], 'name': g['name'], 'slug': g['slug'],
                    'covered_workdays': len(coverage[g['cid']])} for g in groups],
        'candidates': candidates, 'exclusions': exclusions, 'audit': list(reversed(roster.get('audit', []))),
        'warnings': warnings, 'collection_runs': collection_runs[:MESSAGE_EVIDENCE_LIMIT]}, '_decisions': decisions}


def decision_for(snapshot_result, slug, raw):
    message = normalize_message(raw)
    key = identity_key(message)
    return snapshot_result['_decisions'].get((slug, key)) or collection_decision(message, {}, snapshot_result['data']['roster_revision'])['data']
