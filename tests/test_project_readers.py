import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project_database as db
import project_readers as readers
from compare_database_reader import compare, stamp

class ProjectReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'data/autoarticle.sqlite';db.migrate(self.path)
        self.c=db.connect(self.path);self.addCleanup(self.c.close)
        self.c.execute("INSERT INTO articles(id,title,url,created_at,updated_at,identity_state) VALUES ('article-test','Title','https://example.test','2026-09-16T00:00:00Z','2026-09-16T00:00:00Z','held')")
    def fetch(self, **changes):
        row=dict(id='fetch-test',article_id='article-test',version_id=None,status='unverified',source_status='verified',
                 stored_body_hash='saved-hash',stored_body_length=20,body_integrity='held_missing_body',
                 fetched_at='2026-09-16T00:00:00Z',original_url='https://example.test',resolved_url='',
                 completeness='',failure_reason='',extraction_method='',retry_after=None,
                 raw_json=json.dumps(dict(contentText='',contentMarkdown='',contentStatus='verified',extractionScope='body')))
        row.update(changes);return row
    def test_explicit_route_and_rollback_preserve_writes(self):
        for value in ('project-db','legacy','project-db'):
            readers.set_source('comparison',value,self.path)
            self.assertEqual(readers.source(self.root,'comparison'),value)
        self.assertEqual(self.c.execute("SELECT write_target FROM cutover_state WHERE feature='comparison'").fetchone()[0],'legacy')
    def test_missing_route_never_falls_back(self):
        self.c.execute("DELETE FROM cutover_state WHERE feature='dashboard'")
        with self.assertRaises(ValueError):readers.source(self.root,'dashboard')
    def test_held_body_uses_operational_status_and_null_version(self):
        value=readers.Reader(self.c).capture(self.fetch())
        self.assertEqual(value['contentStatus'],'unverified');self.assertEqual(value['contentText'],'')
        self.assertEqual(value['_project']['sourceStatus'],'verified')
        self.assertIsNone(value['_project']['contentVersionId']);self.assertEqual(value['extractionScope'],'body')
    def test_broken_version_is_rejected(self):
        with self.assertRaises(ValueError):readers.Reader(self.c).capture(self.fetch(version_id='missing-version'))
    def test_content_reads_payload_not_cached_raw(self):
        self.c.execute("INSERT INTO content_payloads(id,payload_hash,text,text_hash,markdown,metadata_json,raw_json) VALUES ('payload-test','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','actual body','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','actual markdown','{}','{}')")
        self.c.execute("INSERT INTO article_content_versions(id,article_id,payload_id,first_observed_at,last_observed_at) VALUES ('version-test','article-test','payload-test','2026-09-16T00:00:00Z','2026-09-16T00:00:00Z')")
        result=readers.Reader(self.c).capture(self.fetch(version_id='version-test',status='partial',body_integrity='consistent'))
        self.assertEqual(result['contentText'],'actual body');self.assertEqual(result['contentMarkdown'],'actual markdown')
        self.assertEqual(result['contentStatus'],'partial')
    def test_comparator_business_difference_and_timestamp_spelling(self):
        self.assertTrue(compare({'count':1},{'count':1})['equal'])
        self.assertFalse(compare({'count':1},{'count':2})['equal'])
        self.assertTrue(compare(stamp('2026-09-16T09:00:00+09:00'),stamp('2026-09-16T00:00:00Z'))['equal'])
        self.assertEqual(readers.formatted('2026-09-16 00:00:00.000','2026-09-16T00:00:00Z'),'2026-09-16 00:00:00.000')

if __name__=='__main__':unittest.main()
