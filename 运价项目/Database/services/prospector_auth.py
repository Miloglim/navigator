"""Pure OAuth input validation and PKCE; no IO or account lookups."""
import base64
import hashlib
import ipaddress
import re
import uuid
from urllib.parse import urlsplit, unquote
from core.prospector_contracts import (CLIENT_ID, PKCE_MIN_LENGTH, PKCE_MAX_LENGTH,
                                      MAX_STATE_LENGTH, MIN_PORT, MAX_PORT, READ_ROUTES)

def digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()

def challenge(verifier):
    if not isinstance(verifier, str) or not PKCE_MIN_LENGTH <= len(verifier) <= PKCE_MAX_LENGTH or not re.fullmatch(r'[A-Za-z0-9._~-]+', verifier):
        raise ValueError('invalid code_verifier')
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')

def authorization_params(body):
    if body.get('client_id') != CLIENT_ID or body.get('response_type') != 'code' or body.get('code_challenge_method') != 'S256':
        raise ValueError('unsupported authorization request')
    redirect = str(body.get('redirect_uri') or '')
    parsed = urlsplit(redirect)
    try: port = parsed.port
    except ValueError as error: raise ValueError('invalid redirect_uri') from error
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or port is None
            or not MIN_PORT <= port <= MAX_PORT or parsed.netloc != '127.0.0.1:' + str(port)
            or parsed.path != '/navigator/callback' or parsed.query or parsed.fragment):
        raise ValueError('invalid redirect_uri')
    state = str(body.get('state') or '')
    if not state or len(state) > MAX_STATE_LENGTH: raise ValueError('invalid state')
    pkce = str(body.get('code_challenge') or '')
    if not re.fullmatch(rf'[A-Za-z0-9_-]{{{PKCE_MIN_LENGTH}}}', pkce): raise ValueError('invalid code_challenge')
    return {'redirect_uri':redirect,'state':state,'challenge':pkce}

def read_allowed(method, path):
    image_name = unquote(path.removeprefix('/images/'))
    image = path.startswith('/images/') and bool(image_name) and '/' not in image_name and '\\' not in image_name
    return method == 'GET' and (path in READ_ROUTES or image)

def query_identity(value):
    try: result = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as error: raise ValueError('query_id must be a UUID') from error
    return result

def public_user(row):
    return {key: row.get(key) or (row['name'] if key == 'display_name' else '') for key in ('user_id','name','display_name','role')}


def private_http_transport(peer, host):
    """Trust actual private peers and literal private Host, never proxy headers."""
    from core.prospector_contracts import HTTP_PRIVATE_NETWORKS
    try:
        peer_address = ipaddress.ip_address(peer)
        host_address = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        return False
    networks = tuple(ipaddress.ip_network(value) for value in HTTP_PRIVATE_NETWORKS)
    return any(peer_address in network for network in networks) and any(host_address in network for network in networks)
