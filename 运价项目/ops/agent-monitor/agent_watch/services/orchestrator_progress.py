"""Derive production queue status from a complete snapshot; no IO."""
from agent_watch.core.contracts import OrchestratorQueue

QUEUE_STATES = ('DONE', 'BLOCKED', 'FAILED', 'QUEUED', 'RUNNING', 'RETRY_WAIT', 'WAITING_CHILDREN', 'NEEDS_REVIEW', 'COALESCED')


def summarize_queue(rows, last_progress_at) -> OrchestratorQueue:
    batches = dict.fromkeys(QUEUE_STATES, 0)
    messages = dict.fromkeys(QUEUE_STATES, 0)
    incomplete_lanes = set()
    lane_blocked = running_tasks = 0
    for row in rows:
        state = ('COALESCED' if row.get('coalesced') else 'NEEDS_REVIEW' if row.get('held') else 'FAILED' if row['failed'] else 'RUNNING' if row['running'] else
                 'RETRY_WAIT' if row['retry'] else 'DONE' if not row['unfinished'] else
                 'BLOCKED' if row['child_failed'] else
                 'WAITING_CHILDREN' if row['child_waiting'] else 'QUEUED')
        counts = batches if row['mode'] == 'production-batch' else messages
        counts[state] += 1
        running_tasks += row['running']
        if row['mode'] == 'production-batch' and state not in ('DONE', 'NEEDS_REVIEW', 'COALESCED') and row['lane'] in incomplete_lanes:
            lane_blocked += 1
        if state not in ('DONE', 'NEEDS_REVIEW', 'COALESCED') and not (row['mode'] == 'production-batch' and state == 'FAILED'):
            incomplete_lanes.add(row['lane'])
    return {'batches': batches, 'messages': messages, 'running_tasks': running_tasks,
            'lane_blocked_batches': lane_blocked, 'last_progress_at': last_progress_at}


def business_state(queue):
    counts = [queue['batches'], queue['messages']]
    if queue['running_tasks']:
        return 'running'
    if any(c['FAILED'] or c['BLOCKED'] or c.get('NEEDS_REVIEW') for c in counts) or queue['lane_blocked_batches']:
        return 'blocked'
    if any(sum(v for k, v in c.items() if k not in ('DONE', 'COALESCED')) for c in counts):
        return 'waiting'
    return 'idle'
