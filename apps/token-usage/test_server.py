import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location('token_server', Path(__file__).with_name('server.py'))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
class ReportTests(unittest.TestCase):
    def test_only_valid_reports_and_no_source_details(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = dict(reportKind='autoarticle-token-usage',schemaVersion=1,workDate='2026-09-11',rows=[],sources=[{'path':'private-log'}])
            (root/'2026-09-11.json').write_text(json.dumps(report))
            (root/'2026-09-10.json').write_text('broken')
            (root/'private.json').write_text('{}')
            data = server.load_reports(root)
            self.assertEqual(len(data['reports']), 1)
            self.assertNotIn('sources', data['reports'][0])
            self.assertEqual(len(data['warnings']), 1)
    def test_existing_totals(self):
        for report in server.load_reports()['reports']:
            for metric, expected in report['knownTotals'].items():
                if isinstance(expected, (int,float)):
                    self.assertEqual(sum(row['tokens'].get(metric) or 0 for row in report['rows']), expected)
if __name__ == '__main__': unittest.main()
