"""Hash-only authorization persistence through existing SQLite/PostgreSQL adapters."""
import hmac
import secrets
import time
from urllib.parse import urlencode
from core.contracts import ok
from core.prospector_contracts import CLIENT_ID, RANDOM_BYTES, ACCESS_TTL_SECONDS, CODE_TTL_SECONDS, REFRESH_TTL_SECONDS, PURGE_BATCH_SIZE
from services.prospector_auth import authorization_params, challenge, digest, public_user

class InvalidToken(PermissionError):
    """Expired, revoked, or no longer authorized device grant."""

def _account(connection, user_id, fingerprint=None):
    row = connection.execute('SELECT o.user_id,o.name,o.display_name,o.role,o.active,o.password_hash,COALESCE(s.must_change_password,0) AS must_change_password,COALESCE(s.revision,0) AS security_revision FROM operators o LEFT JOIN account_security s ON s.user_id=o.user_id WHERE o.user_id=?', (user_id,)).fetchone()
    if not row: raise InvalidToken('login required')
    row = dict(row)
    security = digest(str(row.pop('password_hash', '') or '') + ':' + str(row.pop('security_revision')))
    if not row['active'] or row['must_change_password'] or row['role'] not in ('admin','operator','sales') or (fingerprint and not hmac.compare_digest(security, fingerprint)):
        raise InvalidToken('account authorization unavailable')
    row['security_hash'] = security
    return row

def _purge(connection):
    now = int(time.time())
    for table, key, condition in (('prospector_codes','code_hash','consumed=1 OR expires_at<=?'), ('prospector_tokens','token_hash','revoked=1 OR expires_at<=?'), ('prospector_grants','grant_id','expires_at<=?')):
        connection.execute('DELETE FROM ' + table + ' WHERE ' + key + ' IN (SELECT ' + key + ' FROM ' + table + ' WHERE ' + condition + ' LIMIT ?)', (now,PURGE_BATCH_SIZE))


def authorize(connection, session, body):
    params = authorization_params(body)
    account = _account(connection, session['user_id'])
    _purge(connection)
    code = secrets.token_urlsafe(RANDOM_BYTES)
    connection.execute('INSERT INTO prospector_codes(code_hash,user_id,security_hash,redirect_uri,challenge,expires_at) VALUES(?,?,?,?,?,?)',
                       (digest(code), account['user_id'], account['security_hash'], params['redirect_uri'], params['challenge'], int(time.time()) + CODE_TTL_SECONDS))
    connection.commit()
    return ok({'redirect_url':params['redirect_uri'] + '?' + urlencode({'code':code,'state':params['state']})})

def _issue(connection, account, grant_id, new_grant=False):
    access = secrets.token_urlsafe(RANDOM_BYTES)
    refresh = secrets.token_urlsafe(RANDOM_BYTES)
    now = int(time.time())
    if new_grant:
        connection.execute('INSERT INTO prospector_grants(grant_id,expires_at) VALUES(?,?)',(grant_id,now+REFRESH_TTL_SECONDS))
    else:
        connection.execute('UPDATE prospector_grants SET expires_at=? WHERE grant_id=?',(now+REFRESH_TTL_SECONDS,grant_id))
    for kind, token, ttl in (('access', access, ACCESS_TTL_SECONDS), ('refresh', refresh, REFRESH_TTL_SECONDS)):
        connection.execute('INSERT INTO prospector_tokens(token_hash,grant_id,user_id,security_hash,kind,expires_at) VALUES(?,?,?,?,?,?)', (digest(token),grant_id,account['user_id'],account['security_hash'],kind,now+ttl))
    connection.commit()
    return ok({'access_token':access,'refresh_token':refresh,'token_type':'Bearer','expires_in':ACCESS_TTL_SECONDS,'user':public_user(account)})

def token(connection, body):
    if body.get('client_id') != CLIENT_ID: raise ValueError('invalid client_id')
    now = int(time.time())
    grant = body.get('grant_type')
    if grant == 'authorization_code':
        hashed = digest(body.get('code') or '')
        row = connection.execute('SELECT * FROM prospector_codes WHERE code_hash=?', (hashed,)).fetchone()
        if not row or row['consumed'] or row['expires_at'] <= now: raise ValueError('invalid authorization code')
        if body.get('redirect_uri') != row['redirect_uri'] or not hmac.compare_digest(challenge(body.get('code_verifier')), row['challenge']): raise ValueError('invalid authorization proof')
        account = _account(connection, row['user_id'], row['security_hash'])
        changed = connection.execute('UPDATE prospector_codes SET consumed=1 WHERE code_hash=? AND consumed=0 AND expires_at>?', (hashed,now))
        if changed.rowcount != 1: raise ValueError('invalid authorization code')
        return _issue(connection, account, secrets.token_urlsafe(RANDOM_BYTES), new_grant=True)
    if grant == 'refresh_token':
        hashed = digest(body.get('refresh_token') or '')
        row = connection.execute("SELECT * FROM prospector_tokens WHERE token_hash=? AND kind='refresh'", (hashed,)).fetchone()
        if not row or row['revoked'] or row['expires_at'] <= now: raise ValueError('invalid refresh token')
        account = _account(connection, row['user_id'], row['security_hash'])
        # Lock the device row across rotation, so concurrent logout cannot miss new tokens.
        device = connection.execute('UPDATE prospector_grants SET revoked=0 WHERE grant_id=? AND revoked=0 AND expires_at>?', (row['grant_id'],now))
        if device.rowcount != 1: raise ValueError('invalid refresh token')
        changed = connection.execute('UPDATE prospector_tokens SET revoked=1 WHERE token_hash=? AND revoked=0 AND expires_at>?', (hashed,now))
        if changed.rowcount != 1: raise ValueError('invalid refresh token')
        connection.execute('UPDATE prospector_tokens SET revoked=1 WHERE grant_id=?', (row['grant_id'],))
        return _issue(connection, account, row['grant_id'])
    raise ValueError('unsupported grant_type')

def authenticate(connection, raw):
    row = connection.execute("SELECT t.* FROM prospector_tokens t JOIN prospector_grants g ON g.grant_id=t.grant_id WHERE t.token_hash=? AND t.kind='access' AND t.revoked=0 AND g.revoked=0 AND t.expires_at>?", (digest(raw), int(time.time()))).fetchone()
    if not row: raise InvalidToken('login required')
    account = _account(connection, row['user_id'], row['security_hash'])
    account['grant_id'] = row['grant_id']
    return account

def logout(connection, account):
    connection.execute('UPDATE prospector_grants SET revoked=1 WHERE grant_id=?', (account['grant_id'],))
    connection.execute('UPDATE prospector_tokens SET revoked=1 WHERE grant_id=?', (account['grant_id'],))
    connection.commit()
    return ok({'revoked':True})
