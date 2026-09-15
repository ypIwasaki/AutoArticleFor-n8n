import contextlib,json,unittest
import test_import_legacy_database as fixtures
import project_database as db
import import_legacy_database as imp
import database_phase1 as base
from compare_phase4_database import Difference
from compare_phase4_details import Details

class Phase4ComparisonTests(unittest.TestCase):
    def test_detects_dedicated_length_gap_even_when_raw_and_body_survive(self):
        h=fixtures.LegacyImportTests();h.setUp()
        try:
            snap=h.fixture();entry=dict(article_key='key-one',original_url='https://example.test/one',status='verified',content_text='Saved body',content_hash=db.checksum(b'Saved body'),body_length=10,processed_at='2026-09-14T00:00:00Z')
            h.add_files(snap,{'content/article-body-captures/backfill-state.json':json.dumps(dict(entries={'one':entry}))})
            imp.import_snapshot(snap,h.path)
            with contextlib.closing(db.connect(h.path)) as corrupt:
                corrupt.execute("UPDATE content_fetch_attempts SET stored_body_length=NULL WHERE id IN (SELECT target_id FROM source_records WHERE source_path LIKE '%backfill-state.json')")
            out=h.root/'compare';out.mkdir()
            with contextlib.closing(db.connect(h.path,readonly=True)) as c,contextlib.closing(base.ro(snap/'n8n.sqlite')) as old:
                compare=Details(c,old,snap/'files',out)
                with self.assertRaisesRegex(Difference,'stored_body_length'):compare.bodies()
                compare.diffs.close()
                self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0],1)
            diff=json.loads((out/'differences.jsonl').read_text().splitlines()[0]);self.assertEqual((diff['old_value'],diff['new_value'],diff['approval_status']),(10,None,'unapproved'))
        finally:h.tearDown()



class LengthAliasTests(unittest.TestCase):
    def test_alias_agreement_and_disagreement(self):
        self.assertEqual(imp.stored_length(dict(body_length=0)),0)
        self.assertEqual(imp.stored_length(dict(contentLength=8,content_length=8,body_length=8)),8)
        for raw in (dict(contentLength=1,body_length=2),dict(content_length=None,body_length=0)):
            with self.assertRaisesRegex(RuntimeError,'Conflicting saved body length'):imp.content_values(raw)
    def test_lengths_missing_body_and_zero_replay(self):
        h=fixtures.LegacyImportTests();h.setUp()
        try:
            snap=h.fixture();entries={str(n):dict(original_url='https://example.test/'+str(n),status='verified' if n else 'unavailable',content_text='Saved body' if n==10 else '',body_length=n,content_hash=db.checksum(b'Saved body') if n else db.checksum(b''),processed_at='2026-09-14T00:00:00Z') for n in (0,10,20)}
            h.add_files(snap,{'content/article-body-captures/backfill-state.json':json.dumps(dict(entries=entries))})
            imp.import_snapshot(snap,h.path)
            with contextlib.closing(db.connect(h.path,readonly=True)) as c:
                for row in c.execute("SELECT f.* FROM source_records s JOIN content_fetch_attempts f ON f.id=s.target_id WHERE s.source_path LIKE '%backfill-state.json'"):
                    raw=json.loads(row['raw_json']);self.assertEqual(row['stored_body_length'],raw['body_length'])
                    if raw['body_length']==20:self.assertEqual(row['body_integrity'],'held_missing_body');self.assertIsNone(row['version_id'])
                    elif raw['body_length']==0:self.assertEqual(row['body_integrity'],'no_body');self.assertEqual(row['stored_body_length'],0)
                    else:
                        length=c.execute('SELECT length(p.text) FROM article_content_versions v JOIN content_payloads p ON p.id=v.payload_id WHERE v.id=?',(row['version_id'],)).fetchone()[0];self.assertEqual(length,row['stored_body_length'])
            self.assertTrue(all(n==0 for n in imp.import_snapshot(snap,h.path)['row_deltas'].values()))
        finally:h.tearDown()
    def test_conflicting_alias_import_rolls_back(self):
        h=fixtures.LegacyImportTests();h.setUp()
        try:
            snap=h.fixture();h.add_files(snap,{'content/article-body-captures/backfill-state.json':json.dumps(dict(entries={'bad':dict(body_length=1,content_length=2,status='verified')}))})
            with self.assertRaisesRegex(RuntimeError,'Conflicting saved body length'):imp.import_snapshot(snap,h.path)
            with contextlib.closing(db.connect(h.path,readonly=True)) as c:self.assertEqual(c.execute('SELECT count(*) FROM source_records').fetchone()[0],0)
        finally:h.tearDown()

class NumericRepresentationTests(unittest.TestCase):
    def test_integral_legacy_real_length_is_explained_without_data_change(self):
        import sqlite3
        h=fixtures.LegacyImportTests();h.setUp()
        try:
            snap=h.fixture()
            with sqlite3.connect(snap/'n8n.sqlite') as source:
                source.execute('ALTER TABLE data_table_user_article_contents ADD COLUMN content_length REAL')
                source.execute('UPDATE data_table_user_article_contents SET content_length=10.0')
            meta=json.loads((snap/'phase1-result.json').read_text());meta['backup_sha256']=base.sha(snap/'n8n.sqlite');(snap/'phase1-result.json').write_text(json.dumps(meta))
            imp.import_snapshot(snap,h.path);out=h.root/'real-compare';out.mkdir()
            with contextlib.closing(db.connect(h.path,readonly=True)) as c,contextlib.closing(base.ro(snap/'n8n.sqlite')) as old:
                before=list(c.iterdump());audit=Details(c,old,snap/'files',out);audit.bodies();audit.diffs.close();self.assertEqual(before,list(c.iterdump()))
            rows=[json.loads(x) for x in (out/'differences.jsonl').read_text().splitlines()];self.assertEqual(len(rows),3);self.assertTrue(all(x['approval_status']=='approved' and x['old_value']==x['new_value'] for x in rows))
        finally:h.tearDown()

if __name__=='__main__':unittest.main()
