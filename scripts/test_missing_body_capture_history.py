"""Capture-history contract tests on synthetic isolated databases."""
from pathlib import Path
from contextlib import closing
import copy,json,shutil,sqlite3,tempfile,unittest
import project_database as db
import database_phase1 as base
import import_legacy_database as imp
import missing_body_acceptance as acceptance
import missing_body_capture_history as captures
import identity_assessment_history as identity
import identity_history_dependencies as origins
import sync_snapshot_dependencies as deps
import continuous_database_sync as sync
import legacy_sync_projection as projection
from compare_phase4_database import fingerprint
from test_identity_assessment_history import implementation_root,STAMP

class CaptureHistoryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name);self.root=self.home/'live';implementation_root(self.root)
  origin=self.root/'.operation-state/database/identity-origins/fixture-origin';self.origin=origin
  with closing(sqlite3.connect(str(origin/'n8n.sqlite'))) as c:
   c.execute("UPDATE data_table_user_article_contents SET content_status='verified',content_hash=?,content_length=5",(db.checksum(b'absent'),));c.commit()
  base.save(origin/'files/content/article-body-captures/rate-limit-state.json',{'retry_after':'old-control'})
  self.capture={'articleKey':'old-key','originalUrl':'https://example.test/story','contentText':'','contentStatus':'unavailable'}
  p=origin/'files/content/article-body-captures/2026-09-14.jsonl';p.write_text(db.canonical(self.capture)+'\n')
  self.snapshot=self.home/'snapshot';self.snapshot.mkdir();shutil.copyfile(origin/'n8n.sqlite',self.snapshot/'n8n.sqlite');shutil.copytree(origin/'files',self.snapshot/'files')
  self.old=self.snapshot/'project.sqlite';self.run_import(self.old)
  with closing(db.connect(self.old)) as c:
   source,raw=next(acceptance.candidates(c));v,length=acceptance.claims(raw)
   decision=dict(decision_id='missing-body:'+source['id'],subject='missing_body',body_integrity='held_missing_body',status='unverified',preserve_source_status=True,create_payload=False,create_version=False,source_path=source['source_path'],record_position=source['record_position'],input_hash=source['input_hash'],stored_body_hash=v['stored_hash'],stored_body_length=length,source_status=v['status'],conflict_kind='body_integrity',approval_reference='fixture')
   self.ledger={'format_version':acceptance.VERSION,'research':{'fixture':'no matching payload'},'decisions':[decision]};acceptance.apply(c,self.ledger,'fixture');c.commit()
  for root in [self.root,origin/'files']:
   p=root/acceptance.LEDGER_PATH;p.parent.mkdir(parents=True,exist_ok=True);base.save(p,self.ledger)
  meta=json.loads((origin/'origin.json').read_text());meta['capture_scope_complete']=True;meta['files']=[{'path':str(p.relative_to(origin)),'size':p.stat().st_size,'sha256':base.sha(p)} for p in sorted(origin.rglob('*')) if p.is_file() and p.name!='origin.json' and '__pycache__' not in p.parts];base.save(origin/'origin.json',meta)
  registry=json.loads((self.root/origins.REGISTRY).read_text());registry['origins'][0]['manifest_sha256']=base.sha(origin/'origin.json');base.save(self.root/origins.REGISTRY,registry)
  p=self.snapshot/'files/content/article-body-captures/2026-09-16.jsonl';p.write_text(db.canonical(dict(self.capture,contentStatus='partial',contentText='part'))+'\n')
  base.save(self.snapshot/'files/content/article-body-captures/rate-limit-state.json',{'retry_after':'new-control'})
  cache=self.snapshot/'files'/identity.CACHE;value=json.loads(cache.read_text());value['updated_at']='new-header';base.save(cache,value)
  policy,implementation=deps.capture_ledgers(self.root);deps.copy_dependencies(self.root,self.snapshot,policy,implementation)
  base.save(self.snapshot/'input-files.json',self.files())
  self.receipt={'status':'complete','completed_at':STAMP,'snapshot':str(self.snapshot),'snapshot_hash':'fixture-snapshot-hash','legacy_completion':{'entry_hash':'one-business','completed_at':STAMP,'operation_id':'capture-fixture'}}
  base.save(self.snapshot/'phase1-result.json',{'project_backup_sha256':base.sha(self.old)})
  self.candidate=self.home/'candidate.sqlite'
 def files(self):
  return [{'path':str(p.relative_to(self.snapshot/'files')),'size':p.stat().st_size,'sha256':base.sha(p)} for p in sorted((self.snapshot/'files/content').rglob('*')) if p.is_file()]
 def run_import(self,path,ctx=None,cc=None):
  db.migrate(path)
  with closing(db.connect(path)) as c,db.transaction(c):
   c.execute("INSERT INTO migration_runs(id,input_snapshot,importer_version,started_at,status) VALUES ('fixture-migration',?,?,?,'complete')",(str(self.snapshot),imp.VERSION,STAMP))
   engine=imp.Importer(c,self.snapshot,'fixture-migration',STAMP,self.files(),identity_context=ctx,missing_body_context=cc)
   for stage in ('preload','n8n','contents'):getattr(engine,stage)()
   if ctx:
    ctx.persist(engine,self.receipt);acceptance.apply(c,self.ledger,'fixture');return cc.persist(c,self.receipt)
 def build(self):
  self.ctx=identity.Context(self.snapshot,self.home/'proof');self.cc=captures.Context(self.ctx);return self.run_import(self.candidate,self.ctx,self.cc)
 def tearDown(self):self.temp.cleanup()
 def test_parent_exact_extra_reference_replay_and_restore(self):
  self.build()
  with closing(db.connect(self.candidate)) as c,closing(db.connect(self.old,readonly=True)) as old:
   self.assertEqual([tuple(x) for x in c.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity'")],[tuple(x) for x in old.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity'")])
   self.assertEqual(c.execute('SELECT count(*) FROM missing_body_capture_observations').fetchone()[0],2)
   self.assertEqual(c.execute('SELECT count(*) FROM missing_body_capture_sets').fetchone()[0],2)
   self.assertEqual(c.execute("SELECT count(*) FROM source_records WHERE source_path='content/article-body-captures/rate-limit-state.json'").fetchone()[0],2)
   self.assertEqual(c.execute("SELECT count(*) FROM source_records WHERE source_path=? AND record_position='header'",(identity.CACHE,)).fetchone()[0],2)
   before=fingerprint(self.candidate);self.assertEqual(self.cc.persist(c,self.receipt)['added_rows'],0);self.assertEqual(before,fingerprint(self.candidate))
   self.assertEqual(acceptance.apply(c,self.ledger,'fixture')['updated_rows'],0)
  backup=self.home/'backup.sqlite';restored=self.home/'restored.sqlite';db.backup(backup,self.candidate);db.restore_check(backup,restored);self.assertEqual(fingerprint(self.candidate),fingerprint(restored))
  with closing(db.connect(restored,readonly=True)) as c:
   self.assertEqual(captures.verify_database(c,captures.expected_scopes(self.snapshot,self.receipt))['differences'],0)
   self.assertEqual(identity.verify_database(c,identity.expected_processors(self.snapshot),self.home/'restore-proof')['differences'],0)
 def test_changed_same_position_preserves_previous_observation(self):
  p=self.snapshot/'files/content/article-body-captures/2026-09-14.jsonl';p.write_text(db.canonical(dict(self.capture,contentStatus='partial',contentText='changed'))+'\n');base.save(self.snapshot/'input-files.json',self.files());self.build()
  with closing(db.connect(self.candidate)) as c:
   rows=c.execute("SELECT input_hash FROM missing_body_capture_observations WHERE path LIKE '%2026-09-14.jsonl' AND position='line:1'").fetchall();self.assertEqual(len(rows),2);self.assertNotEqual(rows[0][0],rows[1][0]);self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
 def test_parent_source_hash_tamper_and_immutability_rejected(self):
  self.build()
  with closing(db.connect(self.candidate)) as c:
   row=dict(c.execute('SELECT * FROM missing_body_capture_observations LIMIT 1').fetchone())
   for field in ('conflict_id','source_record_id','input_hash'):
    bad=dict(row);bad[field]='missing';bad['id']=identity.digest({k:v for k,v in bad.items() if k!='id'})
    with self.assertRaises(RuntimeError):captures.validate_change_row(c,'captureObservation',bad)
   for table in list(captures.ENTITIES.values()):
    with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM '+table)
   with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE consolidation_conflicts SET details_json=details_json WHERE kind='body_integrity'")
   c.execute('DROP TRIGGER immutable_missing_body_capture_set_members_delete');c.execute('DELETE FROM missing_body_capture_set_members WHERE ordinal=0')
   with self.assertRaisesRegex(RuntimeError,'independent_capture_reference_difference'):captures.verify_database(c,captures.expected_scopes(self.snapshot,self.receipt))
 def test_independent_business_comparison_detects_body_claim_change(self):
  from compare_sync_semantics import SemanticCompare
  from compare_phase4_database import Difference
  from contextlib import ExitStack
  self.build()
  with closing(db.connect(self.candidate)) as c,ExitStack() as stack:
   out=self.home/'semantic-good';out.mkdir();compare=SemanticCompare(c,self.snapshot,out,stack)
   try:
    compare.coverage();compare.sources();compare.identifiers();compare.bodies();compare.reviews();compare.summary_fields();compare.conflicts()
   finally:compare.diffs.close()
   c.execute("UPDATE content_fetch_attempts SET stored_body_length=99 WHERE body_integrity='held_missing_body'")
   out=self.home/'semantic-bad';out.mkdir();compare=SemanticCompare(c,self.snapshot,out,stack)
   try:
    with self.assertRaises(Difference):compare.bodies()
   finally:compare.diffs.close()

 def test_unregistered_historical_scope_rejected_before_projection(self):
  origin=self.snapshot/'identity-history/fixture-origin';meta=json.loads((origin/'origin.json').read_text());meta.pop('capture_scope_complete');base.save(origin/'origin.json',meta)
  registration={'id':'fixture-origin','processor_version':imp.VERSION,'manifest_sha256':base.sha(origin/'origin.json')}
  with self.assertRaisesRegex(RuntimeError,'historical_capture_scope_not_registered'):origins.verify_origin(origin,registration)
  self.assertFalse(self.candidate.exists())

 def test_next_operation_preserves_prior_sets_and_changed_observation(self):
  self.build();first=self.candidate;next_snapshot=self.home/'next-snapshot'
  shutil.copytree(self.snapshot,next_snapshot,ignore=shutil.ignore_patterns('project.sqlite','project.sqlite-wal','project.sqlite-shm'))
  db.backup(next_snapshot/'project.sqlite',first)
  self.snapshot=next_snapshot;self.old=next_snapshot/'project.sqlite';self.candidate=self.home/'next-candidate.sqlite'
  base.save(next_snapshot/'phase1-result.json',{'project_backup_sha256':base.sha(self.old)})
  p=next_snapshot/'files/content/article-body-captures/2026-09-16.jsonl';p.write_text(db.canonical(dict(self.capture,contentText='next observation',contentStatus='partial'))+'\n');base.save(next_snapshot/'input-files.json',self.files())
  self.receipt=dict(self.receipt,snapshot=str(next_snapshot),snapshot_hash='second-snapshot-hash',legacy_completion={'entry_hash':'second-business','completed_at':STAMP,'operation_id':'capture-second'})
  self.ctx=identity.Context(next_snapshot,self.home/'second-proof');self.cc=captures.Context(self.ctx);self.run_import(self.candidate,self.ctx,self.cc)
  original=self.old;self.old=self.home/'next-sync-target.sqlite';db.backup(self.old,original)
  request=projection.plan(self.candidate,self.old,self.receipt)
  def independent(c,r):
   captures.verify_database(c,captures.expected_scopes(next_snapshot,self.receipt));identity.verify_database(c,identity.expected_processors(next_snapshot),self.home/'second-independent');return []
  sync.synchronize('capture-second',request,self.old,lambda _:None,independent)
  with closing(db.connect(self.old,readonly=True)) as c:
   self.assertEqual(c.execute('SELECT count(*) FROM missing_body_capture_scopes').fetchone()[0],3)
   self.assertEqual(c.execute('SELECT count(*) FROM missing_body_capture_observations').fetchone()[0],3)
   self.assertEqual(c.execute("SELECT count(*) FROM source_records WHERE source_path LIKE '%2026-09-16.jsonl' AND record_position='line:1'").fetchone()[0],2)
   self.assertEqual(len(acceptance.plan(c,self.ledger)),1)

 def test_previous_snapshot_hash_is_required_and_tampering_rejected(self):
  from sync_snapshot_dependencies import verify_project_snapshot
  self.assertEqual(verify_project_snapshot(self.snapshot),base.sha(self.old))
  self.old.write_bytes(self.old.read_bytes()+b'changed')
  with self.assertRaisesRegex(deps.DependencyError,'prior_project_snapshot_missing_or_changed'):verify_project_snapshot(self.snapshot)

 def test_duplicate_parent_still_rejected(self):
  self.build()
  with closing(db.connect(self.candidate)) as c:
   row=dict(c.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity'").fetchone());row['id']='duplicate';c.execute('INSERT INTO consolidation_conflicts('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
   with self.assertRaisesRegex(RuntimeError,'acceptance_conflict_not_unique'):acceptance.plan(c,self.ledger)
 def test_transaction_failure_rolls_back_business_history_and_row_audit(self):
  self.build();original=self.old;self.old=self.home/'sync-target.sqlite';db.backup(self.old,original)
  request=projection.plan(self.candidate,self.old,self.receipt);before=fingerprint(self.old)
  def fail(index):
   if index==len(request['changes'])-1:raise RuntimeError('controlled-capture-failure')
  with self.assertRaisesRegex(RuntimeError,'controlled-capture-failure'):sync.synchronize('capture-operation',request,self.old,lambda _:None,lambda c,r:[],fault=fail)
  after=fingerprint(self.old)
  for table in before:
   if table not in ('sync_runs','sync_attempts'):self.assertEqual(before[table],after[table])
  def independent(c,r):captures.verify_database(c,captures.expected_scopes(self.snapshot,self.receipt));return []
  result=sync.synchronize('capture-operation',request,self.old,lambda _:None,independent);self.assertEqual(sync.synchronize('capture-operation',request,self.old,lambda _:None,independent),result)

if __name__=='__main__':unittest.main()
