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
import sync_snapshot_dependencies as dependencies
import missing_body_acceptance as acceptance
import identity_assessment_history as identity_history
import missing_body_capture_history as capture_history

ORDER = ['article','identifier','collection','occurrence','body','bodyVersion','fetchAttempt',
         'review','taskStatus','fact','entity','evidence','factEvidence','entityEvidence','entityFact',
         'summary','talent','alias','relationship','classification','secondaryCategory','feedback',
         'sourceRecord','provenance','identityAssessment','history','runtime','reviewInput','conflict'] + list(identity_history.ENTITIES) + list(capture_history.ENTITIES)


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
            previous=rows(old,entity) if old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(sync.ENTITIES[entity],)).fetchone() else {};projected=rows(new,entity)
            if entity in ('alias','secondaryCategory') and set(previous)-set(projected):
                raise sync.SyncStopped('current_child_rows_removed_requires_history_policy:'+entity)
            for key,after in projected.items():
                before=previous.get(key)
                if entity=='sourceRecord':
                    after['migration_run_id']=before['migration_run_id'] if before else None
                    if before:after['importer_version']=before['importer_version']
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



def compare_snapshot_scope(c,request):
    """In addition to row equality, reject broken preserved acceptance contracts."""
    differences=compare_scope(c,request)
    snapshot=Path(request['receipt']['snapshot'])
    try:
        acceptance.plan(c,acceptance.load(snapshot/'files'/acceptance.LEDGER_PATH))
    except RuntimeError as exc:
        differences.append({'entity':'body_integrity','reason':str(exc),'approval_status':'unapproved'})
    if differences:return differences
    import uuid
    output=snapshot.parent/('identity-independent-'+str(uuid.uuid4()))
    try:
        identity_history.verify_database(c,identity_history.expected_processors(snapshot),output)
        capture_history.verify_database(c,capture_history.expected_scopes(snapshot,request['receipt']))
        from compare_sync_semantics import verify as semantic_verify
        semantic_verify(c,snapshot,output.parent/(output.name+'-semantics'))
    except RuntimeError as exc:
        differences.append({'entity':'identity_history','reason':str(exc),'approval_status':'unapproved'})
    return differences


def verify_snapshot(receipt):
    snapshot=Path(receipt['snapshot']);manifest=json.loads((snapshot/'input-files.json').read_text())
    meta=json.loads((snapshot/'phase1-result.json').read_text())
    if meta['status']!='complete' or baseline.sha(snapshot/'n8n.sqlite')!=meta['backup_sha256']:
        raise sync.SyncStopped('snapshot_database_hash_changed')
    dependency_identity=dependencies.input_identity(snapshot)
    actual=sync.digest(dependency_identity)
    if receipt.get('dependencies')!={k:dependency_identity[k] for k in ('policy_dependencies','implementation_dependencies')}:
        raise sync.SyncStopped('receipt_dependencies_changed')
    if actual!=receipt['snapshot_hash']:raise sync.SyncStopped('snapshot_manifest_changed')
    for item in manifest:
        dependencies.check_file(snapshot/'files',item)
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
    dependencies.verify(snapshot)
    from complete_sync_snapshot import verify_bundle
    verify_bundle(snapshot)
    decision_path=Path(snapshot)/'files'/acceptance.LEDGER_PATH
    ledger=acceptance.load(decision_path)
    with closing(db.connect(Path(snapshot)/'project.sqlite',readonly=True)) as original:
        acceptance.plan(original,ledger)
    meta=json.loads((Path(snapshot)/'phase1-result.json').read_text())
    stamp=meta['projection_epoch']
    candidate=Path(candidate)
    request_hash=sync.digest(dependencies.input_identity(snapshot))
    if receipt.get('snapshot_hash')!=request_hash:raise sync.SyncStopped('projection_input_identity_mismatch')
    if candidate.exists():
        with closing(db.connect(candidate,readonly=True)) as previous:
            row=previous.execute('SELECT status,result_json FROM migration_runs WHERE id=?',(imp.ident('sync-projection',request_hash),)).fetchone()
            if row and row['status']=='complete' and json.loads(row['result_json']).get('input_hash')==request_hash:
                return {'replayed':True,'added_rows':0}
        raise sync.SyncStopped('projection_replay_conflict')
    context=identity_history.Context(snapshot,candidate.parent/(candidate.stem+'-identity-proof'))
    capture_context=capture_history.Context(context)
    db.migrate(candidate)
    manifest=json.loads((Path(snapshot)/'input-files.json').read_text())
    files=[x for x in manifest if any(x['path'].startswith('content/'+d+'/') for d in baseline.TARGETS)]
    with closing(db.connect(candidate)) as c,db.transaction(c):
        c.execute('PRAGMA cache_size=-262144')
        run_id=imp.ident('sync-projection',request_hash)
        c.execute("INSERT INTO migration_runs(id,input_snapshot,importer_version,started_at,status) VALUES (?,?,?,?,'running')",(run_id,str(snapshot),sync.VERSION,db.now()))
        engine=imp.Importer(c,Path(snapshot),run_id,imp.utc(stamp),files,identity_context=context,missing_body_context=capture_context)
        for stage in ('preload','archive_files','structured','n8n','contents','reviews','summaries','histories'):
            getattr(engine,stage)()
        identity_result=context.persist(engine,receipt)
        engine.verify()
        accepted=acceptance.apply(c,ledger,baseline.sha(decision_path))
        reapplied=acceptance.apply(c,ledger,baseline.sha(decision_path))
        if reapplied['updated_rows']!=0:raise sync.SyncStopped('acceptance_replay_changed_rows')
        captures=capture_context.persist(c,receipt)
        capture_replay=capture_context.persist(c,receipt)
        if capture_replay['added_rows']!=0:raise sync.SyncStopped('capture_replay_changed_rows')
        dependencies.verify(snapshot)
        c.execute("UPDATE migration_runs SET status='complete',completed_at=?,result_json=? WHERE id=?",(db.now(),db.canonical({'input_hash':request_hash,'acceptance':accepted,'acceptance_replay':reapplied,'identity_history':identity_result,'capture_history':captures,'capture_replay':capture_replay}),run_id))
    return {'replayed':False}


def synchronize_snapshot(operation_id,snapshot,target,candidate,request_path,verify_live):
    snapshot=Path(snapshot);request_path=Path(request_path)
    if request_path.exists():
        request=json.loads(request_path.read_text())
        if request['receipt']['snapshot']!=str(snapshot.resolve()):raise sync.SyncStopped('operation_snapshot_changed')
    else:
        meta=json.loads((snapshot/'phase1-result.json').read_text());manifest=json.loads((snapshot/'input-files.json').read_text())
        evidence=json.loads((snapshot/'legacy-completion.json').read_text())
        receipt={'status':'complete','completed_at':evidence['completed_at'],'snapshot':str(snapshot.resolve()),
                 'snapshot_hash':sync.digest(dependencies.input_identity(snapshot)),'dependencies':dependencies.verify(snapshot),'legacy_completion':evidence}
        if evidence.get('operation_id')!=operation_id:raise sync.SyncStopped('completion_operation_id_mismatch')
        verify_snapshot(receipt);verify_live(receipt)
        candidate=Path(candidate)
        if candidate.exists():raise sync.SyncStopped('unclassified_existing_projection')
        build_projection(snapshot,candidate,target,receipt)
        request=plan(candidate,target,receipt)
        request_path.write_text(db.canonical(request)+'\n')
    def guard(receipt):
        verify_snapshot(receipt);verify_live(receipt)
    return sync.synchronize(operation_id,request,target,guard,compare_snapshot_scope)
