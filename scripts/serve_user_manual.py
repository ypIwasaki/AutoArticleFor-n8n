#!/usr/bin/env python3
"""Serve the local user manual and its documentation links only."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
ROOT=Path(__file__).resolve().parents[1]
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path=='/':
            self.send_response(302);self.send_header('Location','/docs/user-manual.html');self.end_headers();return
        path=(ROOT/unquote(urlsplit(self.path).path).lstrip('/')).resolve()
        docs=ROOT/'docs'
        explicit={ROOT/'README.md',ROOT/'apps/token-usage/README.md',ROOT/'.agents/skills/autoarticle-operations/SKILL.md',ROOT/'n8n/workflows/daily-keyword-news-summary.workflow.json',ROOT/'apps/talent-dashboard/web/records.js'}
        if not path.is_file() or not (path in explicit or docs in path.parents and path.suffix in {'.md','.html'}):
            self.send_error(404);return
        super().do_GET()
    def do_HEAD(self):
        self.send_error(405)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8878);a=p.parse_args()
    print(f'Manual: http://127.0.0.1:{a.port}/docs/user-manual.html',flush=True)
    ThreadingHTTPServer(('127.0.0.1',a.port),partial(Handler,directory=str(ROOT))).serve_forever()
