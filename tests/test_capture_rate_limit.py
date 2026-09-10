import sys,unittest,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from email.utils import formatdate
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import capture_article_contents as c
class RateLimitTests(unittest.TestCase):
 def response(self):
  x=Mock();x.read.return_value=b'ok';x.geturl.return_value='https://publisher.example/a';x.headers={'Content-Type':'text/html'}
  opened=Mock();opened.__enter__=Mock(return_value=x);opened.__exit__=Mock(return_value=False);return opened
 def limited(self,headers=None):return HTTPError('https://publisher.example/a',429,'limited',headers or {},None)
 def setUp(self):
  self.clock=1000.
  self.patches=[patch.object(c.time,'time',side_effect=lambda:self.clock),patch.object(c.time,'sleep',side_effect=self.sleep)]
  for p in self.patches:p.start();self.addCleanup(p.stop)
 def sleep(self,seconds):self.clock+=seconds
 def test_retry_after_then_resume_and_cache(self):
  f=c.Fetch(5,2,4)
  with patch.object(c.request,'urlopen',side_effect=[self.limited({'Retry-After':'12'}),self.response()]) as opened:
   self.assertEqual(f('https://publisher.example/a')[0],b'ok')
   self.assertEqual(self.clock,1012)
   f('https://publisher.example/a');self.assertEqual(opened.call_count,2)
 def test_host_pacing_independent(self):
  f=c.Fetch(5,2,4)
  with patch.object(c.request,'urlopen',side_effect=[self.response() for _ in range(3)]):
   f('https://publisher.example/a');f('https://other.example/a');self.assertEqual(self.clock,1000)
   f('https://publisher.example/b');self.assertEqual(self.clock,1002)
 def test_date_retry_after(self):
  self.assertEqual(c.retry_delay({'Retry-After':formatdate(1030,usegmt=True)}),30)
  self.assertEqual(c.retry_delay({'Retry-After':'garbage'}),0)
 def test_long_wait_persisted_and_other_host_continues(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp)/'rate.json';f=c.Fetch(5,2,4,state)
   with patch.object(c.request,'urlopen',side_effect=[self.limited({'Retry-After':'120'}),self.response()]) as opened:
    with self.assertRaises(c.RateDeferred) as error:f('https://publisher.example/a')
    self.assertEqual(error.exception.until,1120)
    g=c.Fetch(5,2,4,state)
    with self.assertRaises(c.RateDeferred):g('https://publisher.example/b')
    g('https://other.example/a');self.assertEqual(opened.call_count,2)
    self.clock=1120
   with patch.object(c.request,'urlopen',return_value=self.response()):g('https://publisher.example/b')
 def test_retry_exhausted_is_due_next_run(self):
  f=c.Fetch(5,2,2)
  with patch.object(c.request,'urlopen',side_effect=[self.limited(),self.limited()]) as opened:
   with self.assertRaises(c.RateDeferred) as error:f('https://publisher.example/a')
   self.assertEqual(opened.call_count,2)
  entry={'retry_after':error.exception.until}
  self.assertFalse(c.eligible_article(entry,refresh=True));self.clock=entry['retry_after'];self.assertTrue(c.eligible_article(entry))
 def test_capture_retains_retry_schedule(self):
  article=Mock(url='https://publisher.example/a',title='Title',excerpt='',run_date='2026-09-10',published_at='')
  entry=c.capture(article,'a',Mock(side_effect=c.RateDeferred(1120)),{},[])
  self.assertEqual(entry['retry_after'],1120)
  self.assertEqual(entry['status'],'unavailable')
 def test_verified_reused_and_invalid_options(self):
  self.assertFalse(c.eligible_article({'status':'verified','content_text':'saved'},retry=True))
  for args in [(0,0,0),(-1,1,2),(float('nan'),1,2)]:
   with self.assertRaises(ValueError):c.Fetch(*args)
if __name__=='__main__':unittest.main()
