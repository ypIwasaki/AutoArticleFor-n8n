"""Read-only comparison/planning adapter for verified legacy snapshots.

Projection is isolated from the live project DB. A source snapshot is retained
for every request, and only a completed legacy receipt authorizes application.
"""
from contextlib import closing
from pathlib import Path
import json
import project_database as db
import import_legacy_database as imp
import database_phase1 as baseline
import continuous_database_sync as sync

ORDER = ['article','identifier','collection','occurrence','body','bodyVersion','fetchAttempt',
         'review','taskStatus','fact','entity','evidence','factEvidence','entityEvidence','entityFact',
         'summary','talent','alias','relationship','classification','secondaryCategory','feedback',
         'sourceRecord','provenance','identityAssessment','history','runtime','reviewInput','conflict']


def rows(c,entity):
    table=sync.ENTITIES[entity];keys=sync.primary_key(c,table)
    return {db.canonical({k:r[k] for k in keys}):dict(r) for r in c.execute('SELECT * FROM '+table)}


def plan(candidate, target, receipt):
    """All persisted fields are compared; row count equality is insufficient."""
    changes=[]
    with closing(db.connect(candidate,readonly=True)) as new,closing(db.connect(target,readonly=True)) as old:
        source_rows=[dict(r) for r in new.execute('SELECT * FROM source_records')]
        target_sources={}
        for row in source_rows:
            target_sources.setdefault((row['target_kind'],row['target_id']),[]).append(row)
        source_by_id={r['id']:r for r in source_rows}
        # Disappearing input locations require an explicit decision. Historical
        # versions at a still-present location remain in the destination.
        current_locations={(r['source_path'],r['record_position']) for r in source_rows}
        previous_locations={(r['source_path'],r['record_position']) for r in old.execute('SELECT source_path,record_position FROM source_records')}
        if previous_locations-current_locations:
            raise sync.SyncStopped('source_locations_removed')
        for entity in ORDER:
            previous=rows(old,entity);projected=rows(new,entity)
            if entity in ('alias','secondaryCategory') and set(previous)-set(projected):
                raise sync.SyncStopped('current_child_rows_removed_requires_history_policy:'+entity)
            for key,after in projected.items():
                before=previous.get(key)
                if entity=='sourceRecord':
                    after['migration_run_id']=before['migration_run_id'] if before else None
                    if before:after['importer_version']=before['importer_version']
                if entity=='conflict' and before:
                    # Keep approved investigation additions on existing conflicts.
                    after=before
                if after==before:continue
                sources=target_sources.get((sync.ENTITIES[entity],after.get('id')),[])
                if entity=='sourceRecord':sources=[after]
                if 'source_record_id' in after and after['source_record_id'] in source_by_id:sources=[source_by_id[after['source_record_id']]]
                if sources:
                    raw=[json.loads(x['raw_json']) for x in sources]
                    origin={'path':sources[0]['source_path'],'position':sources[0]['record_position'],'old_key':None,'raw':raw[0] if len(raw)==1 else raw}
                    if isinstance(origin['raw'],dict):origin['old_key']=origin['raw'].get('article_key',origin['raw'].get('articleKey'))
                else:
                    # Derived child rows are traceable through the immutable
                    # projection snapshot and FK parents, never a guessed URL.
                    origin={'path':'snapshot:'+receipt['snapshot'],'position':entity+':'+key,'old_key':None,'raw':{'row':after,'snapshot_hash':receipt['snapshot_hash']}}
                origin['input_hash']=sync.digest(origin['raw'])
                changes.append({'entity':entity,'key':json.loads(key),'before':before,'after':after,'source':origin})
    return {'version':sync.VERSION,'receipt':receipt,'changes':changes}


def compare_scope(c,request):
    differences=[]
    for change in request['changes']:
        actual=sync.read_row(c,change['entity'],change['key'])
        if actual!=change['after']:
            differences.append({'entity':change['entity'],'key':change['key'],'reason':'projected_source_value_mismatch'})
    return differences


def verify_snapshot(receipt):
    snapshot=Path(receipt['snapshot']);manifest=json.loads((snapshot/'input-files.json').read_text())
    meta=json.loads((snapshot/'phase1-result.json').read_text())
    if meta['status']!='complete' or baseline.sha(snapshot/'n8n.sqlite')!=meta['backup_sha256']:
        raise sync.SyncStopped('snapshot_database_hash_changed')
    actual=sync.digest({'database':meta['backup_sha256'],'files':manifest})
    if actual!=receipt['snapshot_hash']:raise sync.SyncStopped('snapshot_manifest_changed')
    for item in manifest:
        if baseline.sha(snapshot/'files'/item['path'])!=item['sha256']:
            raise sync.SyncStopped('snapshot_file_changed')
    evidence=json.loads((snapshot/'legacy-completion.json').read_text())
    if evidence!=receipt['legacy_completion'] or evidence.get('status')!='complete':
        raise sync.SyncStopped('legacy_completion_not_verified')
    if evidence['kind']=='n8n-execution':
        with closing(baseline.ro(snapshot/'n8n.sqlite')) as c:
            row=c.execute('SELECT status,stoppedAt FROM execution_entity WHERE id=?',(evidence['execution_id'],)).fetchone()
            if not row or row['status']!='success' or not row['stoppedAt']:
                raise sync.SyncStopped('legacy_execution_not_successful')
    elif evidence['kind']=='validated-checkpoint':
        saved=json.loads((snapshot/'operation-state.json').read_text())
        entry=saved['steps'].get(evidence['step'],{})
        if entry.get('status')!='completed' or sync.digest(entry)!=evidence['entry_hash']:
            raise sync.SyncStopped('legacy_checkpoint_not_complete')
    else:raise ValueError('Unsupported completion evidence')


def build_projection(snapshot,candidate,target,receipt):
    # The migration's fallback timestamp is an identity/history baseline, not
    # the time this synchronization happens. Do not reset historic timestamps.
    with closing(db.connect(target,readonly=True)) as old:
        row=old.execute('SELECT input_snapshot FROM migration_runs ORDER BY started_at LIMIT 1').fetchone()
    stamp=receipt['completed_at']
    if row:
        meta=Path(row[0])/'phase1-result.json'
        if not meta.exists():raise sync.SyncStopped('original_projection_epoch_unavailable')
        stamp=json.loads(meta.read_text())['finished_at']
    db.migrate(candidate)
    manifest=json.loads((Path(snapshot)/'input-files.json').read_text())
    files=[x for x in manifest if any(x['path'].startswith('content/'+d+'/') for d in baseline.TARGETS)]
    with closing(db.connect(candidate)) as c,db.transaction(c):
        run_id=imp.ident('sync-projection',receipt['snapshot_hash'])
        c.execute("INSERT INTO migration_runs(id,input_snapshot,importer_version,started_at,status) VALUES (?,?,?,?,'running')",(run_id,str(snapshot),sync.VERSION,db.now()))
        engine=imp.Importer(c,Path(snapshot),run_id,imp.utc(stamp),files)
        for stage in ('preload','archive_files','structured','n8n','contents','reviews','summaries','histories'):
            getattr(engine,stage)()
        engine.verify()
        c.execute("UPDATE migration_runs SET status='complete',completed_at=? WHERE id=?",(db.now(),run_id))


def synchronize_snapshot(operation_id,snapshot,target,candidate,request_path,verify_live):
    snapshot=Path(snapshot);request_path=Path(request_path)
    if request_path.exists():
        request=json.loads(request_path.read_text())
        if request['receipt']['snapshot']!=str(snapshot.resolve()):raise sync.SyncStopped('operation_snapshot_changed')
    else:
        meta=json.loads((snapshot/'phase1-result.json').read_text());manifest=json.loads((snapshot/'input-files.json').read_text())
        evidence=json.loads((snapshot/'legacy-completion.json').read_text())
        receipt={'status':'complete','completed_at':evidence['completed_at'],'snapshot':str(snapshot.resolve()),
                 'snapshot_hash':sync.digest({'database':meta['backup_sha256'],'files':manifest}),'legacy_completion':evidence}
        if evidence.get('operation_id')!=operation_id:raise sync.SyncStopped('completion_operation_id_mismatch')
        verify_snapshot(receipt);verify_live(receipt)
        candidate=Path(candidate)
        if candidate.exists():raise sync.SyncStopped('unclassified_existing_projection')
        build_projection(snapshot,candidate,target,receipt)
        request=plan(candidate,target,receipt)
        request_path.write_text(db.canonical(request)+'\n')
    def guard(receipt):
        verify_snapshot(receipt);verify_live(receipt)
    return sync.synchronize(operation_id,request,target,guard,compare_scope)
