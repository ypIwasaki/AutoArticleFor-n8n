"""Storage-contract regressions for shared record values."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import project_record_values as values


class RecordValueTests(unittest.TestCase):
    def test_existing_identifiers_and_hashes_remain_stable(self):
        # Fixed outputs from the pre-refactor implementation, including UTF-8 data.
        self.assertEqual(
            "9af037f9-5be6-50bc-821f-980f80197100",
            values.record_id("source", "content/記事.jsonl", "line:1", "hash"),
        )
        self.assertEqual(
            "d814a57327686b646048dc905b63b7eb982345ae1f32d8f7fddac969056a8f98",
            values.record_hash({"日本語": ["a", 1, None]}),
        )

    def test_timestamp_conventions(self):
        for timestamp in (
            "2026-09-17T09:00:00+09:00",
            "2026-09-17T00:00:00",
            "2026-09-17T00:00:00Z",
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual("2026-09-17T00:00:00Z", values.utc_timestamp(timestamp))
        self.assertEqual("1970-01-01T00:00:00Z", values.utc_timestamp(0))
        self.assertEqual("missing", values.utc_timestamp("", "missing"))

    def test_explicit_null_does_not_fall_back_to_another_alias(self):
        content = values.content_values({
            "contentText": None,
            "content_text": "must not replace the explicit null",
            "contentStatus": None,
            "status": "verified",
        })
        self.assertEqual("", content["text"])
        self.assertIsNone(content["status"])

    def test_body_length_aliases_must_agree_in_value_and_type(self):
        self.assertEqual(
            4, values.stored_body_length({"contentLength": 4, "body_length": 4})
        )
        for conflicting_length in (5, 4.0, True):
            with self.subTest(length=conflicting_length):
                with self.assertRaisesRegex(RuntimeError, "Conflicting saved body length"):
                    values.stored_body_length({
                        "contentLength": 4,
                        "body_length": conflicting_length,
                    })

    def test_missing_or_mismatched_body_remains_held(self):
        examples = (
            ({}, "no_body"),
            ({"contentText": "body"}, "consistent"),
            ({"contentText": "", "contentLength": 4}, "held_missing_body"),
            ({"contentText": "", "contentHash": "saved-hash"}, "held_missing_body"),
            ({"contentText": "body", "contentHash": "different"}, "held_hash_mismatch"),
        )
        for record, expected in examples:
            with self.subTest(record=record):
                self.assertEqual(expected, values.body_integrity(record))


if __name__ == "__main__":
    unittest.main()
