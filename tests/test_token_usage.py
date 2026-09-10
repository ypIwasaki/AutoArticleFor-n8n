from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import autoarticle_ops as ops
import autoarticle_tokens as tokens
import codex_usage_log as usage
import token_usage_report as reports
from autoarticle_progress import Blocked, read_json, write_json

THREAD = "test-thread-id"
DATE = "2026-09-10"


def at(second):
    return "2026-09-10T00:00:%02d+00:00" % second


def counter(number, **overrides):
    value = {"input_tokens": number, "output_tokens": number, "total_tokens": number * 2,
             "cached_input_tokens": number // 2, "cache_write_input_tokens": 0, "reasoning_output_tokens": number // 2}
    value.update(overrides)
    return value


def event(second, number, **overrides):
    return {"timestamp": at(second), "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": counter(number, **overrides), "last_token_usage": counter(999999)}, "rate_limits": {"private": "DO_NOT_SAVE"}}}


class UsageFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "selected-rollout.jsonl"
        self.log.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD, "cwd": "SECRET_PATH"}}) + "\n")
        self.append(event(0, 100))
        self.clock = Mock(return_value=at(1))
        self.tracker = tokens.Tracker(self.root, self.clock)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def append(self, value):
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def bind(self):
        self.tracker.bind(THREAD, str(self.log))

    def report(self):
        source = self.tracker.selected()
        self.tracker.refresh(source)
        self.tracker.save(source)
        return reports.build_report([source], DATE, self.clock())


class ReaderTests(UsageFixture):
    def test_cumulative_difference_not_last_usage_or_duplicate_sum(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        self.append(event(2, 110))
        self.append(event(3, 110))
        self.append(event(4, 125))
        new, samples, issues, pending = usage.advance(self.log, THREAD, cursor)
        self.assertEqual([s["tokens"]["total_tokens"] for s in samples], [20, 30])
        self.assertEqual(samples[-1]["before"]["total_tokens"], 220)
        self.assertEqual(samples[-1]["after"]["total_tokens"], 250)
        self.assertEqual(issues, [])
        self.assertFalse(pending)
        self.assertEqual(usage.advance(self.log, THREAD, new)[1], [])

    def test_malformed_sample_can_be_recovered_from_before_and_after(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        self.append(event(2, 110, input_tokens="unknown"))
        self.append(event(3, 130))
        _, samples, issues, _ = usage.advance(self.log, THREAD, cursor)
        self.assertEqual(samples[0]["tokens"]["total_tokens"], 60)
        self.assertTrue(samples[0]["gap"])
        self.assertEqual(samples[0]["calculation"], "after_minus_before")
        self.assertTrue(issues)

    def test_counter_reset_is_not_negative_usage_or_historical_sum(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        self.append(event(2, 5))
        self.append(event(3, 9))
        _, samples, issues, _ = usage.advance(self.log, THREAD, cursor)
        self.assertEqual([s["tokens"]["total_tokens"] for s in samples], [8])
        self.assertEqual(issues[0]["reason"], "usage_counter_reset")

    def test_partial_last_line_is_retried_once(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        line = json.dumps(event(2, 110))
        with self.log.open("a") as stream:
            stream.write(line[:30])
        new, samples, _, pending = usage.advance(self.log, THREAD, cursor)
        self.assertEqual(new["offset"], cursor["offset"])
        self.assertEqual(samples, [])
        self.assertTrue(pending)
        with self.log.open("a") as stream:
            stream.write(line[30:] + "\n")
        final, samples, _, pending = usage.advance(self.log, THREAD, new)
        self.assertEqual(len(samples), 1)
        self.assertFalse(pending)
        self.assertEqual(usage.advance(self.log, THREAD, final)[1], [])

    def test_log_replacement_and_wrong_thread_fail_closed(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        with self.assertRaisesRegex(Blocked, "thread_mismatch"):
            usage.initial_cursor(self.log, "other")
        original = self.log.read_text()
        self.log.write_text(original.replace('"input_tokens": 100', '"input_tokens": 101'))
        with self.assertRaisesRegex(Blocked, "replaced_or_truncated"):
            usage.advance(self.log, THREAD, cursor)

    def test_missing_baseline_and_optional_details_are_unknown_not_zero(self):
        self.log.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD}}) + "\n")
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        self.append(event(2, 100, cached_input_tokens=None, reasoning_output_tokens=None))
        self.append(event(3, 105, cached_input_tokens=None, reasoning_output_tokens=None))
        _, samples, issues, _ = usage.advance(self.log, THREAD, cursor)
        self.assertEqual(samples[0]["tokens"]["total_tokens"], 10)
        self.assertIsNone(samples[0]["tokens"]["cached_input_tokens"])
        self.assertEqual(issues[0]["reason"], "usage_baseline_missing")

    def test_body_and_rate_limits_are_not_retained(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        self.append({"type": "response_item", "payload": {"text": "PRIVATE_BODY token_count event_msg"}})
        self.append({"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": "PRIVATE"}})
        self.append(event(2, 110))
        result = usage.advance(self.log, THREAD, cursor)
        serialized = json.dumps(result)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("DO_NOT_SAVE", serialized)

    def test_invalid_totals_and_boolean_tokens_are_rejected(self):
        for value in (counter(5, total_tokens=999), counter(5, input_tokens=True), counter(5, reasoning_output_tokens=6)):
            with self.assertRaises(Blocked):
                usage.counters(value)

    def test_batch_limits_do_not_drop_usage(self):
        cursor, _ = usage.initial_cursor(self.log, THREAD)
        self.append(event(2, 110))
        self.append(event(3, 120))
        with patch.object(usage, "BATCH_BYTES", 1):
            middle, samples, _, pending = usage.advance(self.log, THREAD, cursor)
        self.assertEqual(len(samples), 1)
        self.assertTrue(pending)
        _, next_samples, _, _ = usage.advance(self.log, THREAD, middle)
        self.assertEqual(sum(s["tokens"]["total_tokens"] for s in samples + next_samples), 40)


class TrackingTests(UsageFixture):
    def test_begin_switch_end_and_repeated_begin(self):
        self.bind()
        first = self.tracker.begin("summary", DATE)
        self.assertEqual(self.tracker.begin("summary", DATE)["spanId"], first["spanId"])
        self.clock.return_value = at(3)
        self.tracker.begin("talent-review", DATE)
        source = self.tracker.selected()
        self.assertEqual(source["spans"][0]["outcome"], "switched")
        self.assertEqual(len(source["spans"]), 2)
        with self.assertRaisesRegex(Blocked, "mismatch"):
            self.tracker.end("summary", DATE)
        self.tracker.end("talent-review", DATE)
        self.assertIsNone(self.tracker.active_span(self.tracker.selected()))

    def test_unbound_record_is_visible_but_never_zero(self):
        self.tracker.begin("summary", DATE)
        result = self.report()
        self.assertIsNone(result["rows"][0]["tokens"]["total_tokens"])
        self.assertEqual(result["sources"][0]["status"], "log_not_bound")
        self.assertIn("未計測", reports.render(result))

    def test_binding_rejects_other_task_and_never_selects_latest(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "some-other-task"}):
            with self.assertRaisesRegex(Blocked, "current_thread"):
                self.tracker.bind(THREAD, str(self.log))
        with self.assertRaisesRegex(Blocked, "current_thread_id_required"):
            self.tracker.bind()

    def test_closed_scope_does_not_absorb_later_conversation(self):
        self.bind()
        self.tracker.begin("summary", DATE)
        self.append(event(2, 110))  # Start-boundary interval: quantity known, split unknown.
        self.append(event(3, 125))  # Wholly inside summary.
        self.clock.return_value = at(4)
        self.tracker.finish()
        self.append(event(5, 140))  # End boundary: quantified, excluded from totals.
        self.append(event(6, 500))  # Entirely outside operation: omitted.
        report = self.report()
        self.assertEqual(report["knownTotals"]["total_tokens"], 50)
        self.assertEqual(len(report["intervals"]), 3)
        self.assertFalse(report["intervals"][-1]["includedInTotals"])
        self.assertEqual(report["intervals"][-1]["tokens"]["total_tokens"], 30)

    def test_resume_refresh_is_idempotent_and_can_receive_delayed_samples(self):
        self.bind()
        self.tracker.begin("summary", DATE)
        self.clock.return_value = at(5)
        self.tracker.end("summary", DATE)
        self.append(event(2, 110))
        self.append(event(3, 120))
        report = self.report()
        again = self.report()
        self.assertEqual(report["knownTotals"], again["knownTotals"])
        self.assertEqual(len(again["intervals"]), 2)
        source = self.tracker.selected()
        self.assertEqual(len(source["samples"]), 2)

    def test_missing_log_preserves_previous_observations_and_warns(self):
        self.bind()
        self.tracker.begin("summary", DATE)
        self.append(event(2, 110))
        self.report()
        self.log.unlink()
        report = self.report()
        self.assertEqual(report["knownTotals"]["total_tokens"], 20)
        self.assertEqual(report["sources"][0]["status"], "token_log_unavailable")

    def test_rebinding_same_log_does_not_reset_usage(self):
        self.bind()
        self.tracker.begin("summary", DATE)
        self.append(event(2, 110))
        self.report()
        self.bind()
        self.assertEqual(self.report()["knownTotals"]["total_tokens"], 20)

    def test_checkpoint_ends_matching_span_only(self):
        self.bind()
        self.tracker.begin("summary", DATE)
        self.assertEqual(self.tracker.end("talent-review", DATE, checkpoint=True)["reason"], "checkpoint_span_mismatch")
        self.assertIsNotNone(self.tracker.active_span(self.tracker.selected()))
        self.assertEqual(self.tracker.end("summary", DATE, outcome="checkpoint_saved", checkpoint=True)["status"], "ended")

    def test_report_generation_is_private_and_rebuildable(self):
        self.bind()
        self.tracker.begin("summary", DATE)
        self.append(event(2, 110))
        result = self.tracker.reports(DATE)
        path = Path(result["reports"][0])
        markdown = path.read_text()
        public = path.with_suffix(".json").read_text()
        for secret in ("PRIVATE_BODY", "SECRET_PATH", "DO_NOT_SAVE", str(self.log), THREAD):
            self.assertNotIn(secret, markdown + public)
        self.assertIn("前の累積合計", markdown)
        self.assertIn("差分", markdown)
        path.unlink()
        self.tracker.reports(DATE)
        self.assertEqual(path.read_text(), markdown)

    def test_existing_user_markdown_is_not_overwritten(self):
        report = reports.build_report([], DATE, at(1))
        folder = self.root / "content/operation-usage"
        folder.mkdir(parents=True)
        path = folder / (DATE + ".md")
        path.write_text("User notes")
        with self.assertRaisesRegex(Blocked, "non_generated"):
            reports.save_report(self.root, report)
        self.assertEqual(path.read_text(), "User notes")


class AttributionTests(UsageFixture):
    def sample(self, start, end, before=100, after=110, gap=False):
        return {"offset": 1, "from": at(start), "at": at(end), "before": counter(before), "after": counter(after),
                "tokens": {k: counter(after)[k] - counter(before)[k] for k in usage.FIELDS}, "gap": gap}

    def source(self):
        return {"key": "source-" + "a" * 16, "sourceStatus": "readable", "lastReadAt": at(20), "issues": [],
                "windows": [{"id": "w", "start": at(1), "end": at(19), "runDate": "2026-09-03"}],
                "spans": [{"id": "a", "windowId": "w", "step": "summary", "start": at(2), "end": at(7), "runDate": "2026-09-03"},
                          {"id": "b", "windowId": "w", "step": "talent-review", "start": at(7), "end": at(12), "runDate": "2026-09-03"}], "samples": []}

    def test_cross_step_quantity_is_computed_but_not_arbitrarily_split(self):
        source = self.source()
        source["samples"] = [self.sample(5, 9)]
        report = reports.build_report([source], DATE, at(20))
        item = report["intervals"][0]
        self.assertEqual(item["step"], "unassigned")
        self.assertEqual(item["tokens"]["total_tokens"], item["after"]["total_tokens"] - item["before"]["total_tokens"])
        self.assertEqual(item["candidates"], ["summary", "talent-review"])
        self.assertEqual(report["knownTotals"]["total_tokens"], 20)

    def test_recovered_gap_is_assigned_when_contained_in_one_step(self):
        source = self.source()
        source["samples"] = [self.sample(3, 6, gap=True)]
        report = reports.build_report([source], DATE, at(20))
        self.assertEqual(report["intervals"][0]["step"], "summary")
        self.assertEqual(report["intervals"][0]["measurement"], "derived_across_gap")

    def test_between_steps_is_common_and_totals_do_not_double_details(self):
        source = self.source()
        source["samples"] = [self.sample(13, 16)]
        report = reports.build_report([source], DATE, at(20))
        self.assertEqual(report["intervals"][0]["step"], "common")
        totals = report["knownTotals"]
        self.assertEqual(totals["input_tokens"] + totals["output_tokens"], totals["total_tokens"])
        self.assertNotEqual(sum(totals.values()), totals["total_tokens"])

    def test_work_date_is_not_article_date(self):
        source = self.source()
        source["samples"] = [self.sample(3, 5)]
        report = reports.build_report([source], DATE, at(20))
        self.assertEqual(report["workDate"], DATE)
        self.assertEqual(report["rows"][0]["runDate"], "2026-09-03")

    def test_cross_midnight_uses_notification_day_and_remains_unassigned(self):
        source = self.source()
        source["windows"][0].update(start="2026-09-10T14:00:00Z", end="2026-09-10T16:00:00Z")
        source["spans"] = []
        sample = self.sample(3, 5)
        sample.update({"from": "2026-09-10T14:59:59Z", "at": "2026-09-10T15:00:01Z"})
        source["samples"] = [sample]
        before = reports.build_report([source], DATE, "2026-09-10T16:01:00Z")
        after = reports.build_report([source], "2026-09-11", "2026-09-10T16:01:00Z")
        self.assertIsNone(before["knownTotals"]["total_tokens"])
        self.assertEqual(after["knownTotals"]["total_tokens"], 20)
        self.assertEqual(after["intervals"][0]["reason"], "cross_day_notification_date")

    def test_overlapping_recordings_do_not_duplicate_counter_intervals(self):
        source = self.source()
        source["windows"].append(dict(source["windows"][0], id="another", runDate=DATE))
        source["samples"] = [self.sample(3, 5)]
        report = reports.build_report([source], DATE, at(20))
        self.assertEqual(report["knownTotals"]["total_tokens"], 20)
        self.assertEqual(report["intervals"][0]["step"], "unassigned")


class IntegrationTests(UsageFixture):
    def test_cli_token_commands_never_start_n8n(self):
        with patch.object(ops, "ROOT", self.root), patch.object(ops, "Operations", side_effect=AssertionError("must not use n8n")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ops.main(["--date", "2026-09-03", "tokens", "begin", "summary"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "measuring")
        with patch.object(ops, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ops.main(["tokens", "report", "--work-date", DATE]), 0)

    def test_checkpoint_telemetry_failure_does_not_undo_checkpoint(self):
        self.bind()
        with patch.object(tokens.Tracker, "end", side_effect=Blocked("simulated_telemetry_failure")):
            result = tokens.checkpoint_finished(self.root, "summary", DATE)
        self.assertEqual(result["status"], "measurement_attention")

    def test_existing_status_path_has_no_measurement_side_effects(self):
        fake = Mock()
        fake.status.return_value = {"n8n": "unknown"}
        with patch.object(ops, "ROOT", self.root), patch.object(ops, "Operations", return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ops.main(["status"]), 0)
        self.assertFalse(self.tracker.directory.exists())


if __name__ == "__main__":
    unittest.main()
