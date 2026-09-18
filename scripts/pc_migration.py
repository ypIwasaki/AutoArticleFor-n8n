#!/usr/bin/env python3
"""Offline PC transfer for the WSL/npm deployment. Never starts services."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'autoarticle-pc-transfer-v1'


class MigrationError(ValueError):
    """Only deliberately public, credential-free errors use this class."""


PROJECT_TREES = ('content', 'config', '.operation-state')
OMIT_STATE = {'.operation-state/n8n-process.json', '.operation-state/dashboard-process.json',
              '.operation-state/token-usage/binding.json'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def env_values(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"\'')
    return values


def standard_layout(root):
    values = env_values(root / '.env')
    for key in ('AUTOARTICLE_DATABASE_PATH', 'N8N_USER_FOLDER', 'N8N_DATABASE_PATH', 'N8N_ENCRYPTION_KEY'):
        # npm startup deliberately does not source N8N_ENCRYPTION_KEY from .env.
        if os.environ.get(key) or (key != 'N8N_ENCRYPTION_KEY' and values.get(key)):
            raise MigrationError('Custom runtime setting requires a separate migration plan: ' + key)
    if (os.environ.get('DB_TYPE') or values.get('DB_TYPE') or 'sqlite') != 'sqlite':
        raise MigrationError('Only the standard SQLite n8n deployment is supported.')
    from urllib.parse import urlsplit
    api = urlsplit(os.environ.get('N8N_API_BASE_URL') or values.get('N8N_API_BASE_URL') or 'http://localhost:5678')
    if api.scheme != 'http' or api.hostname not in ('localhost', '127.0.0.1') or api.username or api.password:
        raise MigrationError('Only a local n8n API deployment is supported.')
    return values


def ensure_stopped(root, confirmed):
    if not confirmed:
        raise MigrationError('Stop all writers, including Windows apps, then pass --writers-stopped.')
    if sys.platform != 'linux':
        raise MigrationError('Run this command inside WSL/Ubuntu.')
    values = standard_layout(root)
    # Check conventional listeners as well as custom configured local ports.
    from urllib.parse import urlsplit
    ports = {5678, 8765, 8766, 8878, 8877}
    for key in ('N8N_PORT', 'TALENT_DASHBOARD_PORT'):
        if values.get(key): ports.add(int(values[key]))
        if os.environ.get(key): ports.add(int(os.environ[key]))
    for key in ('N8N_API_BASE_URL', 'AUTOARTICLE_DB_SERVICE_URL'):
        url = urlsplit(os.environ.get(key) or values.get(key, ''))
        if url.port: ports.add(url.port)
    for port in ports:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                raise MigrationError('A local service is still listening on port ' + str(port))
        except OSError:
            pass
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid(): continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            cwd = (proc / 'cwd').resolve()
        except (OSError, PermissionError):
            continue
        names = [Path(a.decode(errors='replace')).name for a in args if a]
        if not names: continue
        runtime = names[0].startswith(('python', 'node', 'n8n'))
        if runtime and ('n8n' in names or any(b'/n8n/' in a for a in args) or
                        cwd == root or root in cwd.parents or any(str(root).encode() in a for a in args)):
            raise MigrationError('A possible writer is still running (PID ' + proc.name + ').')


def walk_files(base):
    if base.is_symlink(): raise MigrationError('Symlinks are not supported in transfer inputs.')
    if not base.exists(): return
    for folder, dirs, files in os.walk(str(base)):
        for name in dirs + files:
            p = Path(folder) / name
            if p.is_symlink(): raise MigrationError('Symlinks are not supported in transfer inputs.')
        for name in files:
            p = Path(folder) / name
            if not p.is_file(): raise MigrationError('Non-regular transfer input.')
            yield p


def source_files(root, n8n):
    result = {'project/.env': root / '.env', 'project/data/autoarticle.sqlite': root / 'data/autoarticle.sqlite'}
    for tree in PROJECT_TREES:
        for p in walk_files(root / tree):
            rel = p.relative_to(root).as_posix()
            if rel not in OMIT_STATE:
                result['project/' + rel] = p
    for p in walk_files(n8n):
        rel = p.relative_to(n8n).as_posix()
        if rel.startswith('n8nEventLog') or rel in ('database.sqlite-wal', 'database.sqlite-shm', 'database.sqlite-journal'):
            continue
        result['n8n/' + rel] = p
    for p in result.values():
        if not p.is_file() or p.is_symlink(): raise MigrationError('A required transfer file is missing or linked.')
    if 'n8n/database.sqlite' not in result or 'n8n/config' not in result:
        raise MigrationError('n8n database and encryption configuration are required.')
    config = json.loads((n8n / 'config').read_text())
    if not config.get('encryptionKey'): raise MigrationError('n8n encryption configuration is missing.')
    return result


def fingerprints(files):
    return {key: {'size': p.stat().st_size, 'sha256': digest(p)} for key, p in sorted(files.items())}


def check_database(path):
    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if c.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise MigrationError('SQLite integrity check failed.')
    finally:
        c.close()


def snapshot(source, target):
    src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()
    os.chmod(str(target), 0o600)
    check_database(target)


def pending_executions(path):
    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return c.execute("SELECT count(*) FROM execution_entity WHERE status IN ('new','running','waiting')").fetchone()[0]
    finally:
        c.close()


def export_bundle(root, n8n, output, confirmed):
    ensure_stopped(root, confirmed)
    if git(root, 'status', '--porcelain', '--untracked-files=normal'):
        raise MigrationError('Commit project changes before export; transfer requires a clean checkout.')
    if output.exists() or output.is_symlink(): raise MigrationError('Output must be a new directory.')
    for base in [n8n, *[root / name for name in PROJECT_TREES]]:
        if output == base or base in output.parents: raise MigrationError('Output overlaps transfer inputs.')
    if pending_executions(n8n / 'database.sqlite'):
        raise MigrationError('Resolve new/running/waiting n8n executions before transfer.')
    sources = source_files(root, n8n)
    before = fingerprints(sources)
    # WAL content is part of the frozen source state even though snapshots absorb it.
    sidecars = {str(p): p for base in (root / 'data/autoarticle.sqlite', n8n / 'database.sqlite')
                for p in [Path(str(base) + '-wal')] if p.exists()}
    wal_before = fingerprints(sidecars)
    check_destination(output)
    output.mkdir(parents=True, mode=0o700)
    (output / 'INCOMPLETE').write_text('Transfer is not usable until manifest.json is written.\n')
    for rel, source in sources.items():
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if rel in ('project/data/autoarticle.sqlite', 'n8n/database.sqlite'):
            snapshot(source, target)
        else:
            shutil.copy2(str(source), str(target))
            os.chmod(str(target), 0o700 if os.access(str(source), os.X_OK) else 0o600)
    ensure_stopped(root, confirmed)
    current_sidecars = {str(p): p for base in (root / 'data/autoarticle.sqlite', n8n / 'database.sqlite')
                        for p in [Path(str(base) + '-wal')] if p.exists()}
    if fingerprints(source_files(root, n8n)) != before or fingerprints(current_sidecars) != wal_before:
        raise MigrationError('Source changed during export; retain incomplete output and export again after stopping writers.')
    entries = fingerprints({rel: output / rel for rel in sources})
    manifest = {'format': FORMAT, 'commit': git(root, 'rev-parse', 'HEAD'),
                'runtime': json.loads((root / 'config/runtime-versions.json').read_text()), 'files': entries}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (output / 'INCOMPLETE').unlink()
    return {'status': 'exported', 'files': len(entries), 'manifestSha256': digest(output / 'manifest.json')}


def safe_relative(rel):
    p = PurePosixPath(rel)
    if not rel or '\\' in rel or ':' in rel or p.is_absolute() or any(x in ('', '.', '..') for x in rel.split('/')):
        raise MigrationError('Invalid transfer path.')
    allowed = rel in ('project/.env', 'project/data/autoarticle.sqlite') or any(
        rel.startswith('project/' + name + '/') for name in PROJECT_TREES) or rel.startswith('n8n/')
    if not allowed: raise MigrationError('Unexpected transfer path.')
    return p


def verify_bundle(bundle):
    if bundle.is_symlink() or (bundle / 'INCOMPLETE').exists(): raise MigrationError('Incomplete or linked bundle.')
    # Inspect every member before reading a possibly linked manifest.
    actual = {p.relative_to(bundle).as_posix() for p in walk_files(bundle)}
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('format') != FORMAT or not isinstance(manifest.get('files'), dict):
        raise MigrationError('Unsupported transfer format.')
    files = manifest['files']
    required = {'project/.env', 'project/data/autoarticle.sqlite', 'n8n/config', 'n8n/database.sqlite'}
    if not required <= set(files) or actual != set(files) | {'manifest.json'}:
        raise MigrationError('Transfer file set differs from manifest.')
    for rel, expected in files.items():
        safe_relative(rel)
        p = bundle / rel
        if p.stat().st_size != expected['size'] or digest(p) != expected['sha256']:
            raise MigrationError('Transfer checksum mismatch.')
    for rel in ('project/data/autoarticle.sqlite', 'n8n/database.sqlite'):
        check_database(bundle / rel)
    return manifest


def check_destination(path):
    for parent in [path, *path.parents]:
        if parent.is_symlink(): raise MigrationError('Restore destination contains a symlink.')


def restore_bundle(root, n8n, bundle, confirmed):
    ensure_stopped(root, confirmed)
    manifest = verify_bundle(bundle)
    if git(root, 'rev-parse', 'HEAD') != manifest['commit']:
        raise MigrationError('Checkout the commit recorded in manifest.json before restoring.')
    if git(root, 'status', '--porcelain', '--untracked-files=normal'):
        raise MigrationError('Restore requires a clean fresh clone.')
    for path in (root / '.env', root / 'data/autoarticle.sqlite',
                 root / 'data/autoarticle.sqlite-wal', root / 'data/autoarticle.sqlite-shm',
                 root / 'data/autoarticle.sqlite-journal', root / '.operation-state', n8n):
        if path.exists() or path.is_symlink(): raise MigrationError('Existing local state found; use a fresh clone and unused n8n home.')
    plan = []
    for rel, info in manifest['files'].items():
        parts = safe_relative(rel).parts
        target = (root if parts[0] == 'project' else n8n).joinpath(*parts[1:])
        check_destination(target)
        if target.exists():
            if not target.is_file() or digest(target) != info['sha256']:
                raise MigrationError('Restore would overwrite a different existing file.')
        else:
            plan.append((bundle / rel, target))
    # Every path/hash/conflict is checked before the first write. Failure keeps a
    # marker; never auto-delete an interrupted restoration or overwrite its files.
    marker = root / '.migration' / 'RESTORE_INCOMPLETE'
    check_destination(marker)
    marker.parent.mkdir(exist_ok=True, mode=0o700)
    with marker.open('x') as stream: stream.write('Do not start services. Restore into a fresh location if interrupted.\n')
    for source, target in plan:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.chmod(str(target), 0o700 if os.access(str(source), os.X_OK) else 0o600)
        if digest(target) != manifest['files'][source.relative_to(bundle).as_posix()]['sha256']:
            raise MigrationError('Restored file checksum mismatch.')
    marker.unlink()
    return {'status': 'restored', 'files': len(plan), 'servicesStarted': False,
            'next': 'Run doctor. Keep the old PC stopped before starting n8n here.'}


def doctor(root, n8n):
    checks = []
    def add(name, ok): checks.append({'check': name, 'ok': bool(ok)})
    versions = json.loads((root / 'config/runtime-versions.json').read_text())
    add('python_3_8_or_newer', sys.version_info >= (3, 8))
    for tool in ('node', 'npm'):
        try:
            value = subprocess.check_output([tool, '--version'], stderr=subprocess.DEVNULL).decode().strip().lstrip('v')
        except (OSError, subprocess.CalledProcessError): value = ''
        add(tool + '_version', value == versions[tool])
    package = root / 'runtime/node_modules/n8n/package.json'
    add('project_n8n_version', package.exists() and json.loads(package.read_text())['version'] == versions['n8n'])
    try:
        native = subprocess.run(['node', str(root / 'scripts/check_runtime.cjs')],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired): native = False
    add('sqlite_native_driver', native)
    values = env_values(root / '.env')
    add('env_exists', (root / '.env').is_file())
    add('n8n_api_key_configured', bool(values.get('N8N_API_KEY')))
    token = values.get('AUTOARTICLE_DB_SERVICE_TOKEN', '')
    add('db_service_token_configured', len(token) >= 32 and not any(c.isspace() for c in token))
    add('db_service_url', values.get('AUTOARTICLE_DB_SERVICE_URL') == 'http://127.0.0.1:8766')
    try: standard_layout(root); layout = True
    except ValueError: layout = False
    add('standard_layout', layout)
    add('restore_completed', not (root / '.migration/RESTORE_INCOMPLETE').exists())
    config = n8n / 'config'
    add('n8n_encryption_config', config.exists() and bool(json.loads(config.read_text()).get('encryptionKey')))
    add('n8n_database', (n8n / 'database.sqlite').is_file())
    path = root / 'data/autoarticle.sqlite'
    db_ok = False
    if path.is_file():
        try:
            c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                rows = dict(c.execute('SELECT feature,read_source FROM cutover_state'))
                db_ok = all(rows.get(k) == 'project-db' for k in ('dashboard', 'weekly', 'ai-reader', 'n8n-daily'))
            finally: c.close()
        except sqlite3.Error: pass
    add('project_database_read_routes', db_ok)
    return {'status': 'ready' if all(c['ok'] for c in checks) else 'needs_setup', 'checks': checks,
            'scope': 'offline configuration only; verify API authentication and workflow state after startup'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    export = sub.add_parser('export')
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--writers-stopped', action='store_true')
    verify = sub.add_parser('verify')
    verify.add_argument('--bundle', type=Path, required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--bundle', type=Path, required=True)
    restore.add_argument('--writers-stopped', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    n8n = Path.home() / '.n8n'
    try:
        if args.command == 'doctor': result = doctor(ROOT, n8n)
        elif args.command == 'export': result = export_bundle(ROOT, n8n, args.output.absolute(), args.writers_stopped)
        elif args.command == 'restore': result = restore_bundle(ROOT, n8n, args.bundle.absolute(), args.writers_stopped)
        else:
            m = verify_bundle(args.bundle.absolute())
            result = {'status': 'verified', 'files': len(m['files']), 'commit': m['commit'],
                      'manifestSha256': digest(args.bundle / 'manifest.json')}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] != 'needs_setup' else 2
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        # Exceptions from SQL, subprocesses and files may contain private data.
        message = str(error) if isinstance(error, MigrationError) else 'Transfer failed (' + type(error).__name__ + '); no services were started.'
        print(json.dumps({'status': 'blocked', 'reason': message}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
