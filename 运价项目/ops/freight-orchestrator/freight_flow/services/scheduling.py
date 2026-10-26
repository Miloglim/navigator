"""Pure group display projection. History is never converted to business success."""
ACTIVE_STATES = {'RUNNING', 'WAITING_CHILDREN', 'RETRY_WAIT', 'QUEUED'}


def run_state(row):
    return ('COALESCED' if row.get('coalesced') else 'NEEDS_REVIEW' if row.get('held') else
            'FAILED' if row['failed'] else 'RUNNING' if row['running'] else
            'RETRY_WAIT' if row['retry'] else 'DONE' if not row['unfinished'] else
            'BLOCKED' if row['child_failed'] else
            'WAITING_CHILDREN' if row['child_waiting'] else 'QUEUED')


def group_summary(rows):
    groups = {}
    for row in rows:
        group = groups.setdefault(row['lane'], {'lane': row['lane'], 'name': row['name'] or row['lane'],
            'active': None, 'latest': None, 'followup': 0, 'review_batches': 0, 'merged': 0,
            'failed_messages': row['failed_messages'], 'running_messages': row['running_messages']})
        state = run_state(row)
        item = {'id': row['id'], 'status': state, 'created': row['created'], 'stage': row['stage'],
                'error': row['error'], 'last_event_at': row['last_event_at']}
        if state != 'COALESCED':
            group['latest'] = item
        if row.get('running') or row.get('child_running'):
            group['active'] = item
        if state == 'COALESCED':
            group['merged'] += 1
        elif state in ('NEEDS_REVIEW', 'BLOCKED', 'FAILED'):
            group['review_batches'] += 1
        elif state in ACTIVE_STATES:
            if group['active'] is None:
                group['active'] = item
            else:
                group['followup'] += 1
    result = []
    for group in groups.values():
        current = group.pop('active') or group['latest']
        group.update({'run_id': current['id'], 'stage': current['stage'], 'error': current['error'],
                      'last_event_at': current['last_event_at'], 'created': current['created']})
        group['status'] = ('processing' if group['running_messages'] or current['status'] == 'RUNNING' else
                           'recovering' if current['status'] == 'RETRY_WAIT' else
                           'waiting' if current['status'] in ('QUEUED', 'WAITING_CHILDREN') else
                           'needs_review' if group['failed_messages'] or group['review_batches'] else 'idle')
        del group['latest']
        result.append(group)
    return sorted(result, key=lambda g: ({'processing': 0, 'recovering': 1, 'waiting': 2, 'needs_review': 3, 'idle': 4}[g['status']], g['name']))
