import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from autoarticle_apply import equal, verify
from autoarticle_progress import Blocked
class TimestampTests(unittest.TestCase):
    def test_sqlite_utc_and_javascript_millisecond_precision(self):
        self.assertTrue(equal("last_seen_at", "2026-09-10 07:05:23.032", "2026-09-10T16:05:23.032235+09:00"))
        self.assertTrue(equal("published_at", "2026-09-10 06:30:01.000", "2026-09-10T06:30:01Z"))
        self.assertTrue(equal("classified_at", "2026-09-10 07:05:23.032", "2026-09-10T07:05:23.032999Z"))
    def test_real_differences_and_ambiguous_values_fail(self):
        self.assertFalse(equal("classified_at", "2026-09-10 07:05:23.033", "2026-09-10T07:05:23.032999Z"))
        self.assertFalse(equal("published_at", "2026-09-10 07:05:23.032", "2026-09-10T07:05:23.032+09:00"))
        self.assertFalse(equal("last_seen_at", "2026-09-10 07:05:23.032", "2026-09-10T07:05:23"))
        self.assertFalse(equal("published_at", None, "2026-09-10T00:00:00Z"))
        self.assertFalse(equal("published_at", "invalid", "2026-09-10T00:00:00Z"))
        self.assertFalse(equal("title", "2026-09-10 06:30:01.000", "2026-09-10T06:30:01Z"))
    def test_classification_reconciliation_still_checks_content(self):
        row=dict(article_url="https://example.test", article_type="news_article", primary_category="event", secondary_categories_json=[], relevance="in_scope", confidence=0.9, evidence_text="body evidence", classification_method="ai_review", classified_at="2026-09-10T07:05:23.032235Z")
        stored={k:v for k,v in row.items() if k!="article_url"};stored.update(article_key="a", classified_at="2026-09-10 07:05:23.032")
        current=dict(articles=[dict(url=row["article_url"], article_key="a")], article_classifications=[stored])
        verify("classification",dict(classifications=[row]),current)
        stored["evidence_text"]="wrong evidence"
        with self.assertRaises(Blocked): verify("classification",dict(classifications=[row]),current)
