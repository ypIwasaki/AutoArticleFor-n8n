import sys, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import capture_article_contents as c
class RateLimitTests(unittest.TestCase):
    def test_429_stops_same_host_but_allows_publishers(self):
        fetch=c.Fetch(0,0,4)
        response=Mock()
        response.read.return_value=b"ok"
        response.geturl.return_value="https://publisher.example/article"
        response.headers={"Content-Type":"text/html"}
        opened=Mock()
        opened.__enter__=Mock(return_value=response)
        opened.__exit__=Mock(return_value=False)
        with patch.object(c.request,"urlopen",side_effect=[HTTPError("https://news.google.com/a",429,"limited",{},None),opened]) as request:
            with self.assertRaisesRegex(c.old.CaptureError,"429"):
                fetch("https://news.google.com/a")
            with self.assertRaisesRegex(c.old.CaptureError,"未試行"):
                fetch("https://news.google.com/b")
            self.assertEqual(fetch("https://publisher.example/article")[0],b"ok")
            self.assertEqual(request.call_count,2)
