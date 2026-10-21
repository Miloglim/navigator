"""Dashboard IO adapter to the collector's single publisher authority."""
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[2] / 'Skill/freight-rate-heartbeat/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
from freight_identity.runtime import snapshot, save_decision, load_roster, now


class PublisherPeople(dict):
    """Keep the existing name/group display contract while carrying authoritative account IDs."""
    def __init__(self, members):
        super().__init__()
        self.members=[m for m in members if m.get('decision','publisher')=='publisher']
        for member in self.members:
            name,group=str(member.get('name') or '').strip(),str(member.get('group') or '').strip()
            if name:
                groups=self.setdefault(name,[])
                if group and group not in groups:groups.append(group)


def confirmed_people():
    """Read the collector decision source; login credentials are never inferred."""
    return PublisherPeople(load_roster()['members'])


def publisher_members():
    """Return confirmed and excluded entries for startup reconciliation."""
    roster = load_roster()
    return roster['members'] + roster['exclusions']


def account_library_status(candidate, operators):
    from services.publisher_identity import person_keys
    keys=set(person_keys(candidate))
    if keys:
        exact=[row for row in operators if keys & set(row.get('dingtalk_identity_keys') or [])]
        if len(exact)!=1:
            return {'registered':False,'ambiguous':len(exact)>1}
        row=exact[0]
        return {'registered':True,'ambiguous':False,'user_id':row.get('user_id') or '',
                'login_name':row.get('name') or '', 'role':row.get('role') or '',
                'active':bool(row.get('active')),'has_password':bool(row.get('has_password')),
                'login_enabled':bool(row.get('login_enabled'))}
    names = {str(candidate.get('name') or '').strip(),
             *(str(value).strip() for value in candidate.get('names') or [])}
    names.discard('')
    exact = [row for row in operators if str(row.get('publisher_name') or '').strip() in names]
    if not exact:
        exact = [row for row in operators if str(row.get('name') or '').strip() in names]
    if len(exact) != 1:
        return {'registered': False, 'ambiguous': len(exact) > 1}
    row = exact[0]
    return {'registered': True, 'ambiguous': False, 'user_id': row.get('user_id') or '',
            'login_name': row.get('name') or '', 'role': row.get('role') or '',
            'active': bool(row.get('active')), 'has_password': bool(row.get('has_password')),
            'login_enabled': bool(row.get('login_enabled'))}


def accounts(work, operators=()):
    result = snapshot(work)
    result.pop('_decisions')
    rows = list(operators or [])
    for candidate in result.get('data', {}).get('candidates', []):
        candidate['account_library'] = account_library_status(candidate, rows)
    return result


def decide(work, request, operators=()):
    result = snapshot(work)
    candidate = next((c for c in result['data']['candidates']
                      if c['candidate_id'] == request.get('candidate_id')), None)
    if not candidate and request.get('action') == 'reset':
        candidate = next((c for c in result['data'].get('exclusions', [])
                          if c['candidate_id'] == request.get('candidate_id')), None)
    if not candidate:
        return {'ok': False, 'error': '账号证据已变化，请刷新列表', 'code': 'candidate_missing'}
    candidate['account_library'] = account_library_status(candidate, list(operators or []))
    saved = save_decision(request, candidate, request['_actor'], request['_actor_id'])
    if saved['ok']:
        from services.publisher_identity import person_keys
        saved['data']['identity_keys'] = person_keys(candidate)
        account = candidate.get('account_library') or {}
        if account.get('registered') and not account.get('ambiguous'):
            saved['data']['account_user_id'] = account.get('user_id') or ''
        print('[%s] [INFO] [publisher-accounts] actor=%s action=%s group=%s revision=%s' % (
            now(), request['_actor'], saved['data']['action'], candidate['group'], saved['data']['revision']), flush=True)
    return saved
