"""Temporary SQLite + real HTTP Navigator fixture; never reads production CFG/database."""
import json
import secrets
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import manualdb
import manual_server
from test_port_routes import seed

class NavigatorFixture:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='navigator-isolated-')
        self.database = Path(self.directory.name) / 'test.db'
        self.password = secrets.token_urlsafe(24)
        self.initialize_database()
        self.patches = [patch.object(manual_server, 'conn', self.connect), patch.object(manual_server, 'LOGIN_LIMITER', manual_server.LoginLimiter())]
        for item in self.patches: item.start()
        self.server = manual_server.FreightHTTPServer(('127.0.0.1', 0), manual_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:' + str(self.server.server_port)
        return self

    def initialize_database(self):
        c = self.connect()
        try:
            seed(c)
            manualdb.save_operator(c, {'name':'fixture-admin','role':'admin','password_hash':manual_server.password_hash(self.password)})
        finally:
            c.close()

    def connect(self):
        return manualdb.connect(self.database)

    def request(self, path, payload=None, cookie='', bearer='', origin=None):
        headers = {'Content-Type':'application/json'}
        if cookie: headers['Cookie'] = cookie
        if bearer: headers['Authorization'] = 'Bearer ' + bearer
        if origin is not None: headers['Origin'] = origin
        request = urllib.request.Request(self.base_url + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.status, json.loads(content) if 'application/json' in response.headers.get('Content-Type','') else content.decode(), response.headers

    def login(self):
        return self.request('/api/auth/login', {'name':'fixture-admin','password':self.password})[2]['Set-Cookie'].split(';',1)[0]

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()
        for item in reversed(self.patches): item.stop()
        manualdb.invalidate_master()
        self.directory.cleanup()

if __name__ == '__main__':
    import sys
    with NavigatorFixture() as fixture:
        print(json.dumps({'base_url':fixture.base_url,'test_username':'fixture-admin','test_password':fixture.password}), flush=True)
        sys.stdin.readline()  # Cross-process tests keep stdin open, then send newline for cleanup.

