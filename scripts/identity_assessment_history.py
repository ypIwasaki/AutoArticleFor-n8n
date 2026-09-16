"""Append-only identity evaluations backed by complete immutable source scopes."""
from pathlib import Path
from contextlib import closing
import json,subprocess,sys,collections
import project_database as db
import database_phase1 as base
import import_legacy_database as imp
import identity_history_dependencies as origins

VERSION='identity-assessment-history-v1'
ENTITIES={'identityBlob':'identity_implementation_blobs','identityProcessor':'identity_processors','identityProcessorFile':'identity_processor_files','identityEvidence':'identity_evidence_sets','identityEvidenceMember':'identity_evidence_members','identityVersion':'identity_assessment_versions','identityEvent':'identity_assessment_events'}
CACHE='content/article-body-captures/backfill-state.json'

def digest(value):return db.checksum(db.canonical(value).encode())
def source_id(path,position,raw):return imp.ident('source',path,str(position),digest(raw))

def saved_decisions(folder,implementation,output):
 command=[sys.executable,'-I',str(Path(__file__).with_name('identity_assessment_worker.py')),'--snapshot',str(folder),'--implementation',str(implementation),'--output',str(output)]
 subprocess.run(command,cwd=str(output.parent),check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 rows=[json.loads(x) for x in output.read_text().splitlines()]
 result={x['source_record_id']:x for x in rows}
 if len(result)!=len(rows):raise RuntimeError('duplicate_reproduced_identity')
 return result

def scope(folder):
 with closing(base.ro(Path(folder)/'n8n.sqlite')) as c:
  tables={x['name']:'data_table_user_'+x['id'] for x in c.execute('SELECT id,name FROM data_table') if x['name'] in ('articles','article_contents')}
  if set(tables)!={'articles','article_contents'}:raise RuntimeError('identity_scope_missing_tables')
  articles=[dict(x) for x in c.execute('SELECT * FROM '+base.q(tables['articles'])+' ORDER BY id')]
  subjects=[dict(x) for x in c.execute('SELECT * FROM '+base.q(tables['article_contents'])+' ORDER BY id')]
 cache=json.loads((Path(folder)/'files'/CACHE).read_text())
 if not isinstance(cache.get('entries'),dict):raise RuntimeError('identity_cache_scope_missing')
 return {'tables':tables,'articles':articles,'subjects':subjects,'cache':cache['entries']}

def insert(c,table,row):
 keys=[x['name'] for x in sorted(c.execute('PRAGMA table_info('+table+')'),key=lambda x:x['pk']) if x['pk']]
 before=c.execute('SELECT * FROM '+table+' WHERE '+' AND '.join(k+'=?' for k in keys),[row[k] for k in keys]).fetchone()
 if before:
  if dict(before)!=row:raise RuntimeError('immutable_identity_row_changed:'+table)
  return 0
 c.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()));return 1

def processor_spec(folder,version,policy_files):
 folder=Path(folder);files=[];blobs=[]
 paths=sorted((folder/'implementation').rglob('*'))
 for path in paths:
  if not path.is_file() or '__pycache__' in path.parts:continue
  relative=str(path.relative_to(folder/'implementation'));raw=path.read_bytes();h=db.checksum(raw)
  files.append({'path':relative,'sha256':h,'size':len(raw)});blobs.append({'sha256':h,'size':len(raw),'content':raw.decode('utf-8')})
 for path,relative in policy_files:
  raw=Path(path).read_bytes();h=db.checksum(raw);files.append({'path':'rules/'+relative,'sha256':h,'size':len(raw)});blobs.append({'sha256':h,'size':len(raw),'content':raw.decode('utf-8')})
 files.sort(key=lambda x:x['path']);main='scripts/import_legacy_database.py';implementation=next(x['sha256'] for x in files if x['path']==main)
 rules_hash=digest({'kind':'identity-rules-embedded-in-importer','implementation_sha256':implementation})
 body={'version':version,'implementation_path':main,'implementation_sha256':implementation,'rules_hash':rules_hash,'manifest_json':db.canonical(files)};pid=digest(body)
 return dict(id=pid,**body),files,blobs

def processor(c,folder,version,policy_files):
 row,files,blobs=processor_spec(folder,version,policy_files);pid=row['id']
 for blob in blobs:insert(c,'identity_implementation_blobs',blob)
 insert(c,'identity_processors',row)
 for item in files:insert(c,'identity_processor_files',{'processor_id':pid,'path':item['path'],'sha256':item['sha256']})
 return pid


def evidence(c,data,pid):
 members=[];article_path='n8n:'+data['tables']['articles']
 for role,items,path in [('article',[(str(x['id']),x) for x in data['articles']],article_path),('subject',[(str(x['id']),x) for x in data['subjects']],'n8n:'+data['tables']['article_contents']),('cache',list(data['cache'].items()),CACHE)]:
  for position,raw in items:
   source_position='entries/'+position if role=='cache' else position;sid=source_id(path,source_position,raw)
   saved=c.execute('SELECT input_hash,raw_json,source_path,record_position FROM source_records WHERE id=?',(sid,)).fetchone()
   if not saved or saved['input_hash']!=digest(raw) or json.loads(saved['raw_json'])!=raw or saved['source_path']!=path or saved['record_position']!=source_position:raise RuntimeError('identity_evidence_source_missing_or_changed')
   members.append({'role':role,'position':position,'source_record_id':sid,'input_hash':digest(raw)})
 members.sort(key=lambda x:(x['role'],x['position']));h=digest(members)
 info={'format':VERSION,'article_source_path':article_path,'cache_source_path':CACHE,'article_count':len(data['articles']),'cache_count':len(data['cache']),'subject_count':len(data['subjects']),'subject_source_path':'n8n:'+data['tables']['article_contents'],'lookup':'complete article table by exact URL; complete cache entries by exact article_key then exact original_url','candidate_order':'numeric source row id'}
 body={'processor_id':pid,'scope_json':db.canonical(info),'members_hash':h};eid=digest(body)
 insert(c,'identity_evidence_sets',dict(id=eid,**body))
 for member in members:insert(c,'identity_evidence_members',dict(evidence_set_id=eid,**member))
 return eid

def version_row(source,eid,decision,adopted,kind):
 identity=digest({'source_record_id':source['id'],'input_hash':source['input_hash'],'evidence_set_id':eid})
 return dict(id=identity,source_record_id=source['id'],input_hash=source['input_hash'],evidence_set_id=eid,evaluated_article_id=decision['article_id'],article_id=adopted['article_id'],strategy=decision['strategy'],reason=decision['reason'],candidates_json=decision['candidates_json'],checks_json=decision['checks_json'],kind=kind)

def save_versions(c,eid,decisions,adopted,business_id,stamp,lineage,initial_ids):
 added=0
 for sid,decision in sorted(decisions.items()):
  source=c.execute('SELECT id,input_hash FROM source_records WHERE id=?',(sid,)).fetchone()
  if not source:raise RuntimeError('identity_subject_missing')
  provisional=version_row(source,eid,decision,adopted[sid],'initial')
  provisional['kind']='initial' if sid not in initial_ids or initial_ids[sid]==provisional['id'] else 'reevaluation'
  added+=insert(c,'identity_assessment_versions',provisional)
  event={'assessment_id':provisional['id'],'business_id':business_id,'recorded_at':stamp,'lineage_json':db.canonical(lineage)}
  insert(c,'identity_assessment_events',dict(id=digest(event),**event))
  initial_ids.setdefault(sid,provisional['id'])
 return added

class Context:
 def __init__(self,snapshot,work):
  self.snapshot=Path(snapshot);self.work=Path(work);self.work.mkdir(parents=True,exist_ok=True);self.current={};self.initial={};self.historical=[]
  policy=json.loads((self.snapshot/'policy-dependencies.json').read_text())
  for folder,origin in origins.verify(self.snapshot,policy['identity_history']):
   data=scope(folder);decisions=saved_decisions(folder,folder/'implementation',self.work/(origin['id']+'-reproduced.jsonl'))
   sources={source_id('n8n:'+data['tables']['article_contents'],row['id'],row):row for row in data['subjects']}
   if set(sources)!=set(decisions):raise RuntimeError('historical_identity_coverage')
   for sid,row in decisions.items():
    if sid in self.initial:raise RuntimeError('ambiguous_initial_identity_origin')
    self.initial[sid]=row
   self.historical.append((folder,origin,data,decisions,sources))
  import prior_sync_evidence
  self.prior_proof=prior_sync_evidence.verify(self.snapshot,self.work/'previous-identity-reproduction')
  if self.prior_proof:
   for sid,row in self.prior_proof['initial_rows'].items():
    if sid in self.initial and self.initial[sid]!=row:raise RuntimeError('prior_initial_identity_differs_from_registered_origin')
    self.initial[sid]=row
  with closing(db.connect(self.snapshot/'project.sqlite',readonly=True)) as c:
   expected={x['source_record_id']:dict(x) for x in c.execute('SELECT * FROM article_identity_assessments')}
   if self.initial!=expected:raise RuntimeError('historical_identity_reproduction_mismatch')
   for _,_,_,_,sources in self.historical:
    for sid,raw in sources.items():
     saved=c.execute('SELECT input_hash,raw_json,target_id FROM source_records WHERE id=?',(sid,)).fetchone()
     if not saved or saved['input_hash']!=digest(raw) or json.loads(saved['raw_json'])!=raw:raise RuntimeError('historical_identity_subject_mismatch')
     article=c.execute('SELECT article_id FROM content_fetch_attempts WHERE id=?',(saved['target_id'],)).fetchone()
     if not article or article[0]!=self.initial[sid]['article_id']:raise RuntimeError('historical_identity_mapping_mismatch')
 def choose(self,sid,raw,current):
  if current['source_record_id']!=sid:raise RuntimeError('identity_source_changed')
  self.current[sid]=current
  return self.initial.get(sid,current)
 def persist(self,engine,receipt):
  c=engine.c
  from missing_body_capture_history import restore_sources
  restored=restore_sources(self,engine)
  import prior_sync_evidence
  restored+=prior_sync_evidence.restore_identity(c,self.snapshot,self.prior_proof)
  # Every historical subject must have been reconstructed with its original ID.
  if any(not c.execute('SELECT 1 FROM source_records WHERE id=?',(sid,)).fetchone() for sid in self.initial):raise RuntimeError('historical_subject_not_restored')
  adopted={x['source_record_id']:dict(x) for x in c.execute('SELECT * FROM article_identity_assessments')}
  if any(adopted.get(sid)!=old for sid,old in self.initial.items()):raise RuntimeError('initial_identity_not_preserved')
  initial_ids={x['source_record_id']:x['id'] for x in c.execute("SELECT * FROM identity_assessment_versions WHERE kind='initial'")};added=0
  for folder,origin,data,decisions,sources in self.historical:
   # Recreate missing historical cache observations from historical raw input.
   # Existing payload hashes and version IDs are reused by the normal importer.
   for position,raw in data['cache'].items():
    sid=source_id(CACHE,'entries/'+position,raw)
    if not c.execute('SELECT 1 FROM source_records WHERE id=?',(sid,)).fetchone():engine.file_fetch(CACHE,'entries/'+position,raw)
   policy_files=[(folder/x['path'],x['path'][6:]) for x in origin['files'] if x['path'].startswith('rules/')]
   pid=processor(c,folder,origin['processor']['version'],policy_files);eid=evidence(c,data,pid)
   added+=save_versions(c,eid,decisions,adopted,'migration:'+origin['original_migration_id'],origin['recorded_at'],{'kind':'initial-reproduction','origin_id':origin['id']},initial_ids)
  data={'tables':engine.tables,'articles':engine.dbrows['articles'],'subjects':engine.dbrows['article_contents'],'cache':engine.cache['entries']}
  policy=json.loads((self.snapshot/'policy-dependencies.json').read_text())
  policy_files=[(self.snapshot/'files'/x['path'],x['path']) for x in policy['files']]
  pid=processor(c,self.snapshot,imp.VERSION,policy_files);eid=evidence(c,data,pid)
  completion=receipt['legacy_completion'];business='checkpoint:'+completion['entry_hash'] if 'entry_hash' in completion else 'execution:'+str(completion['execution_id'])
  added+=save_versions(c,eid,self.current,adopted,business,completion['completed_at'],{'kind':'evaluation','operation_id':completion['operation_id'],'supersedes':completion.get('supersedes')},initial_ids)
  return {'restored_historical_rows':restored,'initial_assessments':len(self.initial),'evaluations':len(self.current),'versions_added':added,'compatibility_preserved':True,'evidence_sets':c.execute('SELECT count(*) FROM identity_evidence_sets').fetchone()[0]}

def validate_change_row(c,entity,row):
 if entity=='identityBlob':
  raw=row['content'].encode()
  if len(raw)!=row['size'] or db.checksum(raw)!=row['sha256']:raise RuntimeError('identity_blob_hash_mismatch')
 elif entity in ('identityProcessor','identityEvidence','identityEvent'):
  if digest({k:v for k,v in row.items() if k!='id'})!=row['id']:raise RuntimeError('identity_stable_id_mismatch')
  if entity=='identityProcessor':
   if row['version'] not in ('phase4-length-alias-import-v3',imp.VERSION):raise RuntimeError('identity_processor_version_unregistered')
   if row['rules_hash']!=digest({'kind':'identity-rules-embedded-in-importer','implementation_sha256':row['implementation_sha256']}):raise RuntimeError('identity_rules_hash_mismatch')
 elif entity=='identityProcessorFile':
  processor=c.execute('SELECT manifest_json FROM identity_processors WHERE id=?',(row['processor_id'],)).fetchone()
  if not processor or not any(x['path']==row['path'] and x['sha256']==row['sha256'] for x in json.loads(processor[0])):raise RuntimeError('unmanifested_identity_implementation')
 elif entity in ('identityEvidenceMember','identityVersion'):
  src=c.execute('SELECT input_hash,raw_json FROM source_records WHERE id=?',(row['source_record_id'],)).fetchone()
  if not src or row['input_hash']!=src['input_hash'] or digest(json.loads(src['raw_json']))!=row['input_hash']:raise RuntimeError('identity_source_hash_mismatch')
  if entity=='identityVersion':
   if row['id']!=digest({k:row[k] for k in ('source_record_id','input_hash','evidence_set_id')}):raise RuntimeError('identity_version_id_mismatch')
   adopted=c.execute('SELECT * FROM article_identity_assessments WHERE source_record_id=?',(row['source_record_id'],)).fetchone()
   if not adopted or adopted['article_id']!=row['article_id']:raise RuntimeError('identity_version_cannot_adopt_mapping')
   if row['kind']=='initial':
    for k in ('article_id','strategy','reason','candidates_json','checks_json'):
     if adopted[k]!=row[k]:raise RuntimeError('initial_identity_differs_from_compatibility')

def expected_processors(snapshot):
 snapshot=Path(snapshot);policy=json.loads((snapshot/'policy-dependencies.json').read_text());result={}
 from sync_snapshot_dependencies import verify_project_snapshot
 verify_project_snapshot(snapshot)
 with closing(db.connect(snapshot/'project.sqlite',readonly=True)) as previous:
  if previous.execute("SELECT 1 FROM sqlite_master WHERE name='identity_processors'").fetchone():result.update({x['id']:dict(x) for x in previous.execute('SELECT * FROM identity_processors')})
 for folder,origin in origins.verify(snapshot,policy['identity_history']):
  files=[(folder/x['path'],x['path'][6:]) for x in origin['files'] if x['path'].startswith('rules/')]
  row,_,_=processor_spec(folder,origin['processor']['version'],files);result[row['id']]=row
 row,_,_=processor_spec(snapshot,imp.VERSION,[(snapshot/'files'/x['path'],x['path']) for x in policy['files']]);result[row['id']]=row
 return result

def verify_database(c,expected,work,include_reconstruction=False):
 """Independent reconstruction from DB evidence, using only registered saved code.

 No comparison against the projection request is used to derive decisions.
 """
 import sqlite3
 work=Path(work);work.mkdir(parents=True,exist_ok=False)
 actual={x['id']:dict(x) for x in c.execute('SELECT * FROM identity_processors')}
 if actual!=expected:raise RuntimeError('unregistered_identity_processor_or_manifest')
 for entity,table in ENTITIES.items():
  for row in c.execute('SELECT * FROM '+table):validate_change_row(c,entity,dict(row))
 total=0;initial={};results=[];reproductions={}
 for basis in c.execute('SELECT * FROM identity_evidence_sets ORDER BY id').fetchall():
  folder=work/basis['id'];folder.mkdir();processor=actual[basis['processor_id']]
  files=list(c.execute('SELECT f.path,b.sha256,b.size,b.content FROM identity_processor_files f JOIN identity_implementation_blobs b ON b.sha256=f.sha256 WHERE f.processor_id=? ORDER BY f.path',(processor['id'],)))
  manifest=[{'path':x['path'],'sha256':x['sha256'],'size':x['size']} for x in files]
  if manifest!=json.loads(processor['manifest_json']):raise RuntimeError('identity_processor_file_coverage')
  for item in files:
   relative=Path(item['path'])
   if relative.is_absolute() or '..' in relative.parts:raise RuntimeError('identity_processor_unsafe_path')
   path=folder/'implementation'/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(item['content'].encode())
  members=[dict(x) for x in c.execute('SELECT role,position,source_record_id,input_hash FROM identity_evidence_members WHERE evidence_set_id=? ORDER BY role,position',(basis['id'],))]
  if digest(members)!=basis['members_hash']:raise RuntimeError('identity_scope_members_hash')
  info=json.loads(basis['scope_json'])
  if info['format']!=VERSION:raise RuntimeError('identity_scope_version')
  grouped=collections.defaultdict(list)
  for item in members:
   source=c.execute('SELECT * FROM source_records WHERE id=?',(item['source_record_id'],)).fetchone();raw=json.loads(source['raw_json']);role=item['role'];expected_path=info[{'article':'article_source_path','subject':'subject_source_path','cache':'cache_source_path'}[role]]
   expected_position='entries/'+item['position'] if role=='cache' else item['position']
   if source['source_path']!=expected_path or source['record_position']!=expected_position:raise RuntimeError('identity_scope_source_location')
   grouped[role].append((item['position'],raw))
  for role in ('article','subject','cache'):
   if len(grouped[role])!=info[role+'_count']:raise RuntimeError('identity_scope_count')
  with closing(sqlite3.connect(str(folder/'n8n.sqlite'))) as out:
   out.execute('CREATE TABLE data_table(id TEXT,name TEXT)')
   for role,name in [('article','articles'),('subject','article_contents')]:
    rows=[x[1] for x in sorted(grouped[role],key=lambda x:int(x[0]))]
    table=info[role+'_source_path'][4:];columns=list(rows[0]) if rows else ['id','article_key','url','title','published_at']
    out.execute('INSERT INTO data_table VALUES (?,?)',(table[len('data_table_user_'):],name));out.execute('CREATE TABLE '+base.q(table)+'('+','.join(base.q(k) for k in columns)+')')
    out.executemany('INSERT INTO '+base.q(table)+' VALUES ('+','.join('?' for _ in columns)+')',[[raw[k] for k in columns] for raw in rows])
   out.commit()
  cache=folder/'files'/CACHE;cache.parent.mkdir(parents=True);cache.write_text(db.canonical({'entries':dict(grouped['cache'])}))
  reproduced=saved_decisions(folder,folder/'implementation',folder/'reproduced.jsonl')
  if include_reconstruction:reproductions[basis['id']]=reproduced
  versions=list(c.execute('SELECT * FROM identity_assessment_versions WHERE evidence_set_id=?',(basis['id'],)))
  if {x['source_record_id'] for x in versions}!=set(reproduced):raise RuntimeError('identity_version_scope_coverage')
  for version in versions:
   row=reproduced[version['source_record_id']]
   for key in ('strategy','reason','candidates_json','checks_json'):
    if version[key]!=row[key]:raise RuntimeError('independent_identity_difference:'+key)
   if version['evaluated_article_id']!=row['article_id']:raise RuntimeError('independent_identity_mapping_difference')
   if version['kind']=='initial':
    if version['source_record_id'] in initial:raise RuntimeError('duplicate_initial_identity_version')
    initial[version['source_record_id']]=row
   total+=1
  results.append({'evidence_set_id':basis['id'],'versions':len(versions),'members':len(members)})
 compatibility={x['source_record_id']:dict(x) for x in c.execute('SELECT * FROM article_identity_assessments')}
 if initial!=compatibility:raise RuntimeError('initial_identity_coverage_or_result_difference')
 if c.execute('SELECT count(*) FROM identity_assessment_versions v WHERE NOT EXISTS(SELECT 1 FROM identity_assessment_events e WHERE e.assessment_id=v.id)').fetchone()[0]:raise RuntimeError('identity_version_missing_lineage')
 result={'status':'passed','versions':total,'initial':len(initial),'scopes':results,'differences':0}
 if include_reconstruction:result.update(initial_rows=initial,reproduced_scopes=reproductions)
 return result
