"""Opt-in real PostgreSQL contract tests in a private TEMP cluster; no production DSN."""
import os
import secrets
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import manualdb
import manual_server
import postgresdb
from ops.postgres_migrate import migrate
from test_navigator_fixture import NavigatorFixture
from test_port_routes import seed
import test_prospector_auth as sqlite_tests

PRIVATE_PG_OPT_IN = 'NAVIGATOR_PRIVATE_POSTGRES_TEST'
POSTGRES_BIN = Path('C:/Program Files/PostgreSQL/18/bin')
PROCESS_TIMEOUT_SECONDS = 60

class PrivatePostgresCluster:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='navigator-private-pg-')
        self.root = Path(self.directory.name).resolve()
        self.data = self.root / 'data'
        self.started = False
        try:
            self._run('initdb', '-D', str(self.data), '-U', 'fixture', '--no-locale', '--encoding=UTF8', '--auth-local=trust', '--auth-host=trust')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                self.port = listener.getsockname()[1]
            self._run('pg_ctl', '-D', str(self.data), '-l', str(self.root/'server.log'), '-o', '-h 127.0.0.1 -p ' + str(self.port), '-w', 'start')
            self.started = True
            self.dsn = make_conninfo(host='127.0.0.1',port=self.port,dbname='postgres',user='fixture',connect_timeout=10)
            return self
        except Exception:
            self.__exit__()
            raise

    def _run(self, program, *args):
        # Windows daemon descendants inherit pipes; a file avoids waiting for server EOF.
        output=self.root/(program+'-command.log')
        with output.open('w',encoding='utf-8') as stream:
            result=subprocess.run([str(POSTGRES_BIN/(program+'.exe')),*args],stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,timeout=PROCESS_TIMEOUT_SECONDS,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise RuntimeError(program + ' failed: ' + output.read_text(encoding='utf-8',errors='replace'))

    def __exit__(self,*args):
        if self.started or (self.data/'postmaster.pid').exists():
            self._run('pg_ctl','-D',str(self.data),'-m','fast','-w','stop')
            self.started=False
        # TemporaryDirectory owns exactly the resolved private root; never a PG service path.
        self.directory.cleanup()

class PostgresNavigatorFixture(NavigatorFixture):
    def __init__(self, cluster):
        self.cluster=cluster
        self.schema='fixture_'+uuid.uuid4().hex
        self.test_dsn=make_conninfo(cluster.dsn,options='-c search_path='+self.schema)

    def initialize_database(self):
        c=manualdb.connect(self.database)
        try:
            seed(c)
            manualdb.save_operator(c,{'name':'fixture-admin','role':'admin','password_hash':manual_server.password_hash(self.password)})
        finally:
            c.close()
        with psycopg.connect(self.cluster.dsn,autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        result=migrate(self.database,self.test_dsn,include_business_data=False,replace_target=False)
        if not result['ok']: raise AssertionError('private synthetic configuration migration failed')
        c=self.connect()
        try: c.ensure_identity_schema()
        finally: c.close()

    def connect(self):
        return postgresdb.Connection(self.test_dsn)

    def __exit__(self,*args):
        try:
            super().__exit__(*args)
        finally:
            with psycopg.connect(self.cluster.dsn,autocommit=True) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

@unittest.skipUnless(os.environ.get(PRIVATE_PG_OPT_IN)=='1','requires private TEMP PostgreSQL opt-in')
class ProspectorPrivatePostgresTests(sqlite_tests.ProspectorAuthTests):
    @classmethod
    def setUpClass(cls):
        cls.cluster=PrivatePostgresCluster().__enter__()
        cls.fixture_factory=lambda self: PostgresNavigatorFixture(cls.cluster)

    @classmethod
    def tearDownClass(cls):
        cls.cluster.__exit__()

if __name__=='__main__': unittest.main()
