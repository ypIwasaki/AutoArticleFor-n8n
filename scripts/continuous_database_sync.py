"""Transactional legacy-to-project synchronization. No legacy writes or fallback.

The adapter supplies a verified completion receipt and compare callback. This
module is not an API for caller-provided SQL or an implicit dual-write hook.
"""
from contextlib import closing
import json
import uuid
import project_database as db

VERSION = 'continuous-sync-v1'
ENTITIES = dict(db.ENTITIES, sourceRecord='source_records', provenance='article_source_provenance',
    identityAssessment='article_identity_assessments', history='legacy_history_records',
    runtime='content_runtime_state', reviewInput='review_input_snapshots',
    entityFact='review_entity_facts', conflict='consolidation_conflicts')
IMMUTABLE = {'body','fetchAttempt','review','evidence','fact','entity','sourceRecord',
             'provenance','identityAssessment','history','reviewInput','conflict'}

class SyncStopped(RuntimeError):
    pass


def digest(value):
    return db.checksum(db.canonical(value).encode())


def primary_key(c, table):
    columns = list(c.execute('PRAGMA table_info(' + table + ')'))
    return [r['name'] for r in sorted(columns, key=lambda r:r['pk']) if r['pk']]


def read_row(c, entity, key):
    table = ENTITIES[entity]
    pk = primary_key(c, table)
    if set(key) != set(pk):
        raise ValueError('Complete primary key required')
    where = ' AND '.join('"'+k+'"=?' for k in pk)
    row = c.execute('SELECT * FROM '+table+' WHERE '+where, [key[k] for k in pk]).fetchone()
    return dict(row) if row else None


def validate_change(c, change):
    if set(change) != {'entity','key','before','after','source'}:
        raise ValueError('Invalid change envelope')
    entity = change['entity']
    if entity not in ENTITIES:
        raise ValueError('Unknown synchronization entity')
    after, before, source = change['after'], change['before'], change['source']
    if not isinstance(after,dict) or not isinstance(source,dict):
        raise ValueError('Deletion and missing provenance are forbidden')
    if set(source) != {'path','position','old_key','raw','input_hash'} or digest(source['raw']) != source['input_hash']:
        raise ValueError('Source row and input hash required')
    columns = {r['name'] for r in c.execute('PRAGMA table_info('+ENTITIES[entity]+')')}
    if set(after) != columns:
        raise ValueError('Full projected row required')
    if any(after.get(k) != v for k,v in change['key'].items()):
        raise ValueError('Stable primary key required')
    if read_row(c,entity,change['key']) != before:
        raise SyncStopped('target_changed_before_sync')
    if before and before != after and entity in IMMUTABLE:
        raise SyncStopped('immutable_history_requires_new_record')
    if before:
        for field, held in [('identity_state',{'held','needs_review'}),('state',{'held','proposed'}),('status',{'held','needs_review','pending','unverified','partial','unavailable'})]:
            if before.get(field) in held and after.get(field) != before[field]:
                raise SyncStopped('held_or_unverified_state_change_requires_explicit_review')
    if entity=='body':
        if db.checksum(after['text'].encode()) != after['text_hash']:
            raise SyncStopped('invalid_body_payload')
        fields={k:v for k,v in after.items() if k not in ('id','payload_hash','text_hash')}
        if digest(fields)!=after['payload_hash']:
            raise SyncStopped('invalid_payload_hash')
    if entity=='fetchAttempt':
        import import_legacy_database as legacy
        raw = source['raw']; integrity = legacy.body_integrity(raw)
        if after['body_integrity'] != integrity:
            raise SyncStopped('body_integrity_difference')
        if integrity.startswith('held_') and (after['version_id'] is not None or after['status']!='unverified'):
            raise SyncStopped('missing_body_requires_null_version')
        values=legacy.content_values(raw)
        if values['text'] and values['stored_length'] is not None and len(values['text'])!=values['stored_length']:
            raise SyncStopped('body_length_disagrees_with_payload')
        if after['source_status']!=values['status'] or after['stored_body_hash']!=values['stored_hash'] or json.loads(after['raw_json'])!=raw:
            raise SyncStopped('saved_body_claim_difference')
        if after['stored_body_length'] != legacy.stored_length(raw):
            raise SyncStopped('saved_body_length_difference')
    return entity


def apply_row(c, change):
    entity, after = change['entity'], change['after']
    table = ENTITIES[entity]
    if change['before'] is None:
        columns = list(after)
        c.execute('INSERT INTO '+table+'('+','.join('"'+k+'"' for k in columns)+') VALUES ('+','.join('?' for _ in columns)+')',[after[k] for k in columns])
    elif after != change['before']:
        columns = [k for k in after if k not in change['key']]
        c.execute('UPDATE '+table+' SET '+','.join('"'+k+'"=?' for k in columns)+' WHERE '+ ' AND '.join('"'+k+'"=?' for k in change['key']),[after[k] for k in columns]+list(change['key'].values()))


def event(c, operation_id, request_hash, outcome, details):
    c.execute('INSERT INTO sync_attempts VALUES (?,?,?,?,?,?)',
              (str(uuid.uuid4()),operation_id,request_hash,outcome,db.now(),db.canonical(details)))


def synchronize(operation_id, request, path, verify_legacy, compare, fault=None):
    """Callbacks must read immutable source evidence and check live input stability.

    Failed attempts are audited after rolling back the entire business transaction.
    A retry keeps the original ID/hash. Result-unknown callers use status or retry.
    """
    if not isinstance(operation_id,str) or not db.ID_PATTERN.fullmatch(operation_id):
        raise ValueError('Stable operation ID required')
    if set(request) != {'version','receipt','changes'} or request['version'] != VERSION:
        raise ValueError('Unsupported synchronization contract')
    receipt=request['receipt']
    if receipt.get('status')!='complete' or not receipt.get('completed_at'):
        raise ValueError('Legacy completion required')
    request_hash=digest(request)
    with closing(db.connect(path)) as c:
        with db.transaction(c):
            existing=c.execute('SELECT * FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
            if existing and existing['request_hash'] != request_hash:
                event(c,operation_id,request_hash,'conflict',{'reason':'operation_id_content_conflict'})
                c.execute("UPDATE sync_runs SET outcome='conflict' WHERE id=?",(operation_id,))
                conflict=True
            else:
                conflict=False
                if existing and existing['outcome']=='conflict':
                    raise SyncStopped('unresolved_operation_id_conflict')
                if existing and existing['status']=='complete':
                    c.execute('UPDATE sync_runs SET replay_count=replay_count+1 WHERE id=?',(operation_id,))
                    return json.loads(existing['result_json'])
                if not existing:
                    c.execute("INSERT INTO sync_runs(id,request_hash,operation,status,started_at) VALUES (?,?,?,'running',?)",(operation_id,request_hash,'legacy-sync',db.now()))
        if conflict:
            raise SyncStopped('operation_id_content_conflict')
        try:
            verify_legacy(receipt)
            with db.transaction(c):
                # Serialize retries: another worker may have committed while the
                # source receipt was being checked.
                current=c.execute('SELECT * FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
                if current['request_hash']!=request_hash or current['outcome']=='conflict':
                    raise SyncStopped('operation_id_content_conflict')
                if current['status']=='complete':
                    c.execute('UPDATE sync_runs SET replay_count=replay_count+1 WHERE id=?',(operation_id,))
                    return json.loads(current['result_json'])
                seen=set(); added=updated=0
                for index,change in enumerate(request['changes']):
                    entity=validate_change(c,change)
                    key=(entity,db.canonical(change['key']))
                    if key in seen:raise ValueError('Duplicate row in operation')
                    seen.add(key)
                    apply_row(c,change)
                    c.execute('INSERT INTO sync_row_history VALUES (?,?,?,?,?,?)',(operation_id,entity,key[1],db.canonical(change['before']) if change['before'] is not None else None,db.canonical(change['after']),db.canonical(change['source'])))
                    added+=change['before'] is None
                    updated+=change['before'] is not None and change['before']!=change['after']
                    if fault:fault(index)
                differences=compare(c,request)
                if differences:
                    raise SyncStopped('post_sync_comparison_difference')
                verify_legacy(receipt)
                if c.execute('PRAGMA foreign_key_check').fetchall():raise SyncStopped('foreign_key_violation')
                result={'operationId':operation_id,'added':added,'updated':updated,'unexplainedDifferences':0,'requestHash':request_hash}
                c.execute('INSERT INTO sync_source_receipts VALUES (?,?,?,?)',(operation_id,receipt['completed_at'],digest(receipt),db.canonical(receipt)))
                c.execute("UPDATE sync_runs SET status='complete',outcome='success',completed_at=?,result_json=? WHERE id=?",(db.now(),db.canonical(result),operation_id))
                event(c,operation_id,request_hash,'success',result)
            return result
        except Exception as exc:
            reason=str(exc) if isinstance(exc,SyncStopped) else type(exc).__name__
            outcome='difference' if isinstance(exc,SyncStopped) else 'failure'
            with db.transaction(c):
                # Never erase a success committed by a competing retry.
                row=c.execute('SELECT status FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
                if row['status']!='complete':
                    c.execute("UPDATE sync_runs SET status='failed',outcome=?,result_json=? WHERE id=?",(outcome,db.canonical({'reason':reason}),operation_id))
                event(c,operation_id,request_hash,outcome,{'reason':reason})
            raise


def operation_status(operation_id,path=None):
    with closing(db.connect(path,readonly=True)) as c:
        row=c.execute('SELECT id,status,outcome,request_hash,replay_count,result_json FROM sync_runs WHERE id=?',(operation_id,)).fetchone()
        return dict(row) if row else None
