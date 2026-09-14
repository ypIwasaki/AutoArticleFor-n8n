#!/usr/bin/env python3
"""Loopback-only project data service; authenticated narrow business operations."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from urllib.parse import urlsplit, unquote
import project_database as db

MAX_BODY = 1024 * 1024

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
        if operation not in db.OPERATIONS: return self.send(404,dict(error='not_found'))
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
            if not isinstance(payload,dict) or set(payload)!={'operationId','records'}: raise ValueError('invalid payload')
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
