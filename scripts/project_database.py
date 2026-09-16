"""Common SQLite access layer for project data. No legacy fallback."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / 'database/migrations'
BACKUP_VERSION = 'standalone-backup-v2-delete-journal'
ENTITIES = {
 'article': 'articles', 'identifier': 'article_identifiers',
 'collection': 'collection_runs', 'occurrence': 'article_occurrences',
 'body': 'content_payloads', 'bodyVersion': 'article_content_versions', 'fetchAttempt': 'content_fetch_attempts',
 'review': 'review_records', 'taskStatus': 'review_task_statuses', 'evidence': 'review_evidence',
 'fact': 'review_facts', 'factEvidence': 'review_fact_evidence', 'entity': 'review_entities',
 'entityEvidence': 'review_entity_evidence', 'summary': 'article_summaries',
 'talent': 'talents', 'alias': 'talent_aliases', 'relationship': 'article_talents',
 'classification': 'article_classifications', 'secondaryCategory': 'classification_secondary_categories',
 'feedback': 'article_feedback',
}
OPERATIONS = {
 'articles': ('article','identifier','collection','occurrence'),
 'contents': ('body','bodyVersion','fetchAttempt'),
 'reviews': ('review','taskStatus','evidence','fact','factEvidence','entity','entityEvidence','summary'),
 'talent-proposals': ('talent','alias','relationship'),
 'classifications': ('classification','secondaryCategory'),
 'feedback': ('feedback',),
}
ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$')

class Conflict(ValueError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')

def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)

def checksum(value):
    return hashlib.sha256(value).hexdigest()

def database_path():
    value=Path(os.environ.get('AUTOARTICLE_DATABASE_PATH',str(ROOT/'data/autoarticle.sqlite'))).expanduser().resolve()
    if '.n8n' in value.parts or value == (Path.home()/'.n8n/database.sqlite').resolve():
        raise ValueError('Project database must not be the n8n database')
    return value

def connect(path=None,readonly=False):
    path=Path(path or database_path()).resolve()
    if '.n8n' in path.parts:
        raise ValueError('Legacy database is not a project database')
    if not readonly:
        path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,isolation_level=None,timeout=10)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=10000')
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND (name='data_table' OR name='workflow_entity' OR name='credentials_entity')").fetchone():
            raise ValueError('Legacy database is not a project database')
        if readonly:
            c.execute('PRAGMA query_only=ON')
        else:
            if c.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()!='wal':
                raise RuntimeError('WAL unavailable')
            c.execute('PRAGMA synchronous=FULL')
        if c.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
            raise RuntimeError('Foreign keys unavailable')
        return c
    except BaseException:
        c.close()
        raise

@contextmanager
def transaction(c):
    c.execute('BEGIN IMMEDIATE')
    try:
        yield c
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise

def statements(sql):
    buffer=''
    for line in sql.splitlines(keepends=True):
        buffer+=line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer=''
    if buffer.strip():
        raise ValueError('Incomplete migration statement')

def migrate(path=None,directory=MIGRATIONS):
    c=connect(path)
    try:
        with transaction(c):
            has=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            applied={r['version']:r['checksum'] for r in c.execute('SELECT version,checksum FROM schema_migrations')} if has else {}
            files=sorted(Path(directory).glob('[0-9][0-9][0-9]_*.sql'))
            versions=[p.name.split('_')[0] for p in files]
            if not files or len(set(versions))!=len(versions) or set(applied)-set(versions):
                raise ValueError('Migration versions unavailable or ambiguous')
            added=[]
            for p,version in zip(files,versions):
                raw=p.read_bytes()
                h=checksum(raw)
                if version in applied:
                    if applied[version]!=h: raise ValueError('Applied migration checksum changed')
                    continue
                if applied and version<max(applied): raise ValueError('Out of order migration')
                for statement in statements(raw.decode('utf-8')): c.execute(statement)
                c.execute('INSERT INTO schema_migrations(version,applied_at,checksum) VALUES (?,?,?)',(version,now(),h))
                added.append(version)
            for feature in ('comparison','dashboard','weekly','ai-reader','n8n-daily'):
                c.execute('INSERT OR IGNORE INTO cutover_state(feature,changed_at) VALUES (?,?)',(feature,now()))
        return dict(applied=added)
    finally:
        c.close()

def validate_rows(c,operation,records):
    if operation not in OPERATIONS or not isinstance(records,dict) or not records:
        raise ValueError('Invalid business operation')
    if set(records)-set(OPERATIONS[operation]): raise ValueError('Unknown business entity')
    count=0
    for entity,rows in records.items():
        if not isinstance(rows,list): raise ValueError('Records must be arrays')
        columns={r['name']:r for r in c.execute('PRAGMA table_info('+ENTITIES[entity]+')')}
        for row in rows:
            count+=1
            if not isinstance(row,dict) or not row or set(row)-set(columns): raise ValueError('Invalid record fields')
            for key,value in row.items():
                if key.endswith('_json'):
                    if not isinstance(value,str): raise ValueError('JSON fields require serialized JSON')
                    canonical(json.loads(value))
                if key.endswith(('_at','_after')) and value is not None:
                    if not isinstance(value,str) or not value.endswith('Z'): raise ValueError('UTC timestamp required')
                    datetime.fromisoformat(value[:-1]+'+00:00')
                if (key=='id' or key.endswith('_id')) and value is not None:
                    if not isinstance(value,str) or not value: raise ValueError('Text ID required')
            if entity=='talent' and (row.get('status')!='pending' or row.get('search_enabled')!=0):
                raise ValueError('Proposals cannot approve or enable talent')
            if entity=='relationship' and row.get('state') not in ('proposed','held'):
                raise ValueError('Proposals cannot become current relationships')
            if entity=='body':
                if row.get('text_hash')!=checksum(row.get('text','').encode()): raise ValueError('Body hash mismatch')
                fields={k:v for k,v in row.items() if k not in ('id','payload_hash','text_hash')}
                if row.get('payload_hash')!=checksum(canonical(fields).encode()): raise ValueError('Payload hash mismatch')
    if count<1 or count>1000: raise ValueError('Operation size outside allowed range')

def save_operation(operation,operation_id,records,path=None):
    if not isinstance(operation_id,str) or not ID_PATTERN.fullmatch(operation_id):
        raise ValueError('Invalid operation ID')
    request_hash=checksum(canonical(dict(operation=operation,records=records)).encode())
    c=connect(path)
    try:
        with transaction(c):
            existing=c.execute('SELECT request_hash,status,result_json FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
            if existing:
                if existing['request_hash']!=request_hash or existing['status']!='complete': raise Conflict('Operation ID conflict')
                return json.loads(existing['result_json'])
            validate_rows(c,operation,records)
            c.execute("INSERT INTO sync_runs(id,request_hash,operation,status,started_at) VALUES (?,?,?,'running',?)",(operation_id,request_hash,operation,now()))
            count=0
            held=0
            for entity in OPERATIONS[operation]:
                for row in records.get(entity,[]):
                    table=ENTITIES[entity]
                    columns=','.join('"'+k+'"' for k in row)
                    marks=','.join('?' for k in row)
                    c.execute(f'INSERT INTO {table}({columns}) VALUES ({marks})',tuple(row.values()))
                    count+=1
                    held+=row.get('status')=='held' or row.get('state')=='held'
            result=dict(operationId=operation_id,successCount=count,updatedCount=0,existingCount=0,heldCount=held)
            c.execute("UPDATE sync_runs SET status='complete',completed_at=?,result_json=? WHERE id=?",(now(),canonical(result),operation_id))
        return result
    finally:
        c.close()

def status(path=None):
    c=connect(path,readonly=True)
    try:
        return dict(schema=[dict(r) for r in c.execute('SELECT * FROM schema_migrations ORDER BY version')],
                    cutover=[dict(r) for r in c.execute('SELECT * FROM cutover_state ORDER BY feature')],
                    unresolvedConflicts=c.execute("SELECT count(*) FROM consolidation_conflicts WHERE status='unresolved'").fetchone()[0])
    finally:
        c.close()

def _backup_connection(destination):
    c=sqlite3.connect(destination,isolation_level=None,timeout=10)
    try:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=10000')
        if c.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete':
            raise RuntimeError('Standalone backup journal mode unavailable')
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA cache_size=-131072')
        return c
    except Exception:
        c.close()
        raise

def backup(destination,path=None):
    destination=Path(destination).resolve()
    if '.n8n' in destination.parts:
        raise ValueError('Legacy directory cannot be a project backup destination')
    if destination.exists(): raise ValueError('Backup destination already exists')
    source=connect(path,readonly=True)
    target=None
    try:
        destination.parent.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(str(destination),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(descriptor)
        # Recovery copies are standalone files, never live WAL databases.
        # Keep durable SQLite transactions and foreign-key enforcement, while
        # avoiding a large WAL and shared-memory index on Windows-mounted storage.
        target=_backup_connection(destination)
        foreign_keys={
            'source': source.execute('PRAGMA foreign_keys').fetchone()[0],
            'destination': target.execute('PRAGMA foreign_keys').fetchone()[0],
        }
        if any(value!=1 for value in foreign_keys.values()):
            raise RuntimeError('Foreign keys unavailable for backup')
        source.backup(target)
        # Reopen to recognize the copied source header, then persist DELETE
        # mode before validating. Every reopened connection also enforces FKs.
        target.close()
        target=_backup_connection(destination)
        if [r[0] for r in target.execute('PRAGMA integrity_check')]!=['ok']:
            raise RuntimeError('Backup integrity failure')
        if target.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Backup references invalid')
        if target.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
            raise RuntimeError('Foreign keys unavailable after backup')
    finally:
        source.close()
        if target is not None: target.close()
    return dict(path=str(destination),sha256=checksum(destination.read_bytes()),foreign_keys=foreign_keys,backup_version=BACKUP_VERSION)

def restore_check(snapshot,destination):
    result=backup(destination,path=snapshot)
    c=connect(destination,readonly=True)
    try:
        if c.execute('PRAGMA foreign_key_check').fetchall(): raise RuntimeError('Restored references invalid')
        result['integrity']='ok'
        result['schema']=status(destination)['schema']
    finally: c.close()
    return result
