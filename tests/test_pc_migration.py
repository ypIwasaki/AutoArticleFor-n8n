"""Migration boundary tests; all databases and homes are temporary fixtures."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pc_migration as m

ENSURE_STOPPED = m.ensure_stopped


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'old'
        self.root.mkdir()
        self.n8n = self.base / 'old-home' / '.n8n'
        self.n8n.mkdir(parents=True)
        self.bundle = self.base / 'bundle'
        for name in ('config', 'content', 'data', '.operation-state/token-usage'):
            (self.root / name).mkdir(parents=True)
        (self.root / '.gitignore').write_text('.env\ndata/\n.operation-state/\n.migration/\n')
        (self.root / 'config/runtime-versions.json').write_text(json.dumps({'node':'22.12.0','npm':'11.16.0','n8n':'2.8.4'}))
        (self.root / 'content/article.md').write_text('reviewed article\n')
        (self.root / '.env').write_text('N8N_API_KEY=private-test-value\n')
        (self.root / '.operation-state/2026-09-18.json').write_text('{"steps":{"collect":{"status":"submission_unknown"}}}')
        (self.root / '.operation-state/n8n-process.json').write_text('{"pid":123}')
        (self.root / '.operation-state/token-usage/binding.json').write_text('{}')
        (self.root / '.operation-state/token-usage/source-test.json').write_text('{"history":[]}')
        (self.n8n / 'config').write_text('{"encryptionKey":"private-test-key"}')
        c = sqlite3.connect(str(self.n8n / 'database.sqlite'))
        c.execute('CREATE TABLE execution_entity(status TEXT)')
        c.execute('CREATE TABLE workflow_entity(id TEXT, active INTEGER)')
        c.execute("INSERT INTO workflow_entity VALUES ('test-id',1)")
        c.commit(); c.close()
        c = sqlite3.connect(str(self.root / 'data/autoarticle.sqlite'))
        c.execute('CREATE TABLE articles(id TEXT, body TEXT)')
        c.execute("INSERT INTO articles VALUES ('a','verified body')")
        c.commit(); c.close()
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.stopped = patch.object(m, 'ensure_stopped')
        self.stopped.start()
        self.addCleanup(self.stopped.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def export(self):
        return m.export_bundle(self.root, self.n8n, self.bundle, True)

    def clone(self):
        dest = self.base / 'new'
        subprocess.check_call(['git','clone','-q',str(self.root),str(dest)])
        return dest, self.base / 'new-home' / '.n8n'

    def test_round_trip_preserves_data_credentials_and_unknown_submission(self):
        self.export()
        dest, home = self.clone()
        result = m.restore_bundle(dest, home, self.bundle, True)
        self.assertFalse(result['servicesStarted'])
        self.assertEqual((dest/'.env').read_bytes(), (self.root/'.env').read_bytes())
        self.assertEqual((home/'config').read_bytes(), (self.n8n/'config').read_bytes())
        self.assertFalse((dest/'.operation-state/n8n-process.json').exists())
        self.assertFalse((dest/'.operation-state/token-usage/binding.json').exists())
        self.assertTrue((dest/'.operation-state/token-usage/source-test.json').exists())
        self.assertIn('submission_unknown', (dest/'.operation-state/2026-09-18.json').read_text())
        with sqlite3.connect(str(dest/'data/autoarticle.sqlite')) as c:
            self.assertEqual(c.execute('SELECT body FROM articles').fetchone()[0], 'verified body')
        with sqlite3.connect(str(home/'database.sqlite')) as c:
            self.assertEqual(c.execute('SELECT id,active FROM workflow_entity').fetchone(), ('test-id',1))
        self.assertFalse((dest/'.migration/RESTORE_INCOMPLETE').exists())

    def test_tampered_bundle_rejected_before_restore_writes(self):
        self.export()
        (self.bundle/'project/.env').write_text('corrupted')
        dest, home = self.clone()
        with self.assertRaises(ValueError): m.restore_bundle(dest,home,self.bundle,True)
        self.assertFalse((dest/'.env').exists())
        self.assertFalse(home.exists())

    def test_existing_environment_is_never_overwritten(self):
        self.export()
        dest, home = self.clone()
        (dest/'.env').write_text('existing secret')
        with self.assertRaises(ValueError): m.restore_bundle(dest,home,self.bundle,True)
        self.assertEqual((dest/'.env').read_text(),'existing secret')
        self.assertFalse(home.exists())

    def test_traversal_and_absolute_paths_rejected(self):
        for rel in ('../outside','project/content/../../.env','/etc/passwd','n8n/../escape','n8n/C:/escape','project/content//x'):
            with self.subTest(rel=rel), self.assertRaises(ValueError): m.safe_relative(rel)

    def test_bundle_symlink_rejected(self):
        self.export()
        p=self.bundle/'project/.env'; p.unlink(); p.symlink_to(self.root/'.env')
        with self.assertRaises(ValueError): m.verify_bundle(self.bundle)

    def test_source_symlink_rejected(self):
        (self.n8n/'linked').symlink_to(self.root/'.env')
        with self.assertRaises(ValueError): self.export()
        self.assertFalse(self.bundle.exists())

    def test_pending_execution_blocks_export(self):
        with sqlite3.connect(str(self.n8n/'database.sqlite')) as c:
            c.execute("INSERT INTO execution_entity VALUES ('waiting')")
        with self.assertRaises(ValueError): self.export()
        self.assertFalse(self.bundle.exists())

    def test_dirty_checkout_blocks_export(self):
        (self.root/'content/article.md').write_text('uncommitted')
        with self.assertRaises(ValueError): self.export()
        self.assertFalse(self.bundle.exists())

    def test_wal_committed_rows_are_in_snapshot(self):
        c=sqlite3.connect(str(self.root/'data/autoarticle.sqlite'))
        self.addCleanup(c.close)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute("INSERT INTO articles VALUES ('b','in WAL')"); c.commit()
        self.export()
        with sqlite3.connect(str(self.bundle/'project/data/autoarticle.sqlite')) as copy:
            self.assertEqual(copy.execute('SELECT count(*) FROM articles').fetchone()[0],2)
            self.assertEqual(copy.execute('PRAGMA journal_mode').fetchone()[0],'delete')

    def test_input_changes_leave_incomplete_bundle(self):
        original=m.snapshot
        def changed(source,target):
            original(source,target)
            (self.root/'.operation-state/changed.json').write_text('{}')
        with patch.object(m,'snapshot',side_effect=changed):
            with self.assertRaises(ValueError): self.export()
        self.assertTrue((self.bundle/'INCOMPLETE').exists())
        with self.assertRaises(ValueError): m.verify_bundle(self.bundle)

    def test_restore_symlink_destination_rejected_before_writing(self):
        self.export()
        dest,home=self.clone()
        elsewhere=self.base/'elsewhere'; elsewhere.mkdir()
        (dest/'data').symlink_to(elsewhere,target_is_directory=True)
        with self.assertRaises(ValueError): m.restore_bundle(dest,home,self.bundle,True)
        self.assertFalse((dest/'.env').exists())
        self.assertEqual(list(elsewhere.iterdir()),[])

    def test_missing_stop_confirmation_is_rejected(self):
        with self.assertRaises(ValueError): ENSURE_STOPPED(self.root, False)

    def test_live_listener_is_rejected(self):
        from contextlib import nullcontext
        with patch.object(m.socket, 'create_connection', return_value=nullcontext()):
            with self.assertRaisesRegex(ValueError, 'still listening'):
                ENSURE_STOPPED(self.root, True)

    def test_wrong_commit_is_rejected_before_restore_writes(self):
        self.export()
        dest, home = self.clone()
        subprocess.check_call(['git','-C',str(dest),'-c','user.name=Fixture',
                               '-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','different'])
        with self.assertRaises(ValueError): m.restore_bundle(dest,home,self.bundle,True)
        self.assertFalse((dest/'.env').exists())
        self.assertFalse(home.exists())

    def test_interrupted_restore_leaves_marker_and_refuses_retry(self):
        self.export()
        dest, home = self.clone()
        with patch.object(m.shutil, 'copyfileobj', side_effect=OSError('simulated disk full')):
            with self.assertRaises(OSError): m.restore_bundle(dest,home,self.bundle,True)
        self.assertTrue((dest/'.migration/RESTORE_INCOMPLETE').exists())
        with self.assertRaises((ValueError,FileExistsError)): m.restore_bundle(dest,home,self.bundle,True)

    def test_custom_encryption_environment_does_not_leak_key(self):
        with patch.dict(os.environ, {'N8N_ENCRYPTION_KEY':'do-not-print-secret'}):
            with self.assertRaises(ValueError) as raised: m.standard_layout(self.root)
        self.assertNotIn('do-not-print-secret', str(raised.exception))

    def test_incomplete_restore_blocks_service_start(self):
        from autoarticle_ops import Operations
        from autoarticle_progress import Blocked
        marker=self.root/'.migration/RESTORE_INCOMPLETE'
        marker.parent.mkdir(); marker.write_text('incomplete')
        operations=Operations(root=self.root, run_date='2026-09-18', startup_env={})
        for service in ('n8n', 'dashboard'):
            with self.subTest(service=service), self.assertRaisesRegex(Blocked, 'pc_migration_restore_incomplete'):
                operations.start(service)


if __name__ == '__main__': unittest.main()
