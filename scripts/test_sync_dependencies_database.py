"""Closed policy/implementation dependency regressions, isolated from real DBs."""
from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
import article_review_facts as review
import project_database as db
import sync_snapshot_dependencies as deps
import complete_sync_snapshot as bundle
import legacy_sync_projection as projection
import continuous_database_sync as sync
from test_continuous_database_sync import ContinuousSyncTests

class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'live';self.snapshot=Path(self.temp.name)/'snapshot';self.root.mkdir();self.snapshot.mkdir()
        for path in list(review.POLICY_FILES)+[deps.acceptance.LEDGER_PATH]+deps.implementation_paths(db.ROOT):
            out=self.root/path;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(db.ROOT/path,out)
        from test_identity_assessment_history import create_origin
        create_origin(self.root)
        self.policy,self.implementation=deps.capture_ledgers(self.root)
        deps.copy_dependencies(self.root,self.snapshot,self.policy,self.implementation)
    def tearDown(self):self.temp.cleanup()
    def test_previous_project_evidence_is_bound_to_request_identity(self):
        project=self.snapshot/'project.sqlite';db.migrate(project)
        meta={'project_backup_sha256':deps.base.sha(project),'backup_sha256':'fixture-legacy','projection_epoch':'fixture-epoch'}
        (self.snapshot/'phase1-result.json').write_text(json.dumps(meta));(self.snapshot/'input-files.json').write_text('[]')
        self.assertEqual(deps.input_identity(self.snapshot)['project_snapshot_sha256'],meta['project_backup_sha256'])
        project.write_bytes(project.read_bytes()+b'changed')
        with self.assertRaisesRegex(deps.DependencyError,'prior_project_snapshot_missing_or_changed'):deps.input_identity(self.snapshot)

    def test_policy_hash_reproduced(self):
        self.assertEqual(len(self.policy['files']),4)
        self.assertEqual(deps.verify(self.snapshot,self.root)['policy_dependencies']['policy_hash'],review.policy_hash(self.root))
        for item in self.policy['files']:self.assertTrue({'path','size','sha256','captured_at'}<=set(item))
    def test_each_missing_policy_rejected_before_projection(self):
        for name in review.POLICY_FILES:
            file=self.snapshot/'files'/name;saved=file.read_bytes();file.unlink()
            with self.assertRaises(deps.DependencyError):projection.build_projection(self.snapshot,self.snapshot/'candidate.sqlite',self.snapshot/'target.sqlite',{})
            self.assertFalse((self.snapshot/'candidate.sqlite').exists());file.write_bytes(saved)
    def test_policy_content_size_and_declared_hash_tampering(self):
        p=self.snapshot/'files'/review.POLICY_FILES[0];p.write_bytes(p.read_bytes()+b'change')
        with self.assertRaisesRegex(deps.DependencyError,'dependency_hash_mismatch'):deps.verify(self.snapshot,self.root)
    def test_review_version_change_rejected(self):
        with patch.object(review,'REVIEW_VERSION',str(review.REVIEW_VERSION)+'-different'):
            with self.assertRaisesRegex(deps.DependencyError,'review_version_changed'):deps.verify(self.snapshot,self.root)
    def test_missing_or_changed_implementation_rejected(self):
        p=self.snapshot/'implementation/scripts/import_legacy_database.py';saved=p.read_bytes();p.unlink()
        with self.assertRaises(deps.DependencyError):deps.verify(self.snapshot,self.root)
        p.write_bytes(saved+b'change')
        with self.assertRaises(deps.DependencyError):deps.verify(self.snapshot,self.root)
    def test_unmanifested_policy_and_unreproducible_hash_rejected(self):
        policy=json.loads((self.snapshot/'policy-dependencies.json').read_text());policy['files'].pop();(self.snapshot/'policy-dependencies.json').write_text(json.dumps(policy))
        with self.assertRaisesRegex(deps.DependencyError,'unmanifested_or_missing_policy'):deps.verify(self.snapshot,self.root)
        policy=self.policy.copy();policy['policy_hash']='0'*64;(self.snapshot/'policy-dependencies.json').write_text(json.dumps(policy))
        with self.assertRaisesRegex(deps.DependencyError,'policy_hash_not_reproducible'):deps.verify(self.snapshot,self.root)
    def test_no_live_policy_fallback(self):
        live=self.root/review.POLICY_FILES[0];live.write_bytes(live.read_bytes()+b'live-only-change')
        self.assertEqual(deps.verify(self.snapshot,self.root)['policy_dependencies']['policy_hash'],self.policy['policy_hash'])
        (self.snapshot/'files'/review.POLICY_FILES[0]).unlink()
        with self.assertRaises(deps.DependencyError):deps.verify(self.snapshot,self.root)
    def test_policy_change_during_capture_never_publishes_bundle(self):
        destination=Path(self.temp.name)/'new-bundle'
        def change_policy(stage):
            live=self.root/review.POLICY_FILES[0];live.write_bytes(live.read_bytes()+b'changed-during-copy')
        with self.assertRaises(deps.DependencyError):bundle.capture(self.root,self.root/'no-legacy.sqlite',self.root/'no-project.sqlite',destination,hook=change_policy)
        self.assertFalse(destination.exists())
        stages=list(destination.parent.glob('new-bundle.incomplete-*'));self.assertEqual(len(stages),1)
        self.assertFalse((stages[0]/'bundle.json').exists());self.assertEqual(json.loads((stages[0]/'capture-failure.json').read_text())['published'],False)
    def test_acceptance_missing_hash_version_and_no_fallback(self):
        p=self.snapshot/'files'/deps.acceptance.LEDGER_PATH;saved=p.read_bytes()
        p.unlink()
        with self.assertRaises(deps.DependencyError):projection.build_projection(self.snapshot,self.snapshot/'candidate.sqlite',None,{})
        self.assertFalse((self.snapshot/'candidate.sqlite').exists())
        p.write_bytes(saved+b' ')
        with self.assertRaises(deps.DependencyError):deps.verify(self.snapshot,self.root)
        p.write_bytes(saved)
        policy=json.loads((self.snapshot/'policy-dependencies.json').read_text());policy['acceptance_decision']['format_version']='unknown'
        (self.snapshot/'policy-dependencies.json').write_text(json.dumps(policy))
        with self.assertRaises(deps.DependencyError):deps.verify(self.snapshot,self.root)
    def test_acceptance_only_change_conflicts_on_same_id(self):
        h=ContinuousSyncTests();h.setUp()
        try:
            request=h.request();request['receipt']['dependencies']={'policy_dependencies':self.policy}
            h.send('operation-acceptance',request)
            changed=json.loads(json.dumps(request));changed['receipt']['dependencies']['policy_dependencies']['acceptance_decision']['sha256']='0'*64
            with self.assertRaisesRegex(sync.SyncStopped,'operation_id_content_conflict'):h.send('operation-acceptance',changed)
        finally:h.tearDown()
    def test_dependency_hash_only_change_conflicts_on_same_id(self):
        h=ContinuousSyncTests();h.setUp()
        try:
            request=h.request();request['receipt']['dependencies']={'policy_dependencies':self.policy,'implementation_dependencies':self.implementation}
            h.send('operation-dependency',request)
            changed=json.loads(json.dumps(request));changed['receipt']['dependencies']['policy_dependencies']['files'][0]['sha256']='0'*64
            with self.assertRaisesRegex(sync.SyncStopped,'operation_id_content_conflict'):h.send('operation-dependency',changed)
            self.assertEqual(sync.operation_status('operation-dependency',h.path)['outcome'],'conflict')
        finally:h.tearDown()

if __name__=='__main__':unittest.main()
