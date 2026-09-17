#!/usr/bin/env python3
"""Loopback-only project data service; authenticated narrow business operations."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from urllib.parse import urlsplit, unquote
import project_database as db

MAX_BODY = 8 * 1024 * 1024

def configuration():
    token=os.environ.get('AUTOARTICLE_DB_SERVICE_TOKEN','')
    if len(token)<32 or any(ch.isspace() for ch in token):
        raise ValueError('Service authentication configuration is invalid')
    source=os.environ.get('AUTOARTICLE_DATA_SOURCE','legacy')
    if source not in ('legacy','project-db'): raise ValueError('Invalid data source')
    url=urlsplit(os.environ.get('AUTOARTICLE_DB_SERVICE_URL','http://127.0.0.1:8766'))
    if url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        raise ValueError('Service URL must be a loopback HTTP origin')
    port=url.port if url.port is not None else 8766
    if not 1<=port<=65535: raise ValueError('Invalid service port')
    return token,port

class Handler(BaseHTTPRequestHandler):
    server_version='AutoArticleDB/1'
    def log_message(self,*args):
        pass
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
    def send(self,status,payload):
        body=db.canonical(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection=True
    def authorized(self):
        actual=self.headers.get('Authorization','')
        if not hmac.compare_digest(actual.encode(),('Bearer '+self.server.token).encode()):
            self.send(401,dict(error='unauthorized'))
            return False
        return True
    def do_GET(self):
        if not self.authorized(): return
        path=urlsplit(self.path).path
        try:
            if path=='/health':
                db.status(self.server.database)
                return self.send(200,dict(status='ok'))
            if path=='/v1/status': return self.send(200,db.status(self.server.database))
            if path in ('/v1/read/talents', '/v1/read/article_feedback'):
                from project_readers import service_rows
                return self.send(200,service_rows(path.rsplit('/',1)[-1],self.server.database))
            if path.startswith('/v1/write-status/'):
                from project_write_outbox import status
                result=status(unquote(path[len('/v1/write-status/'):]),self.server.database)
                return self.send(200 if result else 404,result or dict(error='not_found'))
            if path.startswith('/v1/articles/'):
                article_id=unquote(path[len('/v1/articles/'):])
                c=db.connect(self.server.database,readonly=True)
                try:
                    c.execute('BEGIN')
                    row=c.execute('SELECT * FROM articles WHERE id=?',(article_id,)).fetchone()
                    if not row: return self.send(404,dict(error='not_found'))
                    payload=dict(article=dict(row),identifiers=[dict(r) for r in c.execute('SELECT * FROM article_identifiers WHERE article_id=?',(article_id,))],
                        contents=[dict(r) for r in c.execute('SELECT v.id AS version_id,p.* FROM article_content_versions v JOIN content_payloads p ON p.id=v.payload_id WHERE v.article_id=?',(article_id,))],
                        attempts=[dict(r) for r in c.execute('SELECT * FROM content_fetch_attempts WHERE article_id=?',(article_id,))],
                        reviews=[dict(r) for r in c.execute('SELECT * FROM review_records WHERE article_id=?',(article_id,))])
                    return self.send(200,payload)
                finally: c.close()
            return self.send(404,dict(error='not_found'))
        except Exception:
            self.send(503,dict(error='database_unavailable'))
    def do_POST(self):
        if not self.authorized(): return
        path=urlsplit(self.path).path
        operation=path[len('/v1/'):] if path.startswith('/v1/') else ''
        if operation not in db.OPERATIONS and operation not in ('write/collection','write/talent','write/classification','write/feedback'): return self.send(404,dict(error='not_found'))
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
            return self.send(400,dict(error='invalid_length'))
        try:
            size=int(self.headers['Content-Length'])
        except (ValueError,TypeError):
            return self.send(400,dict(error='invalid_length'))
        if not 0<size<=MAX_BODY: return self.send(413,dict(error='request_too_large'))
        if self.headers.get_content_type()!='application/json': return self.send(415,dict(error='json_required'))
        try:
            raw=self.rfile.read(size)
            if len(raw)!=size: raise ValueError('incomplete body')
            payload=json.loads(raw)
            expected={'operationId','payload'} if operation in ('write/talent','write/classification','write/feedback') else {'operationId','records'}
            if not isinstance(payload,dict) or set(payload)!=expected: raise ValueError('invalid payload')
            if operation=='write/collection':
                from project_business_writes import submit_collection
                result=submit_collection(payload['operationId'],payload['records'],self.server.database)
                from project_virtual_files import collection_output
                result=dict(result,collection=collection_output(payload['records'][0]['runDate'],self.server.database))
            elif operation.startswith('write/'):
                from project_business_writes import submit
                result=submit(payload['operationId'],operation.split('/')[1],payload['payload'],self.server.database)
            else:
                result=db.save_operation(operation,payload['operationId'],payload['records'],self.server.database)
            self.send(200,result)
        except db.Conflict:
            self.send(409,dict(error='operation_conflict'))
        except (ValueError,TypeError,db.sqlite3.IntegrityError):
            self.send(400,dict(error='invalid_operation'))
        except Exception:
            self.send(503,dict(error='database_unavailable'))

def make_server(database,token,port):
    if len(token)<32: raise ValueError('Invalid token configuration')
    db.status(database)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.database=database
    server.token=token
    return server

def ensure_running(root, env):
    """Start this existing service when n8n needs it; never log credentials."""
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path
    # Validate the same configuration used by main without altering process env.
    origin=env.get('AUTOARTICLE_DB_SERVICE_URL','http://127.0.0.1:8766')
    parsed=urlsplit(origin)
    token=env.get('AUTOARTICLE_DB_SERVICE_TOKEN','')
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username or parsed.password or len(token)<32:
        raise ValueError('Invalid project DB service configuration')
    request=urllib.request.Request(origin.rstrip('/')+'/health',headers={'Authorization':'Bearer '+token})
    def alive():
        try:
            with urllib.request.urlopen(request,timeout=1) as response:
                return response.status==200
        except urllib.error.HTTPError:
            raise ValueError('Existing DB service authentication failed')
        except OSError:
            return False
    if alive():return
    logs=Path(root)/'.operation-logs';logs.mkdir(exist_ok=True)
    log=logs/'project-db-service.log'
    with log.open('ab') as stream:
        os.chmod(str(log),0o600)
        process=subprocess.Popen([sys.executable,str(Path(root)/'scripts/autoarticle_db_service.py')],cwd=str(root),env=env,
                                 stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
    for _ in range(30):
        if process.poll() is not None:raise ValueError('DB service startup failed')
        if alive():return
        time.sleep(0.1)
    raise ValueError('DB service startup not yet confirmed')


def main():
    try:
        token,port=configuration()
        server=make_server(db.database_path(),token,port)
    except Exception:
        raise SystemExit('Project database service configuration or initialization failed')
    print('Project database service listening on 127.0.0.1:'+str(port),flush=True)
    try: server.serve_forever()
    finally: server.server_close()

if __name__=='__main__': main()
