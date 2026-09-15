"""Phase 2 regression and clean-environment acceptance tests (synthetic DBs only)."""
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import venv
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project_database as db
import autoarticle_db_service as service

class Phase2AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.path=self.root/'project.sqlite'
        db.migrate(self.path)
    def tearDown(self):
        self.temp.cleanup()
    def test_backup_restore_all_connection_settings(self):
        observations=[]
        opened=[]
        original=sqlite3.connect
        class AuditedConnection(sqlite3.Connection):
            def backup(self,target,*args,**kwargs):
                observations.append({label:{
                    'foreign_keys':c.execute('PRAGMA foreign_keys').fetchone()[0],
                    'busy_timeout':c.execute('PRAGMA busy_timeout').fetchone()[0],
                    'journal_mode':c.execute('PRAGMA journal_mode').fetchone()[0],
                    'query_only':c.execute('PRAGMA query_only').fetchone()[0],
                } for label,c in (('source',self),('destination',target))})
                return super().backup(target,*args,**kwargs)
        def factory(*args,**kwargs):
            kwargs['factory']=AuditedConnection
            c=original(*args,**kwargs)
            opened.append(c)
            return c
        with patch.object(sqlite3,'connect',factory):
            result=db.backup(self.root/'backup.sqlite',self.path)
            restored=db.restore_check(self.root/'backup.sqlite',self.root/'restored.sqlite')
        expected={'source':{'foreign_keys':1,'busy_timeout':10000,'journal_mode':'wal','query_only':1},
                  'destination':{'foreign_keys':1,'busy_timeout':10000,'journal_mode':'wal','query_only':0}}
        self.assertEqual(observations,[expected,expected])
        self.assertEqual(result['foreign_keys'],{'source':1,'destination':1})
        self.assertEqual(restored['foreign_keys'],{'source':1,'destination':1})
        self.assertEqual(restored['integrity'],'ok')
        self.assertEqual(len(opened),6)  # two pairs, restored validation and status
        for c in opened:
            with self.assertRaises(sqlite3.ProgrammingError): c.execute('SELECT 1')
        self.assertEqual(result['sha256'],hashlib.sha256((self.root/'backup.sqlite').read_bytes()).hexdigest())
    def test_destination_failure_closes_source(self):
        source=db.connect(self.path,readonly=True)
        with patch.object(db,'connect',side_effect=[source,RuntimeError('simulated destination failure')]):
            with self.assertRaises(RuntimeError): db.backup(self.root/'failed.sqlite',self.path)
        with self.assertRaises(sqlite3.ProgrammingError): source.execute('SELECT 1')
        # Preserve the failed candidate; never overwrite it on retry.
        with self.assertRaises(ValueError): db.backup(self.root/'failed.sqlite',self.path)
    def test_disabled_foreign_keys_fail_closed(self):
        original=sqlite3.connect
        class DisabledConnection(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                if sql=='PRAGMA foreign_keys=ON': sql='PRAGMA foreign_keys=OFF'
                return super().execute(sql,*args,**kwargs)
        def factory(*args,**kwargs):
            kwargs['factory']=DisabledConnection
            return original(*args,**kwargs)
        with patch.object(sqlite3,'connect',factory):
            with self.assertRaisesRegex(RuntimeError,'Foreign keys unavailable'):
                db.backup(self.root/'disabled.sqlite',self.path)
        self.assertFalse((self.root/'disabled.sqlite').exists())
    def test_schema_snapshot_and_reproducible_upgrade(self):
        self.assertEqual((db.ROOT/'database/schema.sql').read_bytes(),b''.join(p.read_bytes() for p in sorted(db.MIGRATIONS.glob('*.sql'))))
        directory=self.root/'migrations'
        shutil.copytree(db.MIGRATIONS,directory)
        fixed='2026-09-14T00:00:00.000000Z'
        upgraded=self.root/'upgraded.sqlite'
        fresh=self.root/'fresh.sqlite'
        with patch.object(db,'now',return_value=fixed):
            db.migrate(upgraded,directory)
            (directory/'900_acceptance.sql').write_text('CREATE TABLE acceptance_upgrade(id TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);\n')
            self.assertEqual(db.migrate(upgraded,directory),{'applied':['900']})
            self.assertEqual(db.migrate(fresh,directory),{'applied':[p.name.split('_')[0] for p in sorted(directory.glob('*.sql'))]})
            with contextlib.closing(db.connect(upgraded,readonly=True)) as a, contextlib.closing(db.connect(fresh,readonly=True)) as b:
                self.assertEqual(list(a.iterdump()),list(b.iterdump()))
            before=upgraded.read_bytes()
            self.assertEqual(db.migrate(upgraded,directory),{'applied':[]})
            self.assertEqual(before,upgraded.read_bytes())
    def test_cli_and_service_in_clean_stdlib_environment(self):
        project=self.root/'clean-project'
        (project/'scripts').mkdir(parents=True)
        for name in ('project_database.py','autoarticle_db.py','autoarticle_db_service.py'):
            shutil.copyfile(db.ROOT/'scripts'/name,project/'scripts'/name)
        shutil.copytree(db.ROOT/'database',project/'database')
        environment=self.root/'venv'
        venv.EnvBuilder(with_pip=False).create(environment)
        python=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        env={k:v for k,v in os.environ.items() if not k.startswith(('AUTOARTICLE_','PYTHON'))}
        env['PYTHONDONTWRITEBYTECODE']='1'
        database=project/'data/autoarticle.sqlite'
        cli=[str(python),str(project/'scripts/autoarticle_db.py')]
        def run(*args,custom_env=None):
            proc=subprocess.run(cli+list(args),cwd=self.root,env=custom_env or env,capture_output=True,text=True,timeout=20)
            self.assertEqual(proc.returncode,0,proc.stderr)
            return json.loads(proc.stdout)
        self.assertEqual(run('init'),{'applied':[p.name.split('_')[0] for p in sorted(db.MIGRATIONS.glob('*.sql'))]})
        before=database.read_bytes()
        self.assertEqual(run('init'),{'applied':[]})
        self.assertEqual(run('migrate'),{'applied':[]})
        self.assertEqual(before,database.read_bytes())
        state=run('status')
        self.assertEqual(len(state['cutover']),5)
        self.assertTrue(all(r['read_source']==r['write_target']=='legacy' for r in state['cutover']))
        self.assertEqual(run('conflicts'),[])
        snapshot=project/'data/backups/empty.sqlite'
        result=run('backup','--output',str(snapshot))
        self.assertEqual(result['foreign_keys'],{'source':1,'destination':1})
        restored=project/'data/backups/restore-test.sqlite'
        self.assertEqual(run('restore-check','--snapshot',str(snapshot),'--output',str(restored))['integrity'],'ok')
        with contextlib.closing(db.connect(database,readonly=True)) as a, contextlib.closing(db.connect(restored,readonly=True)) as b:
            self.assertEqual(list(a.iterdump()),list(b.iterdump()))
            tables=[r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_migrations','cutover_state')")]
            self.assertTrue(all(b.execute('SELECT count(*) FROM '+t).fetchone()[0]==0 for t in tables))
        alternate=dict(env,AUTOARTICLE_DATABASE_PATH=str(self.root/'explicit.sqlite'))
        self.assertEqual(run('init',custom_env=alternate),{'applied':[p.name.split('_')[0] for p in sorted(db.MIGRATIONS.glob('*.sql'))]})
        token='synthetic-service-token-'+os.urandom(20).hex()
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port=s.getsockname()[1]
        service_env=dict(env,AUTOARTICLE_DB_SERVICE_TOKEN=token,AUTOARTICLE_DB_SERVICE_URL='http://127.0.0.1:'+str(port),AUTOARTICLE_DATA_SOURCE='legacy')
        cmd=[str(python),str(project/'scripts/autoarticle_db_service.py')]
        process=subprocess.Popen(cmd,cwd=self.root,env=service_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def request(method,path,payload=None):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=1)
            try:
                c.request(method,path,body=json.dumps(payload) if payload is not None else None,
                          headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                response=c.getresponse()
                return response.status,json.loads(response.read())
            finally: c.close()
        try:
            deadline=time.monotonic()+10
            while True:
                try:
                    health=request('GET','/health')
                    break
                except (ConnectionError,OSError):
                    if process.poll() is not None or time.monotonic()>deadline:
                        self.fail('Clean-environment service failed to start')
                    time.sleep(.05)
            self.assertEqual(health,(200,{'status':'ok'}))
            self.assertEqual(request('POST','/v1/sql',{'sql':'DROP TABLE articles'})[0],404)
            self.assertEqual(request('POST','/v1/articles',{'operationId':'acceptance-sql-1','records':{'sql':['DROP TABLE articles']}})[0],400)
            with contextlib.closing(db.connect(database,readonly=True)) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM articles').fetchone()[0],0)
        finally:
            process.terminate()
            stdout,stderr=process.communicate(timeout=10)
        self.assertTrue(token not in stdout+stderr,'Service logs contain secret')
        self.assertIn('127.0.0.1:'+str(port),stdout)
        bad=dict(service_env,AUTOARTICLE_DB_SERVICE_URL='http://0.0.0.0:8766/'+token)
        failed=subprocess.run(cmd,env=bad,cwd=self.root,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(failed.returncode,0)
        self.assertTrue(token not in failed.stdout+failed.stderr,'Configuration error exposes secret')
    def test_configuration_rejects_invalid_values(self):
        base=dict(AUTOARTICLE_DB_SERVICE_TOKEN='test-only-'+'x'*40,AUTOARTICLE_DB_SERVICE_URL='http://127.0.0.1:8766',AUTOARTICLE_DATA_SOURCE='legacy')
        for field,value in (('AUTOARTICLE_DB_SERVICE_TOKEN',''),('AUTOARTICLE_DB_SERVICE_TOKEN','has spaces '+'x'*40),('AUTOARTICLE_DATA_SOURCE','other'),('AUTOARTICLE_DB_SERVICE_URL','https://127.0.0.1:8766'),('AUTOARTICLE_DB_SERVICE_URL','http://127.0.0.1:0'),('AUTOARTICLE_DB_SERVICE_URL','http://127.0.0.1:65536')):
            with self.subTest(field=field),patch.dict(os.environ,dict(base,**{field:value})):
                with self.assertRaises(ValueError): service.configuration()
        with patch.dict(os.environ,base):
            self.assertEqual(service.configuration()[1],8766)

if __name__=='__main__': unittest.main()
