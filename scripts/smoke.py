"""Owned synthetic network drill against a separate disposable PostgreSQL DB."""
import argparse
from importlib.resources import files
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from anymsg import Client
from messaging_service.store import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dsn-file',required=True,type=Path)
    args=parser.parse_args()
    base=args.dsn_file.read_text().strip()
    assert conninfo_to_dict(base)['dbname']=='messaging_probe_test'
    conninfo_to_dict(base)
    owner=make_conninfo(base,dbname='anymsg_smoke_ci')
    gateway=make_conninfo(owner,user='anymsg_smoke_api',password='synthetic-smoke-password')
    source=Path(__file__).resolve().parents[1]
    created=False
    role=False
    child=None
    store=None
    with tempfile.TemporaryDirectory(prefix='anymsg-smoke-') as work:
        p=Path(work)
        try:
            with psycopg.connect(base,autocommit=True) as c:
                c.execute('CREATE DATABASE anymsg_smoke_ci')
                created=True
            store=Store(owner,p/'files','http://127.0.0.1:18940')
            store.migrate()
            store.seed()
            token=store.issue_key('smoke-agent',['telegram-demo','whatsapp-demo-1'])
            with psycopg.connect(owner,autocommit=True) as c:
                c.execute(files('messaging_service').joinpath('gateway_role.sql').read_text())
                c.execute("CREATE ROLE anymsg_smoke_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'synthetic-smoke-password'")
                role=True
                c.execute('GRANT messaging_gateway TO anymsg_smoke_api')
            store.close();store=None
            (p/'api.dsn').write_text(gateway);(p/'api.dsn').chmod(0o600)
            (p/'agent.token').write_text(token);(p/'agent.token').chmod(0o600)
            env={**os.environ,'DATABASE_URL_FILE':str(p/'api.dsn'),'PROBE_FILES':str(p/'files'),
                 'PROBE_PUBLIC_URL':'http://127.0.0.1:18940','PROBE_ALLOWED_HOSTS':'127.0.0.1:18940',
                 'PROBE_BIND':'127.0.0.1','PROBE_PORT':'18940'}
            child=subprocess.Popen([sys.executable,'-m','anymsg','serve'],env=env,
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            with Client('http://127.0.0.1:18940',token,timeout=2) as client:
                for _ in range(100):
                    if child.poll() is not None:raise RuntimeError('synthetic_server_exited')
                    try:
                        client.capabilities();break
                    except Exception:time.sleep(0.1)
                else:raise RuntimeError('synthetic_server_start_timeout')
                assert client.search(query='Маяк')['items'][0]['id']=='demo-tg-1'
                assert client.search(query='PRIVATE_SCOPE_SENTINEL')['items']==[]
            subprocess.run([sys.executable,str(source/'client_check.py'),'--url',
                            'http://127.0.0.1:18940/mcp','--token-file',str(p/'agent.token')],check=True)
            js='''import {readFileSync} from 'node:fs';
import {AnyMsgClient} from './packages/npm/lib/index.js';
const c=new AnyMsgClient({baseUrl:'http://127.0.0.1:18940',token:readFileSync(process.env.ANYMSG_SMOKE_TOKEN_FILE,'utf8').trim()});
const r=await c.search({query:'Маяк'});
if(r.items[0]?.id!=='demo-tg-1')process.exit(1);
console.log('npm HTTP client: real synthetic request passed');'''
            subprocess.run(['node','--input-type=module','-e',js],cwd=source,
                           env={**os.environ,'ANYMSG_SMOKE_TOKEN_FILE':str(p/'agent.token')},check=True)
        finally:
            if child:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait()
            if store:store.close()
            with psycopg.connect(base,autocommit=True) as c:
                if created:c.execute('DROP DATABASE anymsg_smoke_ci WITH (FORCE)')
                if role:c.execute('DROP ROLE anymsg_smoke_api')
    print('network smoke passed; owned server, role, DB and files removed')

if __name__=='__main__':main()
