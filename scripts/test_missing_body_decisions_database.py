"""Read retained evidence into small memory fixtures; never mutate evidence DBs."""
import copy,json,sqlite3,unittest
from contextlib import closing
import project_database as db
import missing_body_acceptance as acceptance

class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.ledger=acceptance.load(db.ROOT/acceptance.LEDGER_PATH)
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        retained=db.ROOT/'.operation-state/database-phase5/20260915T024435Z/project.sqlite'
        if not retained.exists():self.skipTest('Retained phase4 evidence required for exact 13-row regression')
        with closing(db.connect(retained,readonly=True)) as old:
            sources=[s for s,raw in acceptance.candidates(old)]
            fetches=[dict(old.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(s['target_id'],)).fetchone()) for s in sources]
            articles=[dict(old.execute('SELECT * FROM articles WHERE id=?',(f['article_id'],)).fetchone()) for f in fetches]
            conflicts=[dict(x) for x in old.execute("SELECT * FROM consolidation_conflicts WHERE kind='body_integrity'")]
            for table,rows in [('source_records',sources),('content_fetch_attempts',fetches),('articles',articles),('consolidation_conflicts',conflicts)]:
                self.c.execute(old.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0])
                for row in {x['id']:x for x in rows}.values():
                    self.c.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.expected=list(self.c.execute('SELECT id,details_json FROM consolidation_conflicts ORDER BY id'))
        for row in self.expected:
            d=json.loads(row['details_json']);d.pop('acceptance_research',None);d.pop('preserved_missing_body_claim',None)
            self.c.execute('UPDATE consolidation_conflicts SET details_json=? WHERE id=?',(db.canonical(d),row['id']))
        self.c.commit()
    def tearDown(self):self.c.close()
    def test_exact_thirteen_annotations_and_replay(self):
        result=acceptance.apply(self.c,self.ledger,'fixture-hash');self.assertEqual(result['updated_rows'],13)
        self.assertEqual([tuple(x) for x in self.expected],[tuple(x) for x in self.c.execute('SELECT id,details_json FROM consolidation_conflicts ORDER BY id')])
        self.assertEqual(acceptance.apply(self.c,self.ledger,'fixture-hash')['updated_rows'],0)
    def test_population_and_claim_mismatches_never_mutate(self):
        before=list(self.c.iterdump())
        for field in ('input_hash','stored_body_hash','stored_body_length'):
            ledger=copy.deepcopy(self.ledger);ledger['decisions'][0][field]='different'
            with self.assertRaises(RuntimeError):acceptance.apply(self.c,ledger,'fixture')
            self.assertEqual(before,list(self.c.iterdump()))
        for mode in ('missing','extra'):
            ledger=copy.deepcopy(self.ledger)
            if mode=='missing':ledger['decisions'].pop()
            else:ledger['decisions'].append(dict(ledger['decisions'][0],decision_id='unregistered'))
            with self.assertRaises(RuntimeError):acceptance.apply(self.c,ledger,'fixture')
            self.assertEqual(before,list(self.c.iterdump()))
    def test_nonmissing_fetch_rejected(self):
        self.c.execute("UPDATE content_fetch_attempts SET body_integrity='no_body' WHERE id=(SELECT id FROM content_fetch_attempts LIMIT 1)")
        with self.assertRaises(RuntimeError):acceptance.apply(self.c,self.ledger,'fixture')
    def test_other_conflicts_untouched_and_no_target_database_argument(self):
        self.c.execute("UPDATE consolidation_conflicts SET id='unrelated-conflict',kind='other' WHERE id=(SELECT id FROM consolidation_conflicts LIMIT 1)")
        before=list(self.c.iterdump())
        with self.assertRaises(RuntimeError):acceptance.apply(self.c,self.ledger,'fixture')
        self.assertEqual(before,list(self.c.iterdump()))
        import inspect
        self.assertEqual(list(inspect.signature(acceptance.apply).parameters),['c','ledger','ledger_hash'])

    def test_new_capture_conflicts_do_not_silently_duplicate_acceptance(self):
        path=db.ROOT/'.operation-state/database/normal-articles-20260916/summary-snapshot/sync-request.json'
        if not path.exists():self.skipTest('Retained stopped summary request required')
        request=json.loads(path.read_text());new=[x['after'] for x in request['changes'] if x['entity']=='conflict' and x['after']['kind']=='body_integrity']
        self.assertEqual(len(new),2)
        acceptance.apply(self.c,self.ledger,'fixture-hash')
        for row in new:self.c.execute('INSERT INTO consolidation_conflicts('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
        before=list(self.c.iterdump())
        with self.assertRaisesRegex(RuntimeError,'acceptance_conflict_not_unique'):acceptance.plan(self.c,self.ledger)
        self.assertEqual(before,list(self.c.iterdump()))

if __name__=='__main__':unittest.main()
