"""Immutable capture scopes and observations; parent conflicts come from saved inputs."""
from pathlib import Path
from contextlib import closing
import json,subprocess,sys
import project_database as db
import import_legacy_database as imp
import identity_assessment_history as identity
import missing_body_acceptance as acceptance
import database_phase1 as base

VERSION='missing-body-capture-history-v1'
ENTITIES={'captureScope':'missing_body_capture_scopes','captureScopeMember':'missing_body_capture_scope_members','captureObservation':'missing_body_capture_observations','captureSet':'missing_body_capture_sets','captureSetMember':'missing_body_capture_set_members'}

def reproduce(context):
    if hasattr(context,'reproduced_databases'):return context.reproduced_databases
    result=[]
    for folder,origin,_,_,_ in context.historical:
        dest=context.work/(origin['id']+'-historical.sqlite')
        subprocess.run([sys.executable,'-I',str(Path(__file__).with_name('missing_body_history_worker.py')),'--origin',str(folder),'--database',str(dest)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        result.append((folder,origin,dest))
    context.reproduced_databases=result
    return result

def restore_sources(context,engine):
    """Restore historical source versions from replay, never from the target DB.

    Mutable article presentation stays current. Historical fetches, payloads,
    provenance and adopted assessments retain their original immutable IDs.
    """
    c=engine.c;added=0
    def put(table,row):
        nonlocal added
        if row is None:raise RuntimeError('historical_dependency_missing:'+table)
        row=dict(row)
        if table=='source_records':row['migration_run_id']=engine.run_id
        added+=identity.insert(c,table,row)
    datasets=list(reproduce(context))
    if getattr(context,'prior_proof',None):datasets.append((None,None,context.snapshot/'project.sqlite'))
    for folder,origin,path in datasets:
        with closing(db.connect(path,readonly=True)) as old:
            sources=[dict(x) for x in old.execute("SELECT * FROM source_records WHERE target_kind IN ('articles','content_fetch_attempts','content_runtime_state','legacy_history_records')")]
            for source in sources:
                if c.execute('SELECT 1 FROM source_records WHERE id=?',(source['id'],)).fetchone():continue
                if source['target_kind'] in ('content_runtime_state','legacy_history_records'):
                    put('source_records',source)
                    target=old.execute('SELECT * FROM '+source['target_kind']+' WHERE id=?',(source['target_id'],)).fetchone()
                    if not target or target['source_record_id']!=source['id']:raise RuntimeError('historical_runtime_source_mismatch')
                    put(source['target_kind'],target)
                    continue
                if source['target_kind']=='articles':
                    aid=source['target_id'];fetch=None
                else:
                    fetch=old.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(source['target_id'],)).fetchone();aid=fetch['article_id']
                if not c.execute('SELECT 1 FROM articles WHERE id=?',(aid,)).fetchone():put('articles',old.execute('SELECT * FROM articles WHERE id=?',(aid,)).fetchone())
                if fetch:
                    if fetch['version_id']:
                        version=old.execute('SELECT * FROM article_content_versions WHERE id=?',(fetch['version_id'],)).fetchone()
                        put('content_payloads',old.execute('SELECT * FROM content_payloads WHERE id=?',(version['payload_id'],)).fetchone())
                        existing=c.execute('SELECT * FROM article_content_versions WHERE id=?',(version['id'],)).fetchone()
                        if not existing:put('article_content_versions',version)
                        elif any(existing[k]!=version[k] for k in ('article_id','payload_id')):raise RuntimeError('historical_body_version_identity_changed')
                    put('content_fetch_attempts',fetch)
                put('source_records',source)
                row=old.execute('SELECT * FROM article_source_provenance WHERE source_record_id=?',(source['id'],)).fetchone()
                if row:put('article_source_provenance',row)
                row=old.execute('SELECT * FROM article_identity_assessments WHERE source_record_id=?',(source['id'],)).fetchone()
                if row:put('article_identity_assessments',context.prior_proof['initial_rows'][source['id']] if origin is None else row)
                for row in old.execute('SELECT * FROM article_identifiers WHERE article_id=?',(aid,)):put('article_identifiers',row)
                for row in old.execute('SELECT * FROM consolidation_conflicts WHERE article_id=?',(aid,)):
                    details=json.loads(row['details_json'])
                    if details.get('source_record_id')==source['id'] or (details.get('source_path')==source['source_path'] and str(details.get('source_position'))==source['record_position']):
                        if not c.execute('SELECT 1 FROM consolidation_conflicts WHERE id=?',(row['id'],)).fetchone():
                            if origin is None:
                                decision=context.prior_proof['initial_rows'].get(source['id'])
                                if decision and decision['strategy']=='held':engine.held(source['source_path'],source['record_position'],json.loads(source['raw_json']),decision['reason'],json.loads(decision['candidates_json']))
                                if not c.execute('SELECT 1 FROM consolidation_conflicts WHERE id=?',(row['id'],)).fetchone():raise RuntimeError('prior_conflict_requires_reproducible_source_context')
                            else:put('consolidation_conflicts',row)
    return added

def capture_members(folder):
    folder=Path(folder);manifest=[];members=[]
    # The complete registered file inventory closes the lookup scope, including
    # files with no matching reference. Do not search any live directory.
    if (folder/'origin.json').exists():
        meta=json.loads((folder/'origin.json').read_text());files=[dict(x,path=x['path'][6:]) for x in meta['files'] if x['path'].startswith('files/content/')]
        if not meta.get('capture_scope_complete'):raise RuntimeError('historical_capture_scope_not_registered')
    else:files=json.loads((folder/'input-files.json').read_text())
    for item in sorted(files,key=lambda x:x['path']):
        if not item['path'].startswith('content/article-body-captures/') or not item['path'].endswith('.jsonl'):continue
        file=folder/'files'/item['path']
        if file.stat().st_size!=item['size'] or base.sha(file)!=item['sha256']:raise RuntimeError('capture_scope_file_changed')
        manifest.append({k:item[k] for k in ('path','size','sha256')})
        for number,line in enumerate(file.read_text(encoding='utf-8-sig').splitlines(),1):
            if not line.strip():continue
            raw=json.loads(line);position='line:'+str(number)
            members.append({'path':item['path'],'position':position,'source_record_id':identity.source_id(item['path'],position,raw),'input_hash':imp.hashrow(raw)})
    members.sort(key=lambda x:(x['path'],int(x['position'][5:])))
    return manifest,members

def scope_row(folder,pid,snapshot_hash):
    manifest,members=capture_members(folder)
    row=dict(snapshot_hash=snapshot_hash,processor_id=pid,format_version=VERSION,manifest_json=db.canonical(manifest),members_hash=identity.digest(members))
    return dict(id=identity.digest(row),**row),members

def observation(conflict,source,capture):
    raw=json.loads(capture['raw_json']);v=imp.content_values(raw);claim=imp.content_values(json.loads(source['raw_json']))
    h=db.checksum(v['text'].encode())
    row=dict(conflict_id=conflict['id'],source_record_id=source['id'],capture_source_record_id=capture['id'],path=capture['source_path'],position=capture['record_position'],input_hash=capture['input_hash'],body_hash=h,body_length=len(v['text']),stored_body_hash=v['stored_hash'],stored_body_length=v['stored_length'],source_status=v['status'],matches_saved_hash=int(h==claim['stored_hash']))
    return dict(id=identity.digest(row),**row)

def legacy_reference(row):
    return dict(path=row['path'],position=row['position'],input_hash=row['input_hash'],computed_body_hash=row['body_hash'],body_length=row['body_length'],source_status=row['source_status'],matches_saved_hash=bool(row['matches_saved_hash']))

def capture_index(c,members):
    keys={};urls={};order={}
    for number,member in enumerate(members):
        row=c.execute('SELECT * FROM source_records WHERE id=?',(member['source_record_id'],)).fetchone()
        if not row or row['input_hash']!=member['input_hash'] or imp.hashrow(json.loads(row['raw_json']))!=member['input_hash'] or row['source_path']!=member['path'] or row['record_position']!=member['position']:raise RuntimeError('capture_source_missing_or_changed')
        value=imp.content_values(json.loads(row['raw_json']));order[row['id']]=number
        keys.setdefault(value['key'],[]).append(row);urls.setdefault(value['url'],[]).append(row)
    return keys,urls,order

def matching(c,source,members,index=None):
    claim=imp.content_values(json.loads(source['raw_json']));keys,urls,order=index or capture_index(c,members)
    rows={r['id']:r for r in keys.get(claim['key'],[])+urls.get(claim['url'],[])}
    return sorted(rows.values(),key=lambda x:order[x['id']])

def validate_change_row(c,entity,row):
    if entity in ('captureScope','captureObservation','captureSet') and identity.digest({k:v for k,v in row.items() if k!='id'})!=row['id']:raise RuntimeError('capture_stable_id_mismatch')
    if entity=='captureScope' and row['format_version']!=VERSION:raise RuntimeError('capture_scope_version')
    if entity=='captureScopeMember':
        source=c.execute('SELECT * FROM source_records WHERE id=?',(row['source_record_id'],)).fetchone()
        if not source or source['input_hash']!=row['input_hash'] or imp.hashrow(json.loads(source['raw_json']))!=row['input_hash'] or source['source_path']!=row['path'] or source['record_position']!=row['position']:raise RuntimeError('capture_source_missing_or_changed')
    if entity in ('captureSet','captureObservation'):
        conflict=c.execute('SELECT * FROM consolidation_conflicts WHERE id=?',(row['conflict_id'],)).fetchone();source=c.execute('SELECT * FROM source_records WHERE id=?',(row['source_record_id'],)).fetchone()
        if not conflict or not source or conflict['kind']!='body_integrity' or json.loads(conflict['details_json']).get('source_record_id')!=source['id'] or not acceptance.missing_claim(json.loads(source['raw_json'])):raise RuntimeError('capture_parent_or_source_mismatch')
        fetch=c.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(source['target_id'],)).fetchone()
        if not fetch or fetch['version_id'] is not None or fetch['status']!='unverified' or fetch['body_integrity']!='held_missing_body':raise RuntimeError('capture_parent_disposition_changed')
        if entity=='captureObservation':
            capture=c.execute('SELECT * FROM source_records WHERE id=?',(row['capture_source_record_id'],)).fetchone()
            if not capture or imp.hashrow(json.loads(capture['raw_json']))!=capture['input_hash'] or row!=observation(conflict,source,capture):raise RuntimeError('capture_observation_evidence_mismatch')
    if entity=='captureSetMember':
        parent=c.execute('SELECT * FROM missing_body_capture_sets WHERE id=?',(row['set_id'],)).fetchone();obs=c.execute('SELECT * FROM missing_body_capture_observations WHERE id=?',(row['observation_id'],)).fetchone()
        if not parent or not obs or parent['conflict_id']!=obs['conflict_id'] or parent['source_record_id']!=obs['source_record_id']:raise RuntimeError('capture_set_parent_mismatch')

def persist_scope(c,scope,members,parents,kind):
    added=identity.insert(c,'missing_body_capture_scopes',scope)
    for item in members:
        row=dict(scope_id=scope['id'],**item);validate_change_row(c,'captureScopeMember',row);added+=identity.insert(c,'missing_body_capture_scope_members',row)
    index=capture_index(c,members)
    for conflict in parents:
        source=c.execute('SELECT * FROM source_records WHERE id=?',(json.loads(conflict['details_json'])['source_record_id'],)).fetchone()
        values=dict(conflict_id=conflict['id'],source_record_id=source['id'],scope_id=scope['id'],kind=kind);s=dict(id=identity.digest(values),**values)
        validate_change_row(c,'captureSet',s);observations=[observation(conflict,source,x) for x in matching(c,source,members,index)]
        if kind=='initial' and [legacy_reference(x) for x in observations]!=json.loads(conflict['details_json'])['capture_references']:raise RuntimeError('initial_capture_references_not_reproduced')
        added+=identity.insert(c,'missing_body_capture_sets',s)
        for ordinal,row in enumerate(observations):
            validate_change_row(c,'captureObservation',row);added+=identity.insert(c,'missing_body_capture_observations',row)
            added+=identity.insert(c,'missing_body_capture_set_members',dict(set_id=s['id'],ordinal=ordinal,observation_id=row['id']))
    return added

class Context:
    def __init__(self,identity_context):
        self.identity=identity_context;self.parents={};self.origins=[]
        for folder,origin,path in reproduce(identity_context):
            with closing(db.connect(path)) as c:
                sources=list(acceptance.candidates(c))
                if not sources:continue
                ledger=folder/'files'/acceptance.LEDGER_PATH
                if not ledger.exists():raise RuntimeError('historical_acceptance_ledger_missing')
                before={x['id']:dict(x) for x in c.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity'")}
                acceptance.apply(c,acceptance.load(ledger),base.sha(ledger));c.commit()
                parents=[dict(x) for x in c.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity'")]
                for parent in parents:
                    sid=json.loads(parent['details_json'])['source_record_id']
                    if sid in self.parents:raise RuntimeError('ambiguous_historical_missing_body_parent')
                    self.parents[sid]=(parent,before[parent['id']])
                self.origins.append((folder,origin,parents))
        with closing(db.connect(identity_context.snapshot/'project.sqlite',readonly=True)) as original:
            expected={json.loads(x['details_json'])['source_record_id']:dict(x) for x in original.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity' AND reason='held_missing_body'")}
            if expected!={sid:p[0] for sid,p in self.parents.items()}:raise RuntimeError('historical_missing_body_conflict_reproduction_mismatch')
    def initial_details(self,sid,aid,details):
        if sid not in self.parents:return details
        parent,base_parent=self.parents[sid];original=json.loads(base_parent['details_json'])
        if parent['article_id']!=aid or {k:v for k,v in original.items() if k!='capture_references'}!={k:v for k,v in details.items() if k!='capture_references'}:raise RuntimeError('missing_body_claim_changed_requires_new_decision')
        return original
    def persist(self,c,receipt):
        added=0;expected={}
        if self.identity.prior_proof:
            import prior_sync_evidence
            with closing(db.connect(self.identity.snapshot/'project.sqlite',readonly=True)) as previous:
                for sid,(scope,members) in prior_sync_evidence.scopes(previous).items():
                    kinds={x[0] for x in previous.execute('SELECT DISTINCT kind FROM missing_body_capture_sets WHERE scope_id=?',(sid,))}
                    if len(kinds)!=1:raise RuntimeError('prior_capture_scope_kind_ambiguous')
                    added+=persist_scope(c,scope,members,[p[0] for p in self.parents.values()],next(iter(kinds)));expected[sid]=(scope,members)
        for folder,origin,parents in self.origins:
            policies=[(folder/x['path'],x['path'][6:]) for x in origin['files'] if x['path'].startswith('rules/')]
            processor,_,_=identity.processor_spec(folder,origin['processor']['version'],policies)
            scope,members=scope_row(folder,processor['id'],origin['id']);added+=persist_scope(c,scope,members,parents,'initial');expected[scope['id']]=(scope,members)
        folder=self.identity.snapshot;policy=json.loads((folder/'policy-dependencies.json').read_text())
        processor,_,_=identity.processor_spec(folder,imp.VERSION,[(folder/'files'/x['path'],x['path']) for x in policy['files']])
        scope,members=scope_row(folder,processor['id'],receipt['snapshot_hash']);added+=persist_scope(c,scope,members,[p[0] for p in self.parents.values()],'observation');expected[scope['id']]=(scope,members)
        return {'added_rows':added,'parents':len(self.parents),'verification':verify_database(c,expected)}

def expected_scopes(snapshot,receipt):
    from sync_snapshot_dependencies import verify_project_snapshot
    import prior_sync_evidence
    verify_project_snapshot(snapshot)
    policy=json.loads((Path(snapshot)/'policy-dependencies.json').read_text())
    with closing(db.connect(Path(snapshot)/'project.sqlite',readonly=True)) as c:result=prior_sync_evidence.scopes(c)
    for folder,origin in identity.origins.verify(snapshot,policy['identity_history']):
        if not origin.get('capture_scope_complete'):raise RuntimeError('historical_capture_scope_not_registered')
        processor,_,_=identity.processor_spec(folder,origin['processor']['version'],[(folder/x['path'],x['path'][6:]) for x in origin['files'] if x['path'].startswith('rules/')])
        row,members=scope_row(folder,processor['id'],origin['id']);result[row['id']]=(row,members)
    processor,_,_=identity.processor_spec(snapshot,imp.VERSION,[(Path(snapshot)/'files'/x['path'],x['path']) for x in policy['files']])
    row,members=scope_row(snapshot,processor['id'],receipt['snapshot_hash']);result[row['id']]=(row,members)
    return result

def verify_database(c,expected):
    actual={x['id']:dict(x) for x in c.execute('SELECT * FROM missing_body_capture_scopes')}
    if actual!={k:v[0] for k,v in expected.items()}:raise RuntimeError('capture_scope_registration_mismatch')
    for entity,table in ENTITIES.items():
        for row in c.execute('SELECT * FROM '+table):validate_change_row(c,entity,dict(row))
    checked=0
    for scope_id,(scope,expected_members) in expected.items():
        members=[dict(x) for x in c.execute('SELECT path,position,source_record_id,input_hash FROM missing_body_capture_scope_members WHERE scope_id=?',(scope_id,))];members.sort(key=lambda x:(x['path'],int(x['position'][5:])))
        if members!=expected_members or identity.digest(members)!=scope['members_hash']:raise RuntimeError('capture_scope_members_mismatch')
        sets=list(c.execute('SELECT * FROM missing_body_capture_sets WHERE scope_id=?',(scope_id,)))
        parents={x['id'] for x in c.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity' AND reason='held_missing_body'")}
        if {x['conflict_id'] for x in sets}!=parents:raise RuntimeError('capture_parent_coverage_mismatch')
        index=capture_index(c,members)
        for s in sets:
            source=c.execute('SELECT * FROM source_records WHERE id=?',(s['source_record_id'],)).fetchone();parent=c.execute('SELECT * FROM consolidation_conflicts WHERE id=?',(s['conflict_id'],)).fetchone()
            observations=[observation(parent,source,x) for x in matching(c,source,members,index)]
            actual_rows=[dict(x) for x in c.execute('SELECT o.* FROM missing_body_capture_set_members m JOIN missing_body_capture_observations o ON o.id=m.observation_id WHERE m.set_id=? ORDER BY m.ordinal',(s['id'],))]
            if observations!=actual_rows:raise RuntimeError('independent_capture_reference_difference')
            if s['kind']=='initial' and [legacy_reference(x) for x in observations]!=json.loads(parent['details_json'])['capture_references']:raise RuntimeError('initial_capture_reference_difference')
            checked+=len(observations)
    return {'status':'passed','scopes':len(actual),'reference_checks':checked,'differences':0}
