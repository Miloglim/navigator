"""Pure identity policy for this installation's DWS source; groups are provenance only."""
import json


def person_keys(record):
    """Both IDs observed on one message identify one person, independently of the group."""
    open_id = str(record.get('发送人OpenID') or record.get('source_sender_open_id') or record.get('sender_open_id') or '').strip()
    sender = str(record.get('发送人ID') or record.get('source_sender_id') or record.get('sender_id') or '').strip()
    return [kind+':'+value for kind,value in (('open',open_id),('sender',sender)) if value]


def roster_persons(members):
    """Combine explicit IDs from the roster, never names; one group may contain many people."""
    people = []
    for member in members:
        if member.get('decision','publisher') != 'publisher':
            continue
        keys = set(person_keys(member))
        if not keys:
            continue
        hits = [p for p in people if p['keys'] & keys]
        person = {'keys':keys,'names':{str(member.get('name') or '').strip()}}
        for hit in hits:
            person['keys'].update(hit['keys']);person['names'].update(hit['names']);people.remove(hit)
        person['names'].discard('')
        people.append(person)
    return people

def source_identity(record):
    group = str(record.get('来源群ID') or record.get('source_group_id') or '').strip()
    group_name = str(record.get('来源群') or record.get('source_group') or '').strip()
    name = str(record.get('发送人') or record.get('sender') or '').strip()
    sender = str(record.get('发送人ID') or record.get('source_sender_id') or '').strip()
    open_id = str(record.get('发送人OpenID') or record.get('source_sender_open_id') or '').strip()
    scope = ['group_id', group] if group else ['group_name', group_name]
    kind, value = ('sender_id', sender) if sender else ('open_id', open_id) if open_id else ('name_only', name)
    return json.dumps([*scope, kind, value], ensure_ascii=False, separators=(',', ':')), kind, name, group_name
