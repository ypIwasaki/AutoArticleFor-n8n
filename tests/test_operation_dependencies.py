from __future__ import annotations
import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from autoarticle_progress import Progress

class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.p=Progress(self.root,'2026-09-10','local')
        self.put(self.p.generated()['structured-records'])
    def put(self,path,value='original'):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value);return p
    def checkpoint(self,step,evidence=None):
        for path in self.p.outputs(step):self.put(path)
        evidence=evidence or self.p.outputs(step)
        self.p.checkpoint(step,evidence,'reviewed')
        return self.p.load()['steps'][step]
    def test_keywords_change_only_keywords_and_weekly(self):
        entries={s:self.checkpoint(s) for s in ('summary','talent-review','classification-review','keywords','weekly')}
        self.put('config/keywords.json','changed')
        for step,entry in entries.items():
            self.assertEqual(self.p.current(entry),step not in ('keywords','weekly'),step)
    def test_taxonomy_changes_classification_not_summary_or_talent(self):
        entries={s:self.checkpoint(s) for s in ('summary','talent-review','classification-review')}
        self.put('config/article-classification-taxonomy.json','changed')
        for step,entry in entries.items():self.assertEqual(self.p.current(entry),step!='classification-review')
    def test_body_change_invalidates_all_article_reviews(self):
        entries=[self.checkpoint(s) for s in ('summary','talent-review','classification-review')]
        self.put('content/article-body-captures/2026-09-10.jsonl','changed')
        self.assertTrue(all(not self.p.current(e) for e in entries))
    def test_shared_policy_stays_coupled(self):
        entry=self.checkpoint('summary')
        self.put('docs/ai-rules/article-classification.md','changed')
        self.assertFalse(self.p.current(entry))
    def test_instruction_changes_only_related_review(self):
        summary=self.checkpoint('summary');talent=self.checkpoint('talent-review')
        self.put(self.p.generated()['ai-talent-index-instructions'],'changed')
        self.assertTrue(self.p.current(summary));self.assertFalse(self.p.current(talent))
    def test_explicit_extra_evidence_always_protected(self):
        self.put('config/keywords.json')
        entry=self.checkpoint('summary',['config/keywords.json'])
        self.put('config/keywords.json','changed')
        self.assertFalse(self.p.current(entry))
    def test_legacy_exact_no_silent_reapproval_or_writes(self):
        entry=self.checkpoint('summary')
        entry.pop('dependencyStep');entry.pop('dependencyVersion')
        entry['files']['config/keywords.json']=None
        before=self.p.file.read_bytes();self.put('config/keywords.json','changed')
        change=self.p.changes(entry)
        self.assertFalse(change['current']);self.assertEqual(change['dependencyMode'],'legacy_exact')
        self.assertEqual(before,self.p.file.read_bytes())
    def test_unknown_apply_preserves_dependencies(self):
        entry=self.checkpoint('talent-review')
        self.p.record('apply-talent','submission_unknown',**{k:v for k,v in entry.items() if k not in ('status','checkedAt','target')})
        old=self.p.load()['steps']['apply-talent']
        self.put('config/keywords.json','changed');self.assertTrue(self.p.current(old))
        self.put(self.p.outputs('talent-review')[0],'changed');self.assertFalse(self.p.current(old))
        self.assertEqual(self.p.load()['steps']['apply-talent']['status'],'submission_unknown')
    def test_new_dependency_baseline_missing_and_bounded_reasons(self):
        entry=self.checkpoint('summary');entry['files'].pop(self.p.generated()['structured-records'])
        self.assertIn('dependency_baseline_missing',self.p.changes(entry)['reasons'])
        for n in range(20):entry['files']['extra/%s'%n]='old'
        change=self.p.changes(entry)
        self.assertGreater(change['changedFileCount'],10);self.assertEqual(len(change['changedFiles']),10)

    def test_metadata_keywords_survive_body_and_shared_review_changes(self):
        entry=self.checkpoint('keywords')
        self.put('content/article-body-captures/2026-09-10.jsonl','new body')
        self.put('content/article-review-facts/2026-09-10.jsonl','new review')
        self.assertTrue(self.p.current(entry))
        self.put(self.p.generated()['structured-records'],'new titles')
        self.assertFalse(self.p.current(entry))
    def test_body_evidence_used_for_keywords_remains_protected(self):
        body='content/article-body-captures/2026-09-10.jsonl';self.put(body)
        entry=self.checkpoint('keywords',[body]);self.put(body,'changed')
        self.assertFalse(self.p.current(entry))
    def test_display_code_change_invalidates_checkpoint(self):
        page='apps/talent-dashboard/web/app.js';self.put(page)
        evidence='content/display.md';self.put(evidence)
        entry=self.checkpoint('page',[evidence]);self.put(page,'changed')
        self.assertFalse(self.p.current(entry))

if __name__=='__main__':unittest.main()
