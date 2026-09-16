import contextlib,json,unittest
import test_import_legacy_database as fixtures
import import_legacy_database as imp
import accept_phase3_database as accept
import project_database as db
import missing_body_acceptance as common

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
                decisions=[]
                for source,raw in accept.candidates(c):
                    v,length=accept.claims(raw)
                    decisions.append(dict(decision_id='missing-body:'+source['id'],subject='missing_body',body_integrity='held_missing_body',status='unverified',preserve_source_status=True,create_payload=False,create_version=False,source_path=source['source_path'],record_position=source['record_position'],input_hash=source['input_hash'],stored_body_hash=v['stored_hash'],stored_body_length=length,source_status=v['status'],conflict_kind='body_integrity',approval_reference='test-fixture'))
                ledger=snap/'decisions.json';ledger.write_text(json.dumps(dict(format_version=common.VERSION,research=dict(result='user reports no matching body'),decisions=decisions)))
                self.assertGreater(accept.reconcile(c,ledger),0)
                self.assertEqual(accept.verify(c)['derived_missing_body_count'],len(entries))
                self.assertEqual(before,[tuple(x) for x in c.execute('SELECT * FROM source_records ORDER BY id')])
                self.assertEqual(accept.reconcile(c,ledger),0)
                fingerprint=list(c.iterdump())
            self.assertTrue(all(x==0 for x in imp.import_snapshot(snap,helper.path)['row_deltas'].values()))
            with contextlib.closing(db.connect(helper.path)) as c:self.assertEqual(fingerprint,list(c.iterdump()))
        finally:helper.tearDown()

if __name__=='__main__':unittest.main()
