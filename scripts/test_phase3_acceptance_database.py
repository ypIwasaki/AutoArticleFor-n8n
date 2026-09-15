import contextlib,json,unittest
import test_import_legacy_database as fixtures
import import_legacy_database as imp
import accept_phase3_database as accept
import project_database as db

class AcceptanceTests(unittest.TestCase):
    def test_missing_population_uses_claims_not_fixed_count(self):
        raw=dict(body_length=449,content_hash=db.checksum(b'body'),status='verified')
        self.assertTrue(accept.missing_claim(raw))
        self.assertFalse(accept.missing_claim(dict(raw,body_length=0)))
        self.assertFalse(accept.missing_claim(dict(raw,content_text='body')))
        self.assertFalse(accept.missing_claim(dict(raw,content_hash=db.checksum(b''))))
    def test_cache_claim_enrichment_preserves_inputs_and_replays(self):
        helper=fixtures.LegacyImportTests();helper.setUp()
        try:
            snap=helper.fixture();entries={str(n):dict(original_url='https://example.test/'+str(n),body_length=n,content_hash=db.checksum(('body'+str(n)).encode()),status='verified',content_path='missing.jsonl') for n in (4,9)}
            helper.add_files(snap,{'content/article-body-captures/backfill-state.json':json.dumps(dict(entries=entries))})
            imp.import_snapshot(snap,helper.path)
            with contextlib.closing(db.connect(helper.path)) as c:
                before=[tuple(x) for x in c.execute('SELECT * FROM source_records ORDER BY id')]
                self.assertGreater(accept.reconcile(c,dict(result='user reports no matching body')),0)
                self.assertEqual(accept.verify(c)['derived_missing_body_count'],len(entries))
                self.assertEqual(before,[tuple(x) for x in c.execute('SELECT * FROM source_records ORDER BY id')])
                self.assertEqual(accept.reconcile(c,dict(result='user reports no matching body')),0)
                fingerprint=list(c.iterdump())
            self.assertTrue(all(x==0 for x in imp.import_snapshot(snap,helper.path)['row_deltas'].values()))
            with contextlib.closing(db.connect(helper.path)) as c:self.assertEqual(fingerprint,list(c.iterdump()))
        finally:helper.tearDown()

if __name__=='__main__':unittest.main()
