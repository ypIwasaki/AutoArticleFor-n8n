"""Small synthetic identity-history fixtures; never normal business operations."""
from pathlib import Path
from contextlib import closing
import copy,json,shutil,sqlite3,tempfile,unittest
import project_database as db
import database_phase1 as base
import import_legacy_database as imp
import identity_assessment_history as history
import identity_history_dependencies as origins
import sync_snapshot_dependencies as deps
import continuous_database_sync as sync
import legacy_sync_projection as projection
from compare_phase4_database import fingerprint

STAMP='2026-09-16T00:00:00Z'

def source_fixture(folder):
 folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
 article={'id':1,'article_key':'candidate-key','url':'https://example.test/story','title':'Saved title','published_at':STAMP}
 subject={'id':1,'article_key':'old-key','original_url':article['url'],'content_text':'','content_hash':db.checksum(b''),'content_length':0,'content_status':'unavailable','fetched_at':STAMP}
 cache=dict(subject,article_key='old-key',title=article['title'],published_at=STAMP)
 with closing(sqlite3.connect(str(folder/'n8n.sqlite'))) as c:
  c.execute('CREATE TABLE data_table(id TEXT,name TEXT)')
  for name,rows in [('articles',[article]),('article_contents',[subject]),('talents',[]),('article_talents',[]),('article_classifications',[]),('article_feedback',[])]:
   c.execute('INSERT INTO data_table VALUES (?,?)',(name,name));columns=list(rows[0]) if rows else ['id']
   c.execute('CREATE TABLE data_table_user_'+name+'('+','.join(base.q(k) for k in columns)+')')
   for row in rows:c.execute('INSERT INTO data_table_user_'+name+' VALUES ('+','.join('?' for _ in columns)+')',[row[k] for k in columns])
  c.commit()
 p=folder/'files'/history.CACHE;p.parent.mkdir(parents=True);p.write_text(db.canonical({'entries':{article['url']:cache}}))
 return article,subject,cache

def create_origin(root):
 root=Path(root);identity='fixture-origin';out=root/'.operation-state/database/identity-origins'/identity;source_fixture(out)
 for path in deps.implementation_paths(root):
  dest=out/'implementation'/path;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/path,dest)
 import article_review_facts as review
 for path in review.POLICY_FILES:
  dest=out/'rules'/path;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/path,dest)
 files=[{'path':str(x.relative_to(out)),'size':x.stat().st_size,'sha256':base.sha(x)} for x in sorted(out.rglob('*')) if x.is_file()]
 origin={'capture_scope_complete':True,'format':'identity-origin-v1','id':identity,'original_migration_id':'fixture-migration','recorded_at':STAMP,'epoch':STAMP,'processor':{'path':'scripts/import_legacy_database.py','version':imp.VERSION,'sha256':base.sha(root/'scripts/import_legacy_database.py')},'files':files}
 base.save(out/'origin.json',origin)
 registry={'format':origins.VERSION,'origins':[{'id':identity,'root':str(out.relative_to(root)),'manifest_sha256':base.sha(out/'origin.json'),'processor_version':imp.VERSION}]};base.save(root/origins.REGISTRY,registry)
 return out

def implementation_root(root):
 import article_review_facts as review
 root=Path(root);root.mkdir(parents=True,exist_ok=True)
 for path in list(review.POLICY_FILES)+[deps.acceptance.LEDGER_PATH]+deps.implementation_paths(db.ROOT):
  out=root/path;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(db.ROOT/path,out)
 create_origin(root)

def import_fixture(snapshot,path,context=None):
 db.migrate(path);c=db.connect(path);cache=snapshot/'files'/history.CACHE;files=[{'path':history.CACHE,'size':cache.stat().st_size,'sha256':base.sha(cache)}]
 try:
  with db.transaction(c):
   c.execute("INSERT INTO migration_runs(id,input_snapshot,importer_version,started_at,status) VALUES (?,?,?,?,?)",('fixture-migration',str(snapshot),imp.VERSION,STAMP,'complete'))
   engine=imp.Importer(c,snapshot,'fixture-migration',STAMP,files,identity_context=context)
   for step in ('preload','archive_files','n8n','contents'):getattr(engine,step)()
   if context:result=context.persist(engine,{'legacy_completion':{'entry_hash':'same-normal-summary','completed_at':STAMP,'operation_id':'fixture-summary-v2','supersedes':{'operation_id':'fixture-summary-v1'}}})
   else:result=None
   engine.verify()
  return result
 finally:c.close()

class IdentityHistoryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'live';implementation_root(self.root)
  self.snapshot=Path(self.temp.name)/'snapshot';self.snapshot.mkdir();origin=self.root/'.operation-state/database/identity-origins/fixture-origin'
  shutil.copyfile(origin/'n8n.sqlite',self.snapshot/'n8n.sqlite');p=self.snapshot/'files'/history.CACHE;p.parent.mkdir(parents=True);shutil.copyfile(origin/'files'/history.CACHE,p)
  self.old=self.snapshot/'project.sqlite';import_fixture(self.snapshot,self.old)
  cache=json.loads(p.read_text());next(iter(cache['entries'].values()))['article_key']='updated-key';p.write_text(db.canonical(cache))
  policy,implementation=deps.capture_ledgers(self.root);deps.copy_dependencies(self.root,self.snapshot,policy,implementation)
  base.save(self.snapshot/'phase1-result.json',{'project_backup_sha256':base.sha(self.old)})
  self.candidate=Path(self.temp.name)/'candidate.sqlite';self.receipt={'status':'complete','completed_at':STAMP,'snapshot':str(self.snapshot),'snapshot_hash':'fixture','legacy_completion':{'entry_hash':'same-normal-summary','completed_at':STAMP,'operation_id':'fixture-summary-v2'}}
 def tearDown(self):self.temp.cleanup()
 def build(self):
  ctx=history.Context(self.snapshot,Path(self.temp.name)/'proof');result=import_fixture(self.snapshot,self.candidate,ctx);return ctx,result
 def test_complete_old_and_new_reproduction_replay_and_restore(self):
  ctx,result=self.build();self.assertEqual(result['versions_added'],2)
  with closing(db.connect(self.candidate)) as c:
   old=dict(c.execute('SELECT * FROM article_identity_assessments').fetchone());self.assertTrue(json.loads(old['checks_json'])['cache_source_matches'])
   versions=list(c.execute('SELECT * FROM identity_assessment_versions ORDER BY kind'));self.assertEqual(len(versions),2);self.assertEqual({x['kind'] for x in versions},{'initial','reevaluation'})
   current=next(x for x in versions if x['kind']=='reevaluation');self.assertFalse(json.loads(current['checks_json'])['cache_source_matches']);self.assertEqual(current['article_id'],old['article_id'])
   self.assertEqual(c.execute('SELECT identity_state FROM articles WHERE id=?',(old['article_id'],)).fetchone()[0],'held')
   self.assertEqual(history.verify_database(c,history.expected_processors(self.snapshot),Path(self.temp.name)/'verify')['differences'],0)
   before=fingerprint(self.candidate)
   engine=imp.Importer(c,self.snapshot,'fixture-migration',STAMP,[]);engine.preload()
   repeated=ctx.persist(engine,{'legacy_completion':{'entry_hash':'same-normal-summary','completed_at':STAMP,'operation_id':'fixture-summary-v2','supersedes':{'operation_id':'fixture-summary-v1'}}})
   self.assertEqual(repeated['versions_added'],0)
   self.assertEqual(before,fingerprint(self.candidate));self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall());self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
  backup=Path(self.temp.name)/'backup.sqlite';restored=Path(self.temp.name)/'restored.sqlite';db.backup(backup,self.candidate);db.restore_check(backup,restored)
  self.assertEqual(fingerprint(self.candidate),fingerprint(restored))
  with closing(db.connect(restored,readonly=True)) as c:self.assertEqual(history.verify_database(c,history.expected_processors(self.snapshot),Path(self.temp.name)/'verify-restored')['versions'],2)
 def test_no_history_updates_deletes_or_source_mutation(self):
  self.build()
  with closing(db.connect(self.candidate)) as c:
   for table in ['article_identity_assessments']+list(history.ENTITIES.values()):
    key=c.execute('PRAGMA table_info('+table+')').fetchone()['name']
    with self.assertRaises(sqlite3.IntegrityError):c.execute('UPDATE '+table+' SET '+key+'='+key)
    with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM '+table)
   with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE source_records SET raw_json='{}' WHERE id=(SELECT source_record_id FROM identity_evidence_members LIMIT 1)")
 def test_missing_modified_or_unregistered_origin_rejected(self):
  policy=json.loads((self.snapshot/'policy-dependencies.json').read_text());origin=self.snapshot/'identity-history/fixture-origin';file=origin/'files'/history.CACHE;saved=file.read_bytes()
  file.unlink()
  with self.assertRaises(Exception):origins.verify(self.snapshot,policy['identity_history'])
  file.write_bytes(saved+b' ')
  with self.assertRaises(Exception):origins.verify(self.snapshot,policy['identity_history'])
  file.write_bytes(saved);bad=copy.deepcopy(policy['identity_history']);bad['origins'][0]['processor_version']='unknown'
  with self.assertRaises(Exception):origins.verify(self.snapshot,bad)
  registry=self.snapshot/'files'/origins.REGISTRY;registry.unlink()
  with self.assertRaises(Exception):origins.verify(self.snapshot,policy['identity_history'])
 def test_independent_check_detects_immutable_data_tampering(self):
  self.build()
  with closing(db.connect(self.candidate)) as c:
   c.execute('DROP TRIGGER immutable_identity_assessment_versions_update');c.execute("UPDATE identity_assessment_versions SET checks_json='{}' WHERE kind='reevaluation'")
   with self.assertRaisesRegex(RuntimeError,'independent_identity_difference'):history.verify_database(c,history.expected_processors(self.snapshot),Path(self.temp.name)/'verify-bad')
 def test_atomic_failure_difference_retry_and_same_id_conflict(self):
  self.build();original=self.old;self.old=Path(self.temp.name)/'sync-target.sqlite';db.backup(self.old,original)
  request=projection.plan(self.candidate,self.old,self.receipt);baseline=fingerprint(self.old)
  def failed(index):
   if index==len(request['changes'])-1:raise RuntimeError('controlled-failure')
  with self.assertRaisesRegex(RuntimeError,'controlled-failure'):sync.synchronize('identity-operation',request,self.old,lambda _:None,lambda c,r:[],fault=failed)
  after=fingerprint(self.old)
  for table in baseline:
   if table not in ('sync_runs','sync_attempts'):self.assertEqual(baseline[table],after[table])
  with self.assertRaisesRegex(sync.SyncStopped,'post_sync_comparison_difference'):sync.synchronize('identity-operation',request,self.old,lambda _:None,lambda c,r:[{'reason':'independent_fixture_difference'}])
  after=fingerprint(self.old)
  for table in baseline:
   if table not in ('sync_runs','sync_attempts'):self.assertEqual(baseline[table],after[table])
  def independent(c,r):
   history.verify_database(c,history.expected_processors(self.snapshot),Path(self.temp.name)/'sync-verify');return []
  result=sync.synchronize('identity-operation',request,self.old,lambda _:None,independent);counts=fingerprint(self.old)
  self.assertEqual(result,sync.synchronize('identity-operation',request,self.old,lambda _:None,independent));after=fingerprint(self.old)
  self.assertTrue(all(counts[k]==after[k] for k in counts if k!='sync_runs'))
  altered=copy.deepcopy(request);altered['receipt']['snapshot_hash']='different-evidence'
  with self.assertRaisesRegex(sync.SyncStopped,'operation_id_content_conflict'):sync.synchronize('identity-operation',altered,self.old,lambda _:None,lambda c,r:[])

 def test_source_hash_and_absence_scope_are_checked(self):
  self.build()
  with closing(db.connect(self.candidate)) as c:
   row=dict(c.execute('SELECT * FROM identity_assessment_versions LIMIT 1').fetchone());row['input_hash']='0'*64
   with self.assertRaisesRegex(RuntimeError,'identity_source_hash_mismatch'):history.validate_change_row(c,'identityVersion',row)
   c.execute('DROP TRIGGER immutable_identity_evidence_members_delete');c.execute("DELETE FROM identity_evidence_members WHERE role='cache'")
   with self.assertRaisesRegex(RuntimeError,'identity_scope_members_hash'):history.verify_database(c,history.expected_processors(self.snapshot),Path(self.temp.name)/'missing-scope')
 def test_changed_db_implementation_and_unregistered_processor_rejected(self):
  self.build()
  with closing(db.connect(self.candidate)) as c:
   expected=history.expected_processors(self.snapshot);bad=copy.deepcopy(expected);bad.pop(next(iter(bad)))
   with self.assertRaisesRegex(RuntimeError,'unregistered_identity_processor'):history.verify_database(c,bad,Path(self.temp.name)/'bad-registration')
   c.execute('DROP TRIGGER immutable_identity_implementation_blobs_update');c.execute("UPDATE identity_implementation_blobs SET content=content||'modified' WHERE sha256=(SELECT sha256 FROM identity_implementation_blobs LIMIT 1)")
   with self.assertRaisesRegex(RuntimeError,'identity_blob_hash_mismatch'):history.verify_database(c,expected,Path(self.temp.name)/'bad-runtime')
 def test_historical_success_replay_survives_contract_upgrade(self):
  from unittest.mock import patch
  from test_continuous_database_sync import ContinuousSyncTests
  helper=ContinuousSyncTests();helper.setUp()
  try:
   with patch.object(sync,'VERSION','continuous-sync-v3-acceptance'):
    request=helper.request();result=helper.send('historical-operation',request)
   self.assertEqual(helper.send('historical-operation',request),result)
   changed=copy.deepcopy(request);changed['receipt']['completed_at']='2026-09-17T00:00:00Z'
   with self.assertRaisesRegex(sync.SyncStopped,'operation_id_content_conflict'):helper.send('historical-operation',changed)
  finally:helper.tearDown()

 def test_changed_subject_restores_old_source_judgment_and_evidence(self):
  with closing(sqlite3.connect(str(self.snapshot/'n8n.sqlite'))) as c:
   c.execute("UPDATE data_table_user_article_contents SET content_status='failed'");c.commit()
  ctx,result=self.build()
  with closing(db.connect(self.candidate)) as c:
   self.assertEqual(c.execute('SELECT count(*) FROM article_identity_assessments').fetchone()[0],2)
   self.assertEqual(c.execute("SELECT count(*) FROM source_records WHERE source_path='n8n:data_table_user_article_contents'").fetchone()[0],2)
   self.assertEqual(c.execute('SELECT count(*) FROM identity_assessment_versions').fetchone()[0],2)
   self.assertEqual(history.verify_database(c,history.expected_processors(self.snapshot),Path(self.temp.name)/'changed-source-proof')['differences'],0)
   self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())

 def test_successive_subject_updates_keep_all_initial_judgments(self):
  with closing(sqlite3.connect(str(self.snapshot/'n8n.sqlite'))) as c:c.execute("UPDATE data_table_user_article_contents SET content_status='failed'");c.commit()
  self.build();first=self.candidate;next_snapshot=Path(self.temp.name)/'next-snapshot'
  shutil.copytree(self.snapshot,next_snapshot,ignore=shutil.ignore_patterns('project.sqlite','project.sqlite-wal','project.sqlite-shm'))
  db.backup(next_snapshot/'project.sqlite',first);base.save(next_snapshot/'phase1-result.json',{'project_backup_sha256':base.sha(next_snapshot/'project.sqlite')})
  with closing(sqlite3.connect(str(next_snapshot/'n8n.sqlite'))) as c:c.execute("UPDATE data_table_user_article_contents SET content_status='partial'");c.commit()
  context=history.Context(next_snapshot,Path(self.temp.name)/'third-proof');candidate=Path(self.temp.name)/'next-candidate.sqlite';import_fixture(next_snapshot,candidate,context)
  with closing(db.connect(candidate)) as c:
   self.assertEqual(c.execute('SELECT count(*) FROM article_identity_assessments').fetchone()[0],3)
   self.assertEqual(c.execute("SELECT count(*) FROM source_records WHERE source_path='n8n:data_table_user_article_contents'").fetchone()[0],3)
   self.assertEqual(history.verify_database(c,history.expected_processors(next_snapshot),Path(self.temp.name)/'successive-independent')['initial'],3)

 def test_self_consistent_unknown_version_and_unsafe_registry_id_rejected(self):
  folder=self.snapshot/'identity-history/fixture-origin';origin=json.loads((folder/'origin.json').read_text());origin['processor']['version']='unregistered-v99';base.save(folder/'origin.json',origin)
  registration={'id':'fixture-origin','manifest_sha256':base.sha(folder/'origin.json'),'processor_version':'unregistered-v99'}
  with self.assertRaisesRegex(RuntimeError,'identity_processor_unregistered_version'):origins.verify_origin(folder,registration)
  registry=Path(self.temp.name)/'unsafe-registry.json';base.save(registry,{'format':origins.VERSION,'origins':[{'id':'../outside'}]})
  with self.assertRaisesRegex(RuntimeError,'invalid_identity_origin_id'):origins.load_registry(registry)
  self.assertFalse(self.candidate.exists())

if __name__=='__main__':unittest.main()
