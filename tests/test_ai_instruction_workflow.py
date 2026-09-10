"""Execute the n8n Code node without n8n, network calls or generated file writes."""
from __future__ import annotations

import base64
from datetime import date
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n/workflows/daily-keyword-news-summary.workflow.json"
TASKS = {
    "ai-summary-instructions": ("article-summary", "article-summaries"),
    "ai-extraction-instructions": ("keyword-extraction", "ai-keyword-candidates"),
    "ai-talent-index-instructions": ("talent-index", "talent-index-proposals"),
    "ai-article-classification-instructions": (
        "article-classification", "article-classification-proposals"
    ),
    "ai-weekly-report-instructions": ("weekly-report", "weekly-reports"),
}
NODE_RUNNER = (
    "const fs = require('fs');"
    "const payload = JSON.parse(fs.readFileSync(0, 'utf8'));"
    "const result = new Function('$json', '$env', payload.code)(payload.input, payload.env || {PROJECT_ROOT: '/test/project'});"
    "process.stdout.write(JSON.stringify(result));"
)


def sample_input(count=1, generated_at="2026-09-09T15:01:00.000Z"):
    return {
        "generatedAt": generated_at,
        "keywords": ['監視キーワード「引用」"\\改行\n固有語'],
        "searchSources": ["テスト媒体"],
        "period": {"since": "2026-09-08T15:01:00.000Z", "until": generated_at},
        "articleCount": count,
        "digestMarkdown": "# Digest\nDIGEST_PAYLOAD_DO_NOT_EMBED",
        "llmPrompt": "PROMPT_PAYLOAD_DO_NOT_EMBED",
        "articles": [
            {
                "title": f'記事タイトル「引用」"\\改行\n{index} COVER',
                "url": f"https://example.test/articles/{index}?q=%E6%97%A5",
                "publishedAt": "2026-09-09T10:00:00.000Z",
                "source": "日本語ソース",
                "excerpt": "EXCERPT_PAYLOAD_DO_NOT_EMBED COVER 新企画",
            }
            for index in range(count)
        ],
    }


def decode_item(item):
    return base64.b64decode(item["binary"]["data"]["data"]).decode("utf-8")


def front_matter(markdown):
    # JSON values are valid YAML scalars / flow collections.
    header = markdown.split("---\n", 2)[1]
    return {
        key: json.loads(value.strip())
        for key, value in (line.split(":", 1) for line in header.splitlines())
    }


class AiInstructionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get("AUTOARTICLE_NODE") or shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required to execute the n8n Code node")
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.code = next(
            node["parameters"]["jsCode"]
            for node in workflow["nodes"]
            if node["name"] == "Build Markdown Files"
        )

    def run_code(self, data):
        result = subprocess.run(
            [self.node, "-e", NODE_RUNNER],
            input=json.dumps({"code": self.code, "input": data}, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=30,
        )
        return {
            item["json"]["fileKind"]: item for item in json.loads(result.stdout)
        }

    def test_output_contract_and_input_references(self):
        outputs = self.run_code(sample_input())
        self.assertEqual(set(outputs), set(TASKS) | {"daily-digest", "keyword-candidates"})
        for kind, (task, output_folder) in TASKS.items():
            with self.subTest(task=task):
                item = outputs[kind]
                date = "2026-09-07" if task == "weekly-report" else "2026-09-10"
                relative_path = f"content/{kind}/{date}.md"
                self.assertEqual(item["json"]["relativePath"], relative_path)
                self.assertTrue(item["json"]["filePath"].endswith("/" + relative_path))
                self.assertEqual(item["binary"]["data"]["fileName"], date + ".md")
                self.assertEqual(item["binary"]["data"]["mimeType"], "text/markdown")
                header = front_matter(decode_item(item))
                self.assertEqual(header["instructionVersion"], 2)
                self.assertEqual(header["task"], task)
                self.assertEqual(header["rulesPath"], f"docs/ai-rules/{task}.md")
                self.assertEqual(header["resultRulesPath"], "docs/ai-rules/operation-result.md")
                self.assertIn("保存・検証後の運用報告は resultRulesPath", decode_item(item))
                if task in {"talent-index", "article-classification"}:
                    self.assertEqual(header["outputPaths"], {
                        "markdown": f"content/{output_folder}/{date}.md",
                        "json": f"content/{output_folder}/{date}.json",
                    })
                else:
                    self.assertEqual(header["outputPath"], f"content/{output_folder}/{date}.md")
                if task != "weekly-report":
                    self.assertEqual(header["sourceRunDate"], "2026-09-10")
                    records = "content/structured-records/2026-09-10.jsonl"
                    self.assertEqual(header["sourceStructuredRecords"], records)
                    self.assertEqual(header["runMetadataSource"], records)
                    self.assertEqual(header["sourceBodyCaptures"], "content/article-body-captures/2026-09-10.jsonl")
                    self.assertIn("recordType: run", decode_item(item))
        summary = front_matter(decode_item(outputs["ai-summary-instructions"]))
        extraction = front_matter(decode_item(outputs["ai-extraction-instructions"]))
        classification = front_matter(decode_item(outputs["ai-article-classification-instructions"]))
        self.assertEqual(summary["sourceDigest"], "content/daily-digests/2026-09-10.md")
        self.assertEqual(extraction["sourceDigest"], summary["sourceDigest"])
        self.assertEqual(extraction["ruleBasedCandidates"], "content/keyword-candidates/2026-09-10.md")
        self.assertEqual(classification["sourceTaxonomy"], "config/article-classification-taxonomy.json")

    def test_instruction_size_does_not_grow_with_article_data(self):
        small = self.run_code(sample_input())
        large_input = sample_input(400)
        large_input["articles"][0]["excerpt"] += "長い本文" * 10000
        large = self.run_code(large_input)
        for kind in TASKS:
            with self.subTest(kind=kind):
                markdown = decode_item(large[kind])
                self.assertEqual(markdown, decode_item(small[kind]))
                self.assertLess(len(markdown.encode("utf-8")), 5000)
                for payload in (
                    "DIGEST_PAYLOAD_DO_NOT_EMBED", "PROMPT_PAYLOAD_DO_NOT_EMBED",
                    "EXCERPT_PAYLOAD_DO_NOT_EMBED", "監視キーワード", "記事タイトル",
                    "https://example.test/articles/", "## 分類体系",
                ):
                    self.assertNotIn(payload, markdown)
        digest = decode_item(large["daily-digest"])
        self.assertIn(large_input["articles"][0]["title"], digest)
        self.assertIn("DIGEST_PAYLOAD_DO_NOT_EMBED", digest)
        self.assertIn("EXCERPT_PAYLOAD_DO_NOT_EMBED", digest)
        candidates = decode_item(large["keyword-candidates"])
        self.assertIn("COVER", candidates)
        self.assertIn(large_input["keywords"][0], candidates)

    def test_zero_articles_generate_reference_instructions(self):
        outputs = self.run_code(sample_input(0))
        self.assertEqual(len(outputs), 7)
        for kind in TASKS:
            if kind != "ai-weekly-report-instructions":
                self.assertEqual(outputs[kind]["json"]["articleCount"], 0)
            self.assertEqual(front_matter(decode_item(outputs[kind]))["instructionVersion"], 2)
        self.assertIn("No articles were captured", decode_item(outputs["daily-digest"]))
        self.assertEqual(outputs["keyword-candidates"]["json"]["candidateCount"], 0)

    def test_weekly_dates_do_not_claim_files_exist(self):
        item = self.run_code(sample_input())["ai-weekly-report-instructions"]
        markdown = decode_item(item)
        header = front_matter(markdown)
        self.assertEqual(header["weekStart"], "2026-09-07")
        self.assertEqual(header["weekEnd"], "2026-09-13")
        self.assertEqual(header["coveredThrough"], "2026-09-10")
        self.assertEqual(header["sourceRunDate"], "2026-09-10")
        self.assertEqual(header["sourceDates"], [
            "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"
        ])
        self.assertEqual(header["reportPath"], header["outputPath"])
        self.assertEqual(item["json"]["sourceWeeklyReport"], header["reportPath"])
        self.assertEqual(header["sourceStructuredRecordsPattern"], "content/structured-records/{date}.jsonl")
        self.assertEqual(header["sourceFeedbackDirectory"], "content/article-feedback-instructions/")
        self.assertEqual(header["sourceMetricsPattern"], "content/analysis/weekly-metrics/{isoWeek}.json")
        self.assertIn("python3 scripts/generate_analysis_reports.py --through 2026-09-10", markdown)
        self.assertIn("--weekly-articles", markdown)
        self.assertIn("存在するファイルだけ", markdown)
        self.assertIn("収集成功を保証しない", markdown)
        self.assertIn("欠損を明記", markdown)

    def test_jst_week_boundary_and_year_rollover(self):
        for timestamp, start, end, count in (
            ("2027-01-03T14:59:00.000Z", "2026-12-28", "2027-01-03", 7),
            ("2027-01-03T15:01:00.000Z", "2027-01-04", "2027-01-10", 1),
        ):
            with self.subTest(timestamp=timestamp):
                outputs = self.run_code(sample_input(0, timestamp))
                header = front_matter(decode_item(outputs["ai-weekly-report-instructions"]))
                self.assertEqual(header["weekStart"], start)
                self.assertEqual(header["weekEnd"], end)
                self.assertEqual(len(header["sourceDates"]), count)
                self.assertEqual(header["sourceDates"][-1], header["coveredThrough"])

    def test_reader_commands_match_task_and_run_date(self):
        outputs = self.run_code(sample_input())
        for kind, (task, _) in TASKS.items():
            with self.subTest(task=task):
                self.assertIn(
                    "python3 scripts/read_ai_inputs.py --run-date 2026-09-10"
                    f" --task {task}" + (" --as-of 2026-09-10" if task == "weekly-report" else " --offset 0 --limit 20"),
                    decode_item(outputs[kind]),
                )

    def test_shared_review_references_only_for_supported_tasks(self):
        outputs = self.run_code(sample_input())
        for kind, (task, _) in TASKS.items():
            markdown = decode_item(outputs[kind])
            header = front_matter(markdown)
            with self.subTest(task=task):
                if task in {"article-summary", "talent-index", "article-classification"}:
                    self.assertEqual(header["inputMode"], "shared-review-first")
                    self.assertEqual(header["sharedReviewRulesPath"], "docs/ai-rules/shared-article-review.md")
                    self.assertEqual(header["sourceSharedReviewsPattern"], "content/article-review-facts/{date}.jsonl")
                    self.assertEqual(header["sharedReviewOutputPath"], "content/article-review-facts/2026-09-10.jsonl")
                    self.assertEqual(header["sharedReviewWriter"], "scripts/save_article_review_facts.py")
                    self.assertIn("current/held", markdown)
                    self.assertIn("missing/stale/invalid/needs_review", markdown)
                    self.assertIn("--include-body", markdown)
                    self.assertIn("未確認の工程は ready にしない", markdown)
                else:
                    self.assertNotIn("sharedReviewWriter", header)
                    self.assertEqual(header["inputMode"], "weekly-metrics" if task == "weekly-report" else "article-metadata")
                for name in ("rulesPath", "resultRulesPath", "sharedReviewRulesPath", "sharedReviewWriter"):
                    if name in header:
                        self.assertTrue((ROOT / header[name]).is_file())

    def test_weekly_metrics_path_matches_python_iso_calendar(self):
        for timestamp in ("2026-09-09T15:01:00.000Z", "2027-01-03T14:59:00.000Z",
                          "2027-01-03T15:01:00.000Z", "2024-12-30T00:00:00.000Z"):
            with self.subTest(timestamp=timestamp):
                item = self.run_code(sample_input(0, timestamp))["ai-weekly-report-instructions"]
                header = front_matter(decode_item(item))
                year, week, _ = date.fromisoformat(header["coveredThrough"]).isocalendar()
                self.assertEqual(header["sourceMetricsPath"], f"content/analysis/weekly-metrics/{year}-W{week:02d}.json")
                self.assertEqual(header["reviewAsOf"], header["coveredThrough"])
                self.assertEqual(header["sourceClassificationsJsonPattern"], "content/article-classification-proposals/{date}.json")
                self.assertEqual(header["sourceFeedbackSnapshotPattern"], "content/article-feedback-instructions/{date}.json")

    def test_generated_weekly_commands_run_end_to_end_in_temporary_project(self):
        markdown = decode_item(self.run_code(sample_input(0))["ai-weekly-report-instructions"])
        commands = [shlex.split(line) for line in markdown.splitlines() if line.startswith("python3 ")]
        self.assertEqual(len(commands), 4)
        self.assertNotIn("--offset", commands[1])
        self.assertNotIn("operation_result.py", " ".join(commands[1]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Only test fixtures and generated temporary outputs; no real archives/DB.
            for name in ("generate_analysis_reports.py", "weekly_metrics.py", "read_ai_inputs.py",
                         "article_review_facts.py", "article_feedback_snapshot.py", "operation_result.py"):
                path = root / "scripts" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "scripts" / name, path)
            for path in (ROOT / "docs/ai-rules").glob("*.md"):
                target = root / "docs/ai-rules" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            records = root / "content/structured-records/2026-09-10.jsonl"
            records.parent.mkdir(parents=True)
            records.write_text(json.dumps({"recordType": "run", "runDate": "2026-09-10",
                                          "articleCount": 0, "capturedArticleCount": 0, "keywords": []}) + "\n", encoding="utf-8")
            outputs = []
            for index, command in enumerate(commands):
                if index == 2:
                    report = root / "content/weekly-reports/2026-09-07.md"
                    report.parent.mkdir(parents=True)
                    report.write_text("# Research\n\n考察を維持する。\n", encoding="utf-8")
                command[0] = sys.executable
                if "--" in command:
                    command[command.index("--") + 1] = sys.executable
                response = subprocess.run(command, cwd=root, text=True, encoding="utf-8",
                                          capture_output=True, timeout=30)
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                outputs.append(json.loads(response.stdout))
            self.assertEqual(outputs[1]["articles"], [])
            self.assertEqual(outputs[1]["metrics"]["counts"]["archivedRecords"], 0)
            self.assertEqual(outputs[3]["processStatus"], "exited_ok")
            self.assertTrue(outputs[3]["summary"]["checked"])
            self.assertEqual(outputs[3]["summary"]["resultStatus"], "provisional")
            self.assertTrue(outputs[3]["attentionRequired"])
            self.assertIn("考察を維持する。", report.read_text(encoding="utf-8"))
            self.assertEqual(report.read_text().count("<!-- weekly-metrics:start -->"), 1)


if __name__ == "__main__":
    unittest.main()
