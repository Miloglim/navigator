"""Production IO boundary: DWS, immutable evidence, ledger HTTP and cursor locks."""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from freight_flow.core.contracts import (PRODUCTION_BATCH_STAGES, HISTORY_BATCH_STAGES, PRODUCTION_MESSAGE_STAGES,
    DWS_TIMEOUT_SECONDS, DWS_PAGE_LIMIT, READBACK_PAGE_SIZE, NAME_RESOLVE_BATCH_LIMIT)
from freight_flow.services.production import parse_pull, source_for_message, records_for_ledger, merge_products
from freight_flow.services.production import partial_records, classify_quoted_followup

LOG = logging.getLogger('production')


def hidden_subprocess_kwargs():
    """Prevent console children from allocating visible windows on Windows."""
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def filesystem_path(path):
    """Use the Windows extended prefix for evidence files that may exceed MAX_PATH."""
    # Path.resolve() itself touches the filesystem and can fail before the prefix is added.
    value = os.path.abspath(str(path))
    if os.name == "nt" and len(value) >= 248 and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def read_file_bytes(path):
    with open(filesystem_path(path), "rb") as stream:
        return stream.read()


def unique_image_files(paths):
    """Collapse DWS basename aliases only when their bytes are identical."""
    by_digest = {}
    for path in sorted(map(Path, paths), key=lambda item: str(item)):
        digest = hashlib.sha256(read_file_bytes(path)).hexdigest()
        by_digest.setdefault(digest, path)
    if len(by_digest) > 1:
        raise RuntimeError('下载目录存在多个内容不同的图片，禁止猜测')
    return list(by_digest.values())


class Production:
    def __init__(self, project, runtime, store, agent, dws='dws', ledger=None):
        self.project, self.runtime, self.store, self.agent = Path(project).resolve(), Path(runtime).resolve(), store, agent
        self.dws = str(dws)
        self.execution = threading.local()
        scripts = self.project / 'Skill/freight-rate-heartbeat/scripts'
        if str(scripts) not in sys.path:
            sys.path.append(str(scripts))
        import ledger_client
        import paths
        self.ledger = ledger or ledger_client
        self.paths = paths
        self.wm_path = Path(paths.WATERMARK_F)
        self.history_path = self.runtime / "history-backfill.json"
        self._history_lock = threading.Lock()
        # Use the ledger's own pure identity contract; do not create a second fingerprint definition.
        database = str(self.project / 'Database')
        if database not in sys.path:
            sys.path.append(database)
        from services.submission_coverage import fingerprint
        self.fingerprint = fingerprint

    def groups(self, shard, only=None):
        groups = json.loads(Path(self.paths.CHAT_SOURCES_F).read_text(encoding='utf-8'))['groups']
        selected = [g for g in groups if g.get('shard') == shard and (not only or g['slug'] == only)]
        if not selected:
            raise ValueError('无匹配分片/群')
        return selected

    def all_groups(self):
        return json.loads(Path(self.paths.CHAT_SOURCES_F).read_text(encoding='utf-8'))['groups']

    def enqueue(self, shard, trigger_key, only=None, message_id=None, priority=0):
        runs = []
        for group in self.groups(shard, only):
            source = {'group': group, 'shard': shard}
            if message_id:
                source['message_id'] = str(message_id)
            runs.append(self.store.enqueue(trigger_key + ':' + group['slug'], 'group:' + group['slug'],
                                           source, PRODUCTION_BATCH_STAGES, mode='production-batch', priority=priority))
        return runs

    def enqueue_history(self, trigger_key, slug, start, end, batch_size=3):
        group = next((item for item in self.all_groups() if item["slug"] == slug), None)
        if group is None:
            raise ValueError("历史回补群不在登记清单")
        source = {"group": group, "history_window": {"start": start, "end": end,
                  "batch_size": max(1, int(batch_size))}}
        return self.store.enqueue(trigger_key, "history:" + slug, source,
                                  HISTORY_BATCH_STAGES, mode="history-batch")

    def _knowledge(self, group):
        import spec_index
        spec = spec_index.spec_for(group['slug'])
        if not spec['exists'] and not spec.get('base_path'):
            raise ValueError('群规范未定位: ' + group['slug'])
        import knowledge_gate
        files = [self.project / 'Database/docs/agent-input-contract.md', *map(Path, knowledge_gate.GLOBAL_FILES)]
        files += [Path(spec[k]) for k in ('base_path', 'path', 'knowledge_path') if spec.get(k)]
        return '\n\n'.join('文件: ' + str(p) + '\n' + p.read_text(encoding='utf-8-sig') for p in files)

    def _dws(self, args, cwd):
        result = subprocess.run([self.dws, *args, '--format', 'json'], cwd=cwd,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=DWS_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs())
        if result.returncode:
            # Map recognized failures to fixed safe messages; never emit raw CLI diagnostics.
            try:
                error = json.loads(result.stderr).get('error') or {}
                message = str(error.get('message') or '')
                if 'resolve access token' in message or 'auth login' in message:
                    self._record_dws_health(False, 'authorization_required',
                                            'DWS 登录态失效，需要原账号重新授权；任务与水位保留')
                    raise RuntimeError('DWS 登录态失效，需要原账号重新授权；任务与水位保留')
            except (ValueError, AttributeError):
                pass  # A non-JSON CLI failure is reported by exit code only.
            self._record_dws_health(False, 'command_failed', 'DWS 命令失败 exit=' + str(result.returncode))
            raise RuntimeError('DWS 命令失败 exit=' + str(result.returncode))
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self._record_dws_health(False, 'invalid_response', 'DWS 未返回有效JSON')
            raise RuntimeError('DWS 未返回有效JSON') from exc
        self._record_dws_health(True, 'available', '')
        return payload

    def _record_dws_health(self, ok, status, reason):
        """Keep only a fixed operational state; never persist CLI diagnostics or credentials."""
        runtime = getattr(self, 'runtime', None)
        if runtime is None:
            return
        try:
            atomic_json(runtime / 'dws-health.json', {
                'ok': ok, 'status': status, 'reason': reason, 'at': time.time(),
            })
        except OSError:
            LOG.exception('[DWS] 健康状态落盘失败；原业务结果不因诊断写入失败而改写')

    def _legacy_guard(self, group):
        wm = json.loads(self.wm_path.read_text(encoding='utf-8')) if self.wm_path.exists() else {}
        current = wm.get(group['slug'], '')
        for state in Path(self.paths.WORK).glob('run_*/state.json'):
            data = json.loads(state.read_text(encoding='utf-8-sig'))
            targets = data.get('pull_targets') or {}
            pending = (data.get('pending_watermark') or {}).get(group['slug'])
            active_range = (pending and pending > current) or (not pending and targets.get(group['slug'], '') >= current)
            if data.get('stage') not in ('DONE', None) and group['slug'] in targets and active_range:
                raise RuntimeError('旧采集仍未完成，避免抢水位: ' + str(state))

    def execute(self, task, results):
        self.execution.task = task
        if task.get('mode', 'replay') == 'replay':
            return self.agent.execute(task, results)
        source = json.loads(task['input'])
        stage = task['stage']
        folder = self.runtime / 'artifacts' / task['run_id']
        folder.mkdir(parents=True, exist_ok=True)
        if task['mode'] in ('production-batch', 'history-batch'):
            group = source['group']
            if stage == 'pull':
                self._legacy_guard(group)
                checkpoint = folder / 'pull.json'
                if checkpoint.exists():
                    saved = json.loads(checkpoint.read_text(encoding='utf-8'))
                    parse_pull(saved['envelope'])
                    return saved
                wm = json.loads(self.wm_path.read_text(encoding='utf-8')) if self.wm_path.exists() else {}
                history = source.get('history_window') if task['mode'] == 'history-batch' else None
                since = history['start'] if history else (wm.get(group['slug']) or datetime.now().strftime('%Y-%m-%d 00:00:00'))
                envelope = self._dws(['chat', 'message', 'list', '--group', group['cid'], '--time', since,
                    '--direction', 'newer', '--page-all', '--page-limit', str(DWS_PAGE_LIMIT)], folder)
                try:
                    messages = parse_pull(envelope)
                except ValueError as exc:
                    atomic_json(folder / 'incomplete-pull.json', envelope)
                    raise RuntimeError(str(exc)) from exc
                index = Path(self.paths.WORK) / 'committed_message_ids.json'
                committed = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {}
                previous = committed.get('groups', committed).get(group['slug'], {})
                ids = set(previous.get('ids', previous.get('message_ids', [])) if isinstance(previous, dict) else previous)
                # Boundary messages may be returned again; skip both the committed index and any
                # already-persisted child run so a canary compares against the real oldest gap.
                unprocessed = []
                for item in messages:
                    message_id = item.get('openMessageId') or item.get('messageId') or item.get('id')
                    if message_id in ids:
                        continue
                    if self.store.trigger('source:' + group['cid'] + ':' + message_id):
                        continue
                    unprocessed.append(item)
                messages = unprocessed
                if history:
                    messages = [item for item in messages if str(item.get('createTime') or '') <= history['end']]
                    cursor = self.history_state(group['slug'], history['start'], history['end'])
                    processed = set(cursor.get('processed_ids') or [])
                    messages = [item for item in messages
                                if (item.get('openMessageId') or item.get('messageId') or item.get('id')) not in processed]
                messages.sort(key=lambda item: (str(item.get('createTime') or ''),
                                                str(item.get('openMessageId') or item.get('messageId') or item.get('id') or '')))
                target_message = source.get('message_id')
                if target_message:
                    target_index = next((index for index, item in enumerate(messages)
                                         if (item.get('openMessageId') or item.get('messageId') or item.get('id')) == target_message), None)
                    if target_index != 0:
                        raise ValueError('canary 消息必须是该群当前最旧未处理消息，避免水位越过缺口')
                    messages = [messages[0]]
                elif history:
                    messages = messages[:max(1, int(history.get('batch_size') or 1))]
                saved = {'before': since, 'expected_before': None if history else wm.get(group['slug']),
                         'envelope': envelope, 'messages': messages,
                         'after': max([since, *(m['createTime'] for m in messages)])}
                atomic_json(checkpoint, saved)
                return saved
            if stage == 'fanout':
                knowledge = self._knowledge(group)
                children = []
                for message in results['pull']['messages']:
                    item = source_for_message(group, message, knowledge)
                    stages = [s for s in PRODUCTION_MESSAGE_STAGES if s != 'vision' or item.get('parse_media_ids')]
                    child = self.store.enqueue('source:' + group['cid'] + ':' + item['message_id'],
                        'message:' + group['slug'] + ':' + item['message_id'], item,
                        stages, mode='production-message')
                    self.store.depend(task['run_id'], child)
                    children.append(child)
                return {'children': children, 'messages': len(children)}
            if stage == 'reconcile':
                children = self.store.children(task['run_id'])
                expected = len(results['pull']['messages'])
                closed_states = ('DONE', 'PARTIAL_REVIEW_COMPLETE')
                if len(children) != expected or any(c['status'] not in closed_states for c in children):
                    raise RuntimeError('逐消息交付未完成，不推进水位')
                receipts = []
                for child in children:
                    output = self.store.results(child['id'])
                    noise = child.get('noise_resolution')
                    if noise:
                        receipts.append({'run_id': child['id'], 'noise_reason': noise['reason']})
                    elif output.get('classify', {}).get('decision') == 'followup':
                        receipts.append({'run_id': child['id'], 'followup_reference': output['classify'],
                                         'review_issue_acknowledged': True})
                    elif output.get('classify', {}).get('decision') == 'noise':
                        receipts.append({'run_id': child['id'], 'noise_reason': output['classify']['reason']})
                    elif output.get('readback', {}).get('ok'):
                        receipts.append({'run_id': child['id'], **output['readback']})
                    elif any(isinstance(value, dict) and value.get('ok')
                             and value.get('status') == 'PARTIAL_REVIEW_COMPLETE'
                             for value in output.values()):
                        receipt = next(value for value in output.values()
                                       if isinstance(value, dict) and value.get('ok')
                                       and value.get('status') == 'PARTIAL_REVIEW_COMPLETE')
                        receipts.append({'run_id': child['id'], **receipt})
                    else:
                        raise RuntimeError('消息缺送审读回证明')
                receipt = {'ok': True, 'messages': expected, 'receipts': receipts}
                atomic_json(folder / 'delivery.json', receipt)
                return receipt
            if stage == 'history_cursor':
                history = source['history_window']
                ids = [m.get('openMessageId') or m.get('messageId') or m.get('id')
                       for m in results['pull']['messages']]
                return self.commit_history(group['slug'], history, ids)
            if stage == 'watermark':
                if not results['reconcile']['ok']:
                    raise ValueError('缺少逐消息对账')
                self.commit_cursor(group['slug'], results['pull'])
                return {'before': results['pull']['before'], 'after': results['pull']['after'], 'ok': True}
            raise ValueError('未知生产阶段: ' + stage)
        if stage == 'prepare':
            return self.prepare(source, folder)
        prepared = results['prepare']
        # Existing MSK weekly-card rule applies only in its approved group scope.
        import re
        prepared = {**prepared, 'week_as_validity': prepared['group'].get('slug') == 'turkey_price'
                    and re.search(r'\bMSK\b', prepared['text'], re.I) is not None}
        task = {**task, 'input': json.dumps(prepared, ensure_ascii=False)}
        if stage in ('classify', 'vision', 'extract', 'inventory', 'coverage', 'audit'):
            if stage == 'coverage':
                return self.compare_source_cells(prepared, results)
            if stage == 'vision' and not prepared.get('image_paths'):
                result = {'facts': [], 'summary': '引用图片只展示和校验哈希，本条没有可解析图片'}
            else:
                result = self.agent.execute(task, results)
            if stage == 'classify':
                result = classify_quoted_followup(prepared, result)
                if result.get('decision') == 'followup':
                    response = self.ledger.report_review_issues([{
                        'source_group': prepared['group']['slug'],
                        'message_id': prepared['message_id'],
                        'message_text': prepared['text'],
                        'media_ids': prepared.get('media_ids') or [],
                        'reasons': [
                            '引用跟进待关联：本条只补充或修订被引用报价，不独立生成产品',
                            '被引用消息ID：' + str(prepared.get('quoted_message_id') or ''),
                            '本条补充事实：' + str(prepared.get('own_text') or ''),
                            '处理方式：请回到被引用消息的对应待审条目录入或修改，不能从引用内容重复建价',
                        ],
                    }])
                    if not response.get('ok'):
                        raise RuntimeError('引用跟进待核回执未确认')
                    result = {**result, 'review_issue_acknowledged': True}
            return result
        if stage == 'normalize':
            candidates = results.get('coverage', {}).get('records', results['extract']['records'])
            if not candidates:
                raise ValueError('报价/不确定消息未提取任何条目，不能作为完成')
            return records_for_ledger(prepared, candidates)
        if stage == 'validate':
            validated = {}
            for kind, records in results['normalize'].items():
                if not records:
                    continue
                validated[kind] = []
                for batch in self.ledger.chunks(records):
                    response = self.ledger.validate(kind, batch)
                    if not response.get('ok') or len(response.get('results') or []) != len(batch):
                        raise RuntimeError('预校验无完整回执')
                    validated[kind] += [{**r, **(v.get('resolved') or {})} for r, v in zip(batch, response['results'])]
            return {'records': validated}
        if stage == 'submit':
            return self.submit(prepared, results['validate']['records'], folder)
        if stage == 'readback':
            return self.readback(prepared, results['validate']['records'])
        raise ValueError('未知消息阶段: ' + stage)

    def compare_source_cells(self, source, results):
        from freight_flow.services.workflow import coverage
        from freight_flow.services.production import normalize_candidate_dates
        from datetime import date
        anchor = date.fromisoformat(source['message_time'][:10])
        options = {'source_text': source.get('text', ''), 'week_as_validity': source.get('week_as_validity') is True}
        records = [normalize_candidate_dates(r, anchor, **options) for r in results['extract']['records']]
        inventory = [normalize_candidate_dates(r, anchor, **options) for r in results['inventory']['cells']]
        items, references, identities = [], [], {}
        for rows, pod_key in ((records, '目的港'), (inventory, 'pod')):
            for row in rows:
                for key, entity in (('起运港', 'pol'), (pod_key, 'pod')):
                    if row.get(key):
                        identity = (entity, row[key])
                        if identity not in identities:
                            identities[identity] = len(items)
                            items.append({'type': entity, 'value': row[key]})
                        references.append((row, key, identities[identity]))
        if items:
            matches = []
            for start in range(0, len(items), NAME_RESOLVE_BATCH_LIMIT):
                batch = items[start:start + NAME_RESOLVE_BATCH_LIMIT]
                response = self.ledger.resolve_names(source['group']['name'], batch)
                resolved = response.get('data') or []
                if not response.get('ok') or len(resolved) != len(batch):
                    resolved = []
                    for item in batch:
                        single = self.ledger.resolve_names(source['group']['name'], [item])
                        values = single.get('data') or []
                        if single.get('ok') and len(values) == 1:
                            resolved.append(values[0])
                        else:
                            resolved.append({'status': 'unresolved', 'canonical': item['value'],
                                             'error': single.get('error') or 'invalid or unresolved name'})
                matches.extend(resolved)
            for row, key, index in references:
                match = matches[index]
                if match.get('status') == 'resolved':
                    row[key] = match['canonical']
        records = merge_products(records)
        return coverage(records, inventory)

    def media_source(self, source, folder):
        """Rebuild ownership from the immutable pull receipt for pre-fix queued sources."""
        task = getattr(self.execution, 'task', None)
        if not task or not source.get('media_ids'):
            return source
        with self.store.connect() as db:
            parents = [r[0] for r in db.execute('SELECT parent FROM dependencies WHERE child=?', (task['run_id'],))]
        for parent in parents:
            pull = self.store.results(parent).get('pull') or {}
            for message in pull.get('messages') or []:
                mid = message.get('openMessageId') or message.get('messageId') or message.get('id')
                if mid != source['message_id']:
                    continue
                verified = source_for_message(source['group'], message, source['group_knowledge'])
                if (verified['text'] != source['text'] or verified['message_time'] != source['message_time']
                        or set(verified['display_media_ids']) != set(source.get('display_media_ids') or source['media_ids'])):
                    raise ValueError('原始拉取回执与排队来源不一致，禁止改写来源')
                ownership_fields = ('display_media_ids', 'parse_media_ids', 'quoted_media_ids',
                                    'media_messages', 'media_roles')
                corrected = {**source, **{field: verified[field] for field in ownership_fields}}
                changed = {field: {'before': source.get(field), 'verified': verified[field]}
                           for field in ownership_fields if source.get(field) != verified[field]}
                if changed:
                    atomic_json(folder / 'media-owner-recovery.json', {
                        'parent_run_id': parent, 'business_message_id': mid,
                        'changes': changed})
                    LOG.info('图片归属与解析范围按原始拉取元数据恢复 run=%s', task['run_id'])
                return corrected
        return source

    def materialize_image(self, source, folder, target, media):
        # Never interpret a resource ID as a path.
        if '/' in media or '\\' in media or media in ('.', '..'):
            raise ValueError('图片资源ID无法安全对应本地文件')
        existing = unique_image_files([p for p in target.glob(media + '*')
                                        if os.path.isfile(filesystem_path(p))
                                        and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')])
        image_folder = folder / 'images' / hashlib.sha256(media.encode()).hexdigest()
        image_folder.mkdir(parents=True, exist_ok=True)
        if len(existing) == 1:
            image = image_folder / ("artifact-" + hashlib.sha256(media.encode()).hexdigest()[:16] + existing[0].suffix.lower())
            shutil.copyfile(filesystem_path(existing[0]), filesystem_path(image))
        else:
            downloaded = list(image_folder.glob('*'))
            if not downloaded:
                self._dws(['chat', '+messages-resource-download', '--type', 'mediaId', '--resource-id', media,
                    '--message-id', source.get('media_messages', {}).get(media, source['message_id']), '--open-conversation-id', source['group']['cid'],
                    '--output', './'], image_folder)
            downloaded = unique_image_files([p for p in image_folder.rglob('*')
                                              if os.path.isfile(filesystem_path(p))
                                              and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')])
            if len(downloaded) != 1:
                raise RuntimeError('原图下载未取得唯一完整图片: ' + media)
            downloaded_image = downloaded[0]
            image = image_folder / ("artifact-" + hashlib.sha256(media.encode()).hexdigest()[:16] + downloaded_image.suffix.lower())
            if downloaded_image != image:
                shutil.copyfile(filesystem_path(downloaded_image), filesystem_path(image))
            canonical = target / (media + image.suffix.lower())
            target.mkdir(parents=True, exist_ok=True)
            if canonical.exists() and read_file_bytes(canonical) != read_file_bytes(image):
                raise ValueError('现有图片内容冲突，禁止覆盖')
            if not canonical.exists():
                shutil.copyfile(filesystem_path(image), filesystem_path(canonical))
        digest = hashlib.sha256(read_file_bytes(image)).hexdigest()
        return image, digest, media + image.suffix.lower()

    def prepare(self, source, folder):
        source = self.media_source(source, folder)
        display_media = source.get('display_media_ids') or source.get('media_ids') or []
        configured_parse = source.get('parse_media_ids')
        parse_media = set(display_media if configured_parse is None else configured_parse)
        prepared = {**source, 'image_paths': [], 'image_hashes': {}, 'image_files': [],
                    'display_image_paths': [], 'display_image_hashes': {},
                    'display_image_failures': {}}
        target = Path(self.ledger.images_dir())
        for media in display_media:
            try:
                image, digest, filename = self.materialize_image(source, folder, target, media)
            except Exception as error:  # noqa: BLE001 - quoted images are display-only
                if media in parse_media:
                    raise
                prepared['display_image_failures'][media] = str(error)
                LOG.warning('引用图片不可展示但继续解析本条 run=%s media=%s reason=%s',
                            getattr(getattr(self, 'execution', None), 'task', {}).get('run_id', ''), media, error)
                continue
            path = str(image.resolve())
            prepared['display_image_paths'].append(path)
            prepared['display_image_hashes'][path] = digest
            prepared['image_files'].append(filename)
            if media in parse_media:
                prepared['image_paths'].append(path)
                prepared['image_hashes'][path] = digest
        atomic_json(folder / 'source.json', prepared)
        return prepared

    def commit_cursor(self, slug, pull):
        self.write_guard()
        env = dict(os.environ, FRT_RUNTIME_DIR=str(self.wm_path.parent), PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, str(self.project / 'Skill/freight-rate-heartbeat/scripts/watermark.py'),
            'set', '--map', json.dumps({slug: pull['after']}), '--expect',
            json.dumps({slug: pull['expected_before']})], env=env, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=DWS_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs())
        if result.returncode:
            raise RuntimeError('水位提交失败或被其他采集并发推进；保留对账回执')

    def history_state(self, slug, start, end):
        if not self.history_path.exists():
            return {'processed_ids': [], 'updated_at': ''}
        data = json.loads(self.history_path.read_text(encoding='utf-8'))
        window = (data.get('windows') or {}).get(start + '|' + end, {})
        return (window.get('groups') or {}).get(slug, {'processed_ids': [], 'updated_at': ''})

    def commit_history(self, slug, history, message_ids):
        self.write_guard()
        key = history['start'] + '|' + history['end']
        with self._history_lock:
            data = (json.loads(self.history_path.read_text(encoding='utf-8'))
                    if self.history_path.exists() else {'version': 1, 'windows': {}})
            window = data.setdefault('windows', {}).setdefault(key, {'groups': {}})
            current = window.setdefault('groups', {}).setdefault(
                slug, {'processed_ids': [], 'updated_at': ''})
            before = len(current.get('processed_ids') or [])
            processed = list(dict.fromkeys([*(current.get('processed_ids') or []), *message_ids]))
            current['processed_ids'] = processed
            current['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            atomic_json(self.history_path, data)
        return {'ok': True, 'window': key, 'messages': len(message_ids),
                'processed_before': before, 'processed_after': len(processed)}

    def pending_rows(self, source):
        rows, offset, total = [], 0, None
        while total is None or offset < total:
            result = self.ledger._request('GET', '/api/pending?' + urlencode({
                'id_query': source['message_id'], 'status': 'all', 'limit': READBACK_PAGE_SIZE, 'offset': offset}))
            if not result.get('ok'):
                raise RuntimeError('待审核读回失败')
            current_total = int(result.get('total') or 0)
            if total is not None and total != current_total:
                raise RuntimeError('待审核分页期间条数变化')
            total = current_total
            page = result.get('rows') or []
            if offset < total and not page:
                raise RuntimeError('待审核分页缺页')
            rows += page
            offset += len(page)
        return [r for r in rows if r['payload'].get('消息ID') == source['message_id']
                and r['payload'].get('来源群') == source['group']['name']]

    def covered(self, source, records):
        actual = {}
        for row in self.pending_rows(source):
            if row['payload'].get('消息原文') != source['text']:
                continue
            payload = {**row['payload'], **(row.get('resolved') or {})}
            actual[self.fingerprint(row['kind'], payload)] = row['id']
        expected = {self.fingerprint(kind, r) for kind, items in records.items() for r in items}
        return expected, actual

    def submit(self, source, records, folder):
        expected, actual = self.covered(source, records)
        receipts = []
        for kind, items in records.items():
            missing = [r for r in items if self.fingerprint(kind, r) not in actual]
            for batch in self.ledger.chunks(missing):
                self.write_guard()
                response = self.ledger.submit_missing(kind, batch)
                if not response.get('ok') or not (response.get('coverage') or {}).get('ok'):
                    # No blind immediate retry. Next lease first reads the ledger to resolve uncertain delivery.
                    raise RuntimeError('送审回执未证明完整交付；下一次先读回再补缺')
                receipts.append(response)
        result = {'expected': len(expected), 'receipts': receipts, 'already_covered': len(expected & actual.keys())}
        atomic_json(folder / 'submission.json', result)
        return result

    def write_guard(self):
        task = getattr(self.execution, 'task', None)
        if task is not None and not self.store.active(task):
            raise RuntimeError('执行租约已失效，拒绝新的送审或水位写入')

    def readback(self, source, records, settle_issue=True):
        expected, actual = self.covered(source, records)
        missing = expected - actual.keys()
        match_mode = 'exact'
        if missing:
            rows = self.pending_rows(source)
            partial_rows = [row for row in rows
                            if row['payload'].get('partial_review') is True
                            or row['payload'].get('message_partial_review') is True]
            expected_partial = [row for kind, items in records.items() for row in items
                                if row.get('partial_review') is True or row.get('message_partial_review') is True]
            if partial_rows and expected_partial:
                def key(kind, payload):
                    remark = str(payload.get('备注') or '')
                    source_row = ''
                    for marker in ('来源行=',):
                        if marker in remark:
                            source_row = remark.rsplit(marker, 1)[1].split(';')[0].split('；')[0]
                    return (kind, str(payload.get('海运费USD') if payload.get('海运费USD') not in (None, '') else payload.get('价格USD') or ''),
                            str(payload.get('柜型') or ''), source_row, str(payload.get('船期ETD') or ''),
                            str(payload.get('有效期起') or ''), str(payload.get('有效期止') or ''),
                            str(payload.get('船名航次') or ''), str(payload.get('航线服务') or ''),
                            str(payload.get('目的码头') or ''), str(payload.get('箱量') or ''))
                expected_counts = Counter(key(kind, row) for kind, items in records.items() for row in items)
                actual_counts = Counter(key(row['kind'], {**row['payload'], **(row.get('resolved') or {})})
                                        for row in rows)
                if expected_counts != actual_counts:
                    raise RuntimeError('送审读回缺 ' + str(len(missing)) + ' 个真实产品，部分计数核对仍不一致，保留水位')
                covered_ids = sorted(row['id'] for row in rows)
                match_mode = 'partial_product_counts'
            else:
                raise RuntimeError('送审读回缺 ' + str(len(missing)) + ' 个真实产品，保留水位')
        else:
            covered_ids = sorted(actual[k] for k in expected)
        result = {'ok': True, 'expected': len(expected), 'covered_ids': covered_ids,
                  'match_mode': match_mode}
        if settle_issue:
            self.settle_reported_issue(source, records, result['covered_ids'])
        return result

    def settle_reported_issue(self, source, records, covered_ids):
        """Only close our own acknowledged report after complete source readback."""
        task = getattr(self.execution, 'task', None)
        if not task:
            return
        receipt = self.runtime / 'artifacts' / task['run_id'] / 'review-issue.json'
        if not receipt.is_file():
            return  # Historical reports without an acknowledged local receipt stay open.
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        if (saved.get('source_group') != source['group']['slug']
                or saved.get('message_id') != source['message_id']
                or saved.get('message_text') != source['text']):
            raise RuntimeError('待核结案来源与原报告不一致，保留待核问题')
        rows = [row for row in self.pending_rows(source)
                if row.get('review_status') in ('awaiting', 'approved')
                and row['payload'].get('消息原文') == source['text']]
        expected = {self.fingerprint(kind, rec) for kind, items in records.items() for rec in items}
        actual = {self.fingerprint(row['kind'], {**row['payload'], **(row.get('resolved') or {})})
                  for row in rows}
        if (not covered_ids or {row['id'] for row in rows} != set(covered_ids)
                or len(rows) != len(covered_ids) or actual != expected):
            raise RuntimeError('待核结案未证明完整原文交付，保留待核问题')
        self.write_guard()
        try:
            response = self.ledger.settle_review_issue(source['group']['slug'], source['message_id'], covered_ids)
        except Exception as exc:
            raise RuntimeError('待核结案请求失败，保留原回执等待重试') from exc
        data = response.get('data') or response
        if not response.get('ok') or data.get('resolved') is not True:
            raise RuntimeError('待核结案回执未确认成功，保留原回执等待重试')
        atomic_json(receipt.with_name('review-issue-settlement.json'), {
            'source_group': source['group']['slug'], 'message_id': source['message_id'],
            'covered_ids': covered_ids, 'response': response})

    def report_failure(self, task, reason, results=None):
        if task.get('mode') != 'production-message':
            return False
        source = json.loads(task['input'])
        results = results or {}
        extracted = (results.get('extract') or {}).get('records') or []
        inventoried = (results.get('inventory') or {}).get('cells') or []
        classified = results.get('classify') or {}
        vision_facts = (results.get('vision') or {}).get('facts') or []
        completed = [stage for stage, value in results.items() if value is not None]
        reasons = [
            '技术失败待核：卡在' + str(task.get('stage') or ''),
            '已完成阶段：' + ('/'.join(completed) or '无'),
            '识别产品：%d；独立盘点格：%d；已安全送审：0；未形成可读回产品：%d' % (
                len(extracted), len(inventoried), len(extracted)),
            '阶段事实：' + json.dumps({
                'classification': classified.get('decision') or '',
                'classified_fact_count': len(classified.get('facts') or []),
                'vision_fact_count': len(vision_facts),
            }, ensure_ascii=False, sort_keys=True),
            '失败原因：' + str(reason),
            '需人工确认：模型阶段连续技术失败，未形成完整结构化交付；请结合原文和图片确认是否可重试、分拆或人工补录。',
        ]
        response = self.ledger.report_review_issues([{'source_group': source['group']['slug'],
            'message_id': source['message_id'], 'message_text': source['text'],
            'media_ids': source.get('media_ids') or [], 'reasons': reasons}])
        if not response.get('ok'):
            LOG.error('[%s] 待核问题保存失败；任务和水位仍保留', task['run_id'])
            return False
        else:
            atomic_json(self.runtime / 'artifacts' / task['run_id'] / 'review-issue.json', {
                'source_group': source['group']['slug'], 'message_id': source['message_id'],
                'message_text': source['text'], 'reasons': reasons, 'response': response})
            return True

    def deliver_partial(self, task, results, reason):
        """Persist every usable product and a visible review issue before ending the child."""
        if task.get('mode') != 'production-message':
            raise ValueError('部分交付只适用于单消息轮次')
        source = results.get('prepare') or json.loads(task['input'])
        failed_stage = task['stage']
        completed_stages = [stage for stage, value in results.items()
                            if stage not in (failed_stage, 'partial_delivery') and value is not None]
        candidates = results.get('normalize')
        if not isinstance(candidates, dict):
            candidates = (results.get('coverage') or {}).get('records')
        if candidates is None:
            candidates = (results.get('extract') or {}).get('records') or []
        partial = partial_records(source, candidates, failed_stage, reason, completed_stages)
        records = partial['records']
        self.resolve_partial_names(source, records)
        validation, collapsed_source_values = self.canonicalize_partial_records(records)
        for rows in records.values():
            for record in rows:
                passed = validation.get(id(record)) is True
                record['partial_review'] = not passed
                record['partial_product_status'] = 'complete' if passed else 'incomplete'
        folder = self.runtime / 'artifacts' / task['run_id']
        submission = None
        readback = None
        if any(records.values()):
            submission = self.submit(source, records, folder)
            readback = self.readback(source, records, settle_issue=False)

        record_counts = {kind: len(rows or []) for kind, rows in records.items()}
        facts = {
            'classification': (results.get('classify') or {}).get('decision') or '',
            'classified_facts': (results.get('classify') or {}).get('facts') or [],
            'vision_facts': (results.get('vision') or {}).get('facts') or [],
            'extract_candidates': len((results.get('extract') or {}).get('records') or []),
            'inventory_cells': len((results.get('inventory') or {}).get('cells') or []),
            'coverage': {key: (results.get('coverage') or {}).get(key)
                         for key in ('priced_cells', 'ignored_inventory_cells', 'ignored_candidate_records')},
        }
        issue_reasons = [
            '部分解析待核：卡在' + failed_stage,
            '已完成阶段：' + ('/'.join(completed_stages) or '无'),
            '识别产品：%d；已写待审并读回：%d；产品分布：%s' % (
                partial['identified'], sum(record_counts.values()),
                json.dumps(record_counts, ensure_ascii=False, sort_keys=True)),
            '失败原因：' + str(reason),
            '识别事实：' + json.dumps(facts, ensure_ascii=False, sort_keys=True),
        ]
        if partial['excluded']:
            issue_reasons.append('被排除候选：' + json.dumps(
                partial['excluded'], ensure_ascii=False, sort_keys=True))
        if partial['identified'] != sum(record_counts.values()):
            issue_reasons.append('台账权威归一后有候选成为同一可见身份，已合并待审行；原始值：'
                                 + json.dumps(collapsed_source_values, ensure_ascii=False) + '；需人工拆分或确认。')
        if not any(records.values()):
            issue_reasons.append('当前没有可安全送审的产品；原文和图片证据保留，需人工确认缺失或冲突事实。')
        response = self.ledger.report_review_issues([{
            'source_group': source['group']['slug'],
            'message_id': source['message_id'],
            'message_text': source['text'],
            'media_ids': source.get('media_ids') or [],
            'reasons': issue_reasons,
        }])
        if not response.get('ok'):
            raise RuntimeError('部分交付的待核回执未确认，保留任务等待重试')
        receipt = {
            'ok': True,
            'status': 'PARTIAL_REVIEW_COMPLETE',
            'failed_stage': failed_stage,
            'failure_reason': str(reason),
            'completed_stages': completed_stages,
            'identified': partial['identified'],
            'records': record_counts,
            'excluded': partial['excluded'],
            'facts': facts,
            'submission': submission,
            'readback': readback,
            'pending_ids': (readback or {}).get('covered_ids') or [],
            'review_issue_acknowledged': True,
        }
        atomic_json(folder / 'review-issue.json', {
            'source_group': source['group']['slug'], 'message_id': source['message_id'],
            'message_text': source['text'], 'partial': True, 'response': response})
        atomic_json(folder / 'partial-delivery.json', receipt)
        return receipt

    def resolve_partial_names(self, source, records):
        """Use the ledger's authoritative resolver before partial fingerprinting."""
        items, references, identities = [], [], {}
        for kind, rows in records.items():
            pod_key = '目的港'
            for row in rows:
                for key, entity in (('起运港', 'pol'), (pod_key, 'pod')):
                    if not row.get(key):
                        continue
                    identity = (entity, str(row[key]))
                    if identity not in identities:
                        identities[identity] = len(items)
                        items.append({'type': entity, 'value': row[key]})
                    references.append((row, key, identities[identity]))
        if not items:
            return
        matches = []
        for start in range(0, len(items), NAME_RESOLVE_BATCH_LIMIT):
            batch = items[start:start + NAME_RESOLVE_BATCH_LIMIT]
            response = self.ledger.resolve_names(source['group']['name'], batch)
            resolved = response.get('data') or []
            if not response.get('ok') or len(resolved) != len(batch):
                LOG.warning('部分交付名称解析回执不完整，保留原值并由台账预检归一: group=%s batch=%s returned=%s',
                            source['group']['name'], len(batch), len(resolved))
                resolved = [{'status': 'unresolved', 'canonical': item['value']} for item in batch]
            matches.extend(resolved)
        for row, key, index in references:
            match = matches[index]
            if match.get('status') == 'resolved':
                row[key] = match['canonical']

    def canonicalize_partial_records(self, records):
        """Freeze the ledger's resolved values even when mechanical checks fail."""
        validation_by_record = {}
        markers_by_record = {}
        collapsed_source_values = set()
        for kind, rows in records.items():
            deduplicated = []
            by_fingerprint = {}
            for batch in self.ledger.chunks(rows):
                response = self.ledger.validate(kind, batch)
                results = response.get('results') or []
                if not response.get('ok') or len(results) != len(batch):
                    raise RuntimeError('部分交付前的台账字段归一无完整回执')
                for record, validation in zip(batch, results):
                    # Keep invalid source values visible in the note, then mirror the ledger's
                    # canonical projection. Candidates that become the same product are collapsed
                    # with their raw evidence retained instead of being resubmitted indistinguishably.
                    source_markers = []
                    canonical = dict(validation.get('resolved') or {})
                    for field in (validation.get('name_resolution') or {}).get('fields') or []:
                        matches = field.get('matches') or []
                        if matches and all(item.get('status') != 'resolved' for item in matches):
                            canonical[field.get('field')] = ''
                    for key, value in canonical.items():
                        original = str(record.get(key) or '').strip()
                        if not value and original:
                            marker = '原始%s=%s' % (key, original)
                            source_markers.append(marker)
                            note = str(record.get('备注') or '').strip()
                            if marker not in note:
                                record['备注'] = ';'.join(filter(None, (note, marker)))
                        record[key] = value
                    passed = validation.get('ok') is True
                    fingerprint = self.fingerprint(kind, record)
                    existing = by_fingerprint.get(fingerprint)
                    if existing is None:
                        by_fingerprint[fingerprint] = record
                        deduplicated.append(record)
                        validation_by_record[id(record)] = passed
                        markers_by_record[id(record)] = tuple(source_markers)
                        continue
                    validation_by_record[id(existing)] = validation_by_record[id(existing)] and passed
                    collapsed_source_values.update(markers_by_record.get(id(existing), ()))
                    collapsed_source_values.update(source_markers)
                    note = str(existing.get('备注') or '').strip()
                    for marker in source_markers:
                        if marker not in note:
                            note = ';'.join(filter(None, (note, marker)))
                    existing['备注'] = note
                    markers_by_record[id(existing)] = tuple(dict.fromkeys(
                        [*markers_by_record.get(id(existing), ()), *source_markers]))
            records[kind] = deduplicated
        return validation_by_record, sorted(collapsed_source_values)
