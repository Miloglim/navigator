"""Reproducible account-session load test. All writes use a disposable database/schema."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import manualdb
import manual_server as server
from ops.account_security import LoginLimiter
from test_port_routes import seed, LANE_A, PORT_A, POL, CARRIER

FIXTURE_ROWS=1200
MAX_CLIENTS=300
REQUEST_PATHS=('/api/rates?limit=50','/api/space?limit=50','/api/rates_grouped?limit=50')
REQUEST_TIMEOUT_SECONDS=60
LOGIN_RETRY_LIMIT=40

@contextmanager
def fixture_backend(backend):
    if backend=='sqlite':
        with tempfile.TemporaryDirectory(prefix='lan-capacity-') as folder:
            database=Path(folder)/'fixture.db'
            connection=manualdb.connect(database);connection.close()
            yield lambda:manualdb.connect(database)
    else:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        import postgresdb
        from ops.postgres_sync import load_dsn
        from ops.postgres_migrate import postgres_schema
        base=load_dsn(postgresdb.DEFAULT_ENV_FILE)
        schema='freight_load_'+uuid.uuid4().hex
        with psycopg.connect(base,autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            isolated=make_conninfo(base,options='-c search_path='+schema)
            with psycopg.connect(isolated) as connection:
                for statement in postgres_schema():connection.execute(statement)
            yield lambda:postgresdb.pooled_connect(isolated)
        finally:
            postgresdb.close_pool(isolated)
            with psycopg.connect(base,autocommit=True) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def summary(results):
    values=sorted(row['ms'] for row in results)
    def percentile(fraction):return round(values[min(len(values)-1,int((len(values)-1)*fraction))],2) if values else 0
    return {'requests':len(results),'success':sum(row['ok'] for row in results),'failed':sum(not row['ok'] for row in results),
            'p50_ms':percentile(.50),'p95_ms':percentile(.95),'p99_ms':percentile(.99),
            'max_ms':round(max(values),2) if values else 0,
            'errors':sorted({row['error'] for row in results if row.get('error')})}


def run(backend='sqlite',clients=300,requests_per_client=3):
    if clients<1 or clients>MAX_CLIENTS:raise ValueError('clients must be 1..300')
    with fixture_backend(backend) as connect:
        password=secrets.token_urlsafe(24);hashed=server.password_hash(password)
        connection=connect()
        try:
            seed(connection)
            for index in range(clients):
                connection.execute('INSERT INTO operators(user_id,name,role,active,password_hash,groups) VALUES(?,?,?,?,?,?)',
                    ('usr_'+uuid.uuid4().hex,'load-user-'+str(index),'sales',1,hashed,'Group A'))
            for index in range(FIXTURE_ROWS):
                connection.execute('INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,route,container_type,price_usd,source_group,message_time,status,valid_from,valid_to) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('frt_'+uuid.uuid4().hex,'fixture-'+str(index),'rates' if index%3 else 'space',POL,PORT_A,CARRIER,LANE_A,'20GP',100+index,
                     'Group A' if index%2 else 'Group B','2026-09-29 12:00:00','当前生效','2026-09-01','2026-10-31'))
            connection.commit()
        finally:connection.close()
        with patch.object(server,'conn',side_effect=connect),patch.object(server,'LOGIN_LIMITER',LoginLimiter()):
            http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever);thread.start();base='http://127.0.0.1:%d'%http.server_port
            def request(path,payload=None,cookie=''):
                req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'Content-Type':'application/json','Cookie':cookie})
                try:response=urllib.request.urlopen(req,timeout=REQUEST_TIMEOUT_SECONDS)
                except urllib.error.HTTPError as error:response=error
                with response:return response.status,json.load(response),response.headers
            def login(index):
                start=time.perf_counter();retries=0
                try:
                    for attempt in range(LOGIN_RETRY_LIMIT):
                        status,data,headers=request('/api/auth/login',{'name':'load-user-'+str(index),'password':password})
                        if status==200:
                            return {'ok':True,'ms':(time.perf_counter()-start)*1000,'retries':retries,'cookie':headers['Set-Cookie'].split(';',1)[0]}
                        if status!=429 or data.get('code')!='login_busy':
                            return {'ok':False,'ms':(time.perf_counter()-start)*1000,'retries':retries,'error':'HTTP '+str(status)}
                        retries+=1
                        time.sleep(min(float(headers.get('Retry-After','1')),5)+(index%11)/20)
                    return {'ok':False,'ms':(time.perf_counter()-start)*1000,'error':'login wait exceeded'}
                except Exception as error:
                    return {'ok':False,'ms':(time.perf_counter()-start)*1000,'error':type(error).__name__}
            def queries(cookie):
                rows=[]
                for index in range(requests_per_client):
                    start=time.perf_counter()
                    try:
                        status,data,_=request(REQUEST_PATHS[index%len(REQUEST_PATHS)],cookie=cookie)
                        # Check scope on returned facts; a successful status alone is insufficient.
                        leak=any(row.get('source_group')!='Group A' for row in data.get('rows',[]))
                        leak=leak or any(group['current'].get('source_group')!='Group A' for group in data.get('groups',[]))
                        ok=status==200 and data.get('ok') is True and not leak
                        rows.append({'ok':ok,'ms':(time.perf_counter()-start)*1000,'error':'' if ok else ('scope leak' if leak else 'HTTP '+str(status))})
                    except Exception as error:rows.append({'ok':False,'ms':(time.perf_counter()-start)*1000,'error':type(error).__name__})
                return rows
            started=time.perf_counter()
            try:
                with ThreadPoolExecutor(max_workers=clients) as pool:logins=list(pool.map(login,range(clients)))
                login_summary=summary(logins)
                login_summary['busy_retries']=sum(row.get('retries',0) for row in logins)
                print(json.dumps({'phase':'login_complete','backend':backend,**login_summary}),flush=True)
                with ThreadPoolExecutor(max_workers=clients) as pool:
                    batches=list(pool.map(queries,[row['cookie'] for row in logins if row['ok']]))
                query_summary=summary([row for batch in batches for row in batch])
                return {'ok':login_summary['failed']==0 and query_summary['failed']==0,
                    'created_at':datetime.now().isoformat(timespec='seconds'),'backend':backend,'clients':clients,
                    'fixture_rows':FIXTURE_ROWS,'isolation':'disposable database/schema; no production writes',
                    'network':'loopback HTTP; TLS verified separately; no LAN latency measured',
                    'login':login_summary,'query':query_summary,'elapsed_seconds':round(time.perf_counter()-started,2)}
            finally:http.shutdown();thread.join(timeout=10);http.server_close();manualdb.invalidate_master()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--backend',choices=('sqlite','postgres'),default='sqlite')
    parser.add_argument('--clients',type=int,default=300)
    parser.add_argument('--requests-per-client',type=int,default=3)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();result=run(args.backend,args.clients,args.requests_per_client)
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
