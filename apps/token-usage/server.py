#!/usr/bin/env python3
"""Read-only, local token usage comparison app. No external dependencies."""
import argparse
import json
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
WEB = Path(__file__).resolve().parent / 'web'
REPORTS = ROOT / 'content' / 'operation-usage'


def load_reports(directory=REPORTS):
    reports, warnings = [], []
    for path in sorted(directory.glob('*.json')):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json', path.name):
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if value.get('reportKind') != 'autoarticle-token-usage' or value.get('schemaVersion') != 1:
                raise ValueError('未対応の記録形式')
            if value.get('workDate') != path.stem or not isinstance(value.get('rows'), list):
                raise ValueError('日付または工程データが不正')
            reports.append({key: value.get(key) for key in (
                'workDate', 'timezone', 'status', 'rows', 'knownTotals', 'generatedAt')})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            warnings.append(f'{path.name}: 読み込みできません ({type(exc).__name__})')
    return {'reports': reports, 'warnings': warnings}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path == '/api/reports':
            payload = json.dumps(load_reports(), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)
            return
        # Serve only packaged web files, never repository or task logs.
        if urlsplit(self.path).path not in ('/', '/index.html', '/app.js', '/model.mjs', '/styles.css'):
            self.send_error(404)
            return
        super().do_GET()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(WEB)))
    print(f'Token comparison: http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()
