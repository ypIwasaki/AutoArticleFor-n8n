"""DB-first transactions followed by durable, idempotent compatibility delivery.

Only trusted business adapters build changes/deliveries. No arbitrary SQL/file
endpoint is exposed. Pending automatic delivery is NOT success. On-demand exports remain deferred.
"""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import tempfile
import project_database as db
import continuous_database_sync as sync
import project_legacy_retirement as retirement

VERSION = 'project-write-v1'

class WriteStopped(RuntimeError):
    pass


def file_hash(path):
    return db.checksum(path.read_bytes()) if path.exists() else None


def safe_file(root, relative):
    root = Path(root).resolve()
    p = root / relative
    if not isinstance(relative, str) or not relative.startswith('content/') or '..' in Path(relative).parts:
        raise ValueError('Invalid compatibility file')
    if root not in p.resolve().parents:
        raise ValueError('Compatibility file escapes project')
    return p


class Unit:
    def __init__(self, c, operation_id, root, fault=None):
        self.c, self.operation_id, self.root, self.fault = c, operation_id, Path(root), fault
        self.added = self.updated = self.existing = self.held = self.ordinal = 0

    def save(self, entity, row, source):
        table = sync.ENTITIES[entity]
        info = list(self.c.execute('PRAGMA table_info(' + table + ')'))
        keys = sync.primary_key(self.c, table)
        key = {k: row[k] for k in keys}
        before = sync.read_row(self.c, entity, key)
        after = dict(before) if before else {x['name']: self.c.execute('SELECT ' + x['dflt_value']).fetchone()[0] if x['dflt_value'] else None for x in info}
        if set(row) - set(after): raise ValueError('Unknown business field')
        after.update(row)
        change = dict(entity=entity,key=key,before=before,after=after,source=source)
        sync.validate_change(self.c, change)
        if before == after:
            self.existing += 1
            return after
        sync.apply_row(self.c, change)
        # One operation may update a current flag after creating a row: audit its
        # original before and final after, without dropping intermediate source.
        audit = self.c.execute('SELECT old_json,source_json FROM sync_row_history WHERE operation_id=? AND entity=? AND row_key=?', (self.operation_id,entity,db.canonical(key))).fetchone()
        old = audit['old_json'] if audit else (db.canonical(before) if before is not None else None)
        self.c.execute('INSERT OR REPLACE INTO sync_row_history VALUES (?,?,?,?,?,?)',
            (self.operation_id,entity,db.canonical(key),old,db.canonical(after),db.canonical(source)))
        self.added += before is None
        self.updated += before is not None
        self.held += after.get('state') == 'held' or str(after.get('body_integrity','')).startswith('held_')
        if self.fault: self.fault('business', self.added + self.updated)
        return after

    def file(self, relative, text):
        p = safe_file(self.root, relative)
        self.delivery('file', relative, {'text':text}, file_hash(p))

    def file_reference(self, relative, reference):
        self.delivery('file',relative,reference,file_hash(safe_file(self.root,relative)))

    def delivery(self, kind, target, payload, previous_hash=None):
        if kind=='file' and self.c.execute('SELECT 1 FROM compatibility_deliveries WHERE operation_id=? AND kind=? AND target=?',(self.operation_id,kind,target)).fetchone():raise ValueError('Duplicate compatibility file in operation')
        self.c.execute('INSERT INTO compatibility_deliveries(operation_id,ordinal,kind,target,payload_json,payload_hash,previous_hash) VALUES (?,?,?,?,?,?,?)',
            (self.operation_id,self.ordinal,kind,target,db.canonical(payload),sync.digest(payload),previous_hash))
        retirement.schedule(self.c,self.operation_id,self.ordinal,kind,target,payload)
        self.ordinal += 1


def status(operation_id, path=None):
    with closing(db.connect(path,readonly=True)) as c:
        row = c.execute('SELECT id,status,outcome,result_json,request_hash FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
        if not row: return None
        return dict(row, dbCommitted=bool(c.execute('SELECT 1 FROM project_write_requests WHERE operation_id=?',(operation_id,)).fetchone()), pendingDeliveries=retirement.pending(c,operation_id), deferredDeliveries=retirement.pending(c,operation_id,False)-retirement.pending(c,operation_id))


def commit(operation_id, request, path, root, build, deliver=None, fault=None):
    if not isinstance(operation_id,str) or not db.ID_PATTERN.fullmatch(operation_id): raise ValueError('Stable operation ID required')
    if request.get('version') != VERSION: raise ValueError('Unsupported write contract')
    request_hash = sync.digest(request)
    with closing(db.connect(path)) as c:
        # Reserve the ID before business work so even a failed first attempt cannot
        # be retried with a different payload. Only audit survives business failure.
        with db.transaction(c):
            existing = c.execute('SELECT * FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
            conflict = bool(existing and existing['request_hash'] != request_hash)
            if conflict:
                sync.event(c,operation_id,request_hash,'conflict',{'reason':'operation_id_content_conflict'})
            elif not existing:
                c.execute("INSERT INTO sync_runs(id,request_hash,operation,status,started_at) VALUES (?,?,?,'running',?)", (operation_id,request_hash,'project-write:'+request['kind'],db.now()))
        if conflict: raise db.Conflict('operation_id_content_conflict')
        try:
            with db.transaction(c):
                current = c.execute('SELECT * FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
                if current['status'] == 'complete':
                    c.execute('UPDATE sync_runs SET replay_count=replay_count+1 WHERE id=?',(operation_id,))
                    sync.event(c,operation_id,request_hash,'replayed',{'additionalRows':0})
                    return json.loads(current['result_json'])
                committed = c.execute('SELECT 1 FROM project_write_requests WHERE operation_id=?',(operation_id,)).fetchone()
                if not committed:
                    if retirement.mode(c)=='maintenance':raise WriteStopped('legacy_delivery_maintenance')
                    if c.execute("SELECT 1 FROM compatibility_deliveries d WHERE d.status='pending' AND d.operation_id!=? AND "+retirement.AUTOMATIC+" LIMIT 1",(operation_id,)).fetchone():
                        raise WriteStopped('pending_compatibility_must_be_resumed_first')
                    unit = Unit(c,operation_id,root,fault)
                    # The FK for deliveries is deferred by creating the parent first;
                    # any adapter/compare failure rolls this parent back as well.
                    c.execute('INSERT INTO project_write_requests VALUES (?,?,?,?)',(operation_id,VERSION,db.canonical(request),db.now()))
                    build(unit,request)
                    for a in c.execute('SELECT * FROM sync_row_history WHERE operation_id=?',(operation_id,)):
                        if sync.read_row(c,a['entity'],json.loads(a['row_key'])) != json.loads(a['new_json']):
                            raise WriteStopped('business_save_comparison_failed')
                    if c.execute('PRAGMA foreign_key_check').fetchone(): raise WriteStopped('foreign_key_violation')
                    result=dict(operationId=operation_id,successCount=unit.added+unit.updated,added=unit.added,updatedCount=unit.updated,existingCount=unit.existing,heldCount=unit.held,writeTarget='project-db',requestHash=request_hash)
                    c.execute("UPDATE sync_runs SET status='running',outcome='pending',result_json=? WHERE id=?",(db.canonical(result),operation_id))
            if fault: fault('after_database_commit',0)
            return drain(operation_id,path,root,deliver,fault)
        except Exception as exc:
            with db.transaction(c):
                current=c.execute('SELECT status FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
                if current['status']!='complete':
                    c.execute("UPDATE sync_runs SET status='failed',outcome='failure' WHERE id=?",(operation_id,))
                    sync.event(c,operation_id,request_hash,'failure',{'reason':str(exc) if isinstance(exc,WriteStopped) else type(exc).__name__})
            raise


def deliver_file(row, root, database=None):
    p = safe_file(root,row['target']); payload=json.loads(row['payload_json'])
    if sync.digest(payload)!=row['payload_hash']: raise WriteStopped('outbox_payload_hash_changed')
    if 'text' in payload:text=payload['text']
    else:
        from project_business_writes import render_compatibility_file
        text=render_compatibility_file(payload,database)
    wanted=db.checksum(text.encode())
    current=file_hash(p)
    if current==wanted: return  # response may have been lost after the atomic rename
    if current!=row['previous_hash']: raise WriteStopped('compatibility_input_changed')
    p.parent.mkdir(parents=True,exist_ok=True)
    fd, name=tempfile.mkstemp(prefix='.'+p.name+'.',dir=str(p.parent))
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(text.encode());out.flush();os.fsync(out.fileno())
        if file_hash(p)!=row['previous_hash']:raise WriteStopped('compatibility_input_changed')
        os.replace(name,str(p))
        directory=os.open(str(p.parent),os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)
    if file_hash(p)!=wanted: raise WriteStopped('compatibility_verification_failed')


def drain(operation_id,path,root,deliver=None,fault=None):
    # Serialize delivery across service threads and Python commands. This tiny
    # lock is on the DB filesystem; backup/scratch artifacts remain on D:.
    lock=Path(path).with_suffix('.write.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        with closing(db.connect(path)) as c:
            completed=c.execute('SELECT status,result_json FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
            if completed and completed['status']=='complete':return json.loads(completed['result_json'])
            rows=c.execute("SELECT d.* FROM compatibility_deliveries d WHERE d.operation_id=? AND d.status='pending' AND "+retirement.AUTOMATIC+" ORDER BY d.ordinal",(operation_id,)).fetchall()
            if not c.execute('SELECT 1 FROM project_write_requests WHERE operation_id=?',(operation_id,)).fetchone():raise WriteStopped('database_not_committed')
            for row in rows:
                if row['kind']=='file':deliver_file(row,root,path)
                elif deliver:deliver(dict(row))
                else:raise WriteStopped('compatibility_adapter_unavailable')
                if fault:fault('after_delivery',row['ordinal'])
                with db.transaction(c):
                    c.execute("UPDATE compatibility_deliveries SET status='complete',completed_at=? WHERE operation_id=? AND ordinal=?",(db.now(),operation_id,row['ordinal']))
            with db.transaction(c):
                row=c.execute('SELECT * FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
                result=json.loads(row['result_json']);result['compatibility']='on-demand' if retirement.pending(c,operation_id,False) else 'complete'
                c.execute("UPDATE sync_runs SET status='complete',outcome='success',completed_at=?,result_json=? WHERE id=?",(db.now(),db.canonical(result),operation_id))
                sync.event(c,operation_id,row['request_hash'],'success',result)
            return result


def set_write_target(feature,target,path=None):
    if target not in ('legacy','project-db'):raise ValueError('Invalid write target')
    with closing(db.connect(path)) as c, db.transaction(c):
        if c.execute("SELECT 1 FROM compatibility_deliveries WHERE status!='complete' LIMIT 1").fetchone():
            raise WriteStopped('compatibility_pending_cannot_switch')
        if c.execute('UPDATE cutover_state SET write_target=?,changed_at=?,rollback_state=? WHERE feature=?', (target,db.now(),'rolled-back' if target=='legacy' else 'verified',feature)).rowcount!=1:
            raise ValueError('Unknown feature')
