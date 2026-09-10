from __future__ import annotations
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import autoarticle_brief as brief
import autoarticle_ops as ops
from autoarticle_progress import ALL_STEPS, Blocked, Progress

class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.p=Progress(self.root,'2026-09-10','local')
        self.live={'progress':{step:'not_recorded' for step in ALL_STEPS},'issues':{},'dependencyChanges':{},'n8n':'reachable'}
        self.operator=Mock(root=self.root,progress=self.p)
        self.operator.resume.return_value=self.live
    def test_same_saved_state_independent_of_chat_or_diagnostic_files(self):
        one=brief.build(self.operator,'articles')
        path=self.root/'.operation-logs/diagnostics/old.json';path.parent.mkdir(parents=True);path.write_text('OLD SECRET INVESTIGATION'*10000)
        (self.root/'chat-history.txt').write_text('OLD CHAT'*10000)
        two=brief.build(self.operator,'articles')
        one.pop('checkedAt');two.pop('checkedAt')
        self.assertEqual(one,two);self.assertNotIn('SECRET',json.dumps(two))
        self.assertEqual(self.operator.resume.call_count,2)
    def test_scope_never_adds_collection_or_application(self):
        packet=brief.build(self.operator,'articles')
        self.assertNotIn('collect',packet['nextSteps'])
        self.assertNotIn('apply-talent',packet['nextSteps'])
        self.assertIn('selection_only',packet['authorization'])
    def test_noop_status_scope(self):
        packet=brief.build(self.operator,'status')
        self.assertEqual(packet['nextSteps'],[]);self.assertEqual(packet['steps'],[])
        self.assertFalse(self.p.file.exists())
    def test_unknown_outside_scope_still_guards_against_resubmit(self):
        self.p.record('apply-talent','submission_unknown',files={},workflowId='wf')
        self.live['progress']['apply-talent']='stale'
        packet=brief.build(self.operator,'prepare')
        guard=packet['submissionGuards'][0]
        self.assertEqual(guard['recordedState'],'submission_unknown')
        self.assertEqual(guard['checkedState'],'stale')
        self.assertNotIn('apply-talent',packet['nextSteps'])
    def test_bounded_note_and_evidence_with_changed_reason(self):
        self.p.record('summary','completed',files={},note='x'*1000,evidence=['item/%s'%i for i in range(15)])
        self.live['progress']['summary']='stale';self.live['issues']['summary']='dependency_changed'
        self.live['dependencyChanges']['summary']={'changedFiles':['source.jsonl']}
        item=brief.build(self.operator,'articles')['steps'][0]
        self.assertEqual(len(item['operatorNote']),180);self.assertTrue(item['operatorNoteTruncated'])
        self.assertEqual(len(item['evidence']),5);self.assertEqual(item['evidenceCount'],15)
        self.assertEqual(item['reason'],'dependency_changed')
    def test_save_does_not_change_operation_evidence_and_old_snapshot_not_loaded(self):
        self.p.record('collect','completed',files={},executionId='159')
        before=self.p.file.read_bytes()
        packet=brief.build(self.operator,'full',save=True)
        path=self.root/packet['savedSnapshot'];self.assertTrue(path.exists())
        self.assertEqual(self.p.file.read_bytes(),before)
        path.write_text('corrupt old snapshot')
        packet=brief.build(self.operator,'articles',save=True)
        self.assertEqual(json.loads(path.read_text())['scope'],'articles')
    def test_concurrent_evidence_change_blocks_snapshot(self):
        def changed():
            self.p.record('collect','submission_unknown',files={});return self.live
        self.operator.resume.side_effect=changed
        with self.assertRaisesRegex(Blocked,'changed_during_brief'):brief.build(self.operator,'full',save=True)
        self.assertFalse((self.root/'content/operation-start/2026-09-10.json').exists())
    def test_failed_live_check_does_not_reuse_snapshot(self):
        brief.build(self.operator,'full',save=True)
        self.operator.resume.side_effect=Blocked('live_failed')
        with self.assertRaisesRegex(Blocked,'live_failed'):brief.build(self.operator,'full')
    def test_cli_no_workflow_mutation(self):
        with patch.object(ops,'ROOT',self.root), patch.object(ops.Operations,'resume',return_value=self.live), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ops.main(['--date','2026-09-10','brief','--scope','articles']),0)
        self.assertEqual(json.loads(output.getvalue())['scope'],'articles')
        self.assertFalse(self.p.file.exists())

if __name__=='__main__':unittest.main()
