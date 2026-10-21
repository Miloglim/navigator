"""Persistence of company identities; candidate discovery never overwrites decisions."""
import time
import logging
from services.company_mappings import mapping_form

def read(connection, business_id, kind, name):
    row = connection.execute('SELECT * FROM company_mappings WHERE business_user_id=? AND kind=? AND local_name=?',
                             (business_id, kind, name)).fetchone()
    return dict(row) if row else None

def candidate(connection, business_id, kind, name, result, country=''):
    connection.execute("INSERT INTO company_mappings(business_user_id,kind,local_name,company_id,company_code,company_name,country,status,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,'pending','auto',?) ON CONFLICT(business_user_id,kind,local_name) DO NOTHING",
                       (business_id,kind,name,result['id'],result.get('code') or result.get('match_text') or '',
                        result.get('enName') or result.get('name') or result.get('match_text') or '',country,int(time.time())))
    connection.commit()

def save(connection, business_id, body, master, actor):
    data = mapping_form(body, master)
    old = read(connection,business_id,data['kind'],data['local_name'])
    revision = body.get('revision')
    if old and revision != old['revision']:
        raise ValueError('映射已被更新，请刷新后再保存')
    values = tuple(data[k] for k in ('company_id','company_code','company_name','country','status','note'))
    if old:
        changed = connection.execute('UPDATE company_mappings SET company_id=?,company_code=?,company_name=?,country=?,status=?,note=?,updated_by=?,updated_at=?,revision=revision+1 WHERE business_user_id=? AND kind=? AND local_name=? AND revision=?',
                                     (*values,actor,int(time.time()),business_id,data['kind'],data['local_name'],revision))
        if changed.rowcount != 1:
            raise ValueError('映射已被更新，请刷新后再保存')
    else:
        connection.execute('INSERT INTO company_mappings(company_id,company_code,company_name,country,status,note,updated_by,updated_at,business_user_id,kind,local_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (*values,actor,int(time.time()),business_id,data['kind'],data['local_name']))
    connection.commit()
    logging.getLogger('company-mappings').info('[company-mappings] saved kind=%s local=%s status=%s actor=%s',
                                              data['kind'],data['local_name'],data['status'],actor)
    return read(connection,business_id,data['kind'],data['local_name'])
