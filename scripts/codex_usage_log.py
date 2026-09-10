"""Read only numeric usage metadata from one explicitly identified Codex rollout.

This is a version-dependent local-log adapter, not a billing API. Never persist
or return messages, tool arguments, reasoning text, credentials or rate limits.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from autoarticle_progress import Blocked, timestamp

CORE = ("input_tokens", "output_tokens", "total_tokens")
DETAIL = ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")
FIELDS = CORE + DETAIL
TAIL_BYTES = 8 * 1024 * 1024
BATCH_BYTES = 32 * 1024 * 1024
LINE_BYTES = 16 * 1024 * 1024
EVENT = re.compile(rb'"type"\s*:\s*"event_msg"')
TOKEN = re.compile(rb'"type"\s*:\s*"token_count"')


def counters(value):
    if not isinstance(value, dict):
        raise Blocked("token_usage_schema_unknown")
    result = {}
    for field in FIELDS:
        number = value.get(field)
        if number is None and field in DETAIL:
            result[field] = None
        elif type(number) is not int or number < 0:
            raise Blocked("token_usage_counter_invalid")
        else:
            result[field] = number
    if result["input_tokens"] + result["output_tokens"] != result["total_tokens"]:
        raise Blocked("token_usage_total_mismatch")
    for detail, parent in (("cached_input_tokens", "input_tokens"), ("cache_write_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if result[detail] is not None and result[detail] > result[parent]:
            raise Blocked("token_usage_detail_invalid")
    return result


def decode_usage(line, offset):
    if not EVENT.search(line) or not TOKEN.search(line):
        return None
    try:
        event = json.loads(line)
        if event.get("type") != "event_msg" or event.get("payload", {}).get("type") != "token_count":
            return None
        event_time = timestamp(event["timestamp"]).isoformat()
        info = event["payload"].get("info")
        # Rate-limit-only notifications carry info:null and are not usage samples.
        if info is None:
            return None
        values = counters(info.get("total_token_usage"))
        return {"offset": offset, "at": event_time, "total": values}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Blocked("token_usage_event_invalid") from None


def identity(path, thread_id):
    with Path(path).open("rb") as stream:
        line = stream.readline(1024 * 1024)
    try:
        meta = json.loads(line)
        if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != thread_id:
            raise Blocked("token_log_thread_mismatch")
    except (ValueError, AttributeError):
        raise Blocked("token_log_header_invalid") from None


def anchor(stream, cursor):
    stream.seek(max(0, cursor - 256))
    return hashlib.sha256(stream.read(min(cursor, 256))).hexdigest()


def initial_cursor(path, thread_id):
    identity(path, thread_id)
    with Path(path).open("rb") as stream:
        size = stream.seek(0, 2)
        start = max(0, size - TAIL_BYTES)
        stream.seek(start)
        data = stream.read(size - start)
        if start:
            split = data.find(b"\n")
            if split < 0:
                raise Blocked("token_log_tail_has_no_complete_line")
            start += split + 1
            data = data[split + 1:]
        last = None
        cursor = start
        issues = []
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                sample = decode_usage(line, cursor)
                if sample:
                    last = sample
            except Blocked as error:
                issues.append(str(error))
                last = None
            cursor += len(line)
        return {"offset": cursor, "anchor": anchor(stream, cursor), "last": last}, issues


def advance(path, thread_id, cursor):
    """Append-only bounded scan. A partial last line is retried on the next call."""
    identity(path, thread_id)
    samples, issues = [], []
    with Path(path).open("rb") as stream:
        size = stream.seek(0, 2)
        position = cursor["offset"]
        if size < position or anchor(stream, position) != cursor["anchor"]:
            raise Blocked("token_log_replaced_or_truncated")
        if cursor.get("last"):
            # The trailing bytes alone need not include the previous usage line.
            # Validate that baseline too before subtracting it from a new sample.
            baseline = cursor["last"]
            stream.seek(baseline["offset"])
            try:
                actual = decode_usage(stream.readline(LINE_BYTES + 1), baseline["offset"])
            except Blocked:
                actual = None
            if actual != baseline:
                raise Blocked("token_log_replaced_or_truncated")
        stream.seek(position)
        budget_end = min(size, position + BATCH_BYTES)
        last = cursor.get("last")
        gap = cursor.get("gap", False)
        while stream.tell() < budget_end:
            offset = stream.tell()
            line = stream.readline(min(LINE_BYTES + 1, size - offset))
            if len(line) > LINE_BYTES:
                raise Blocked("token_log_line_too_large")
            if not line.endswith(b"\n"):
                stream.seek(offset)
                break
            try:
                sample = decode_usage(line, offset)
            except Blocked as error:
                issues.append({"offset": offset, "reason": str(error)})
                # Retain the last valid cumulative counter. A later valid sample
                # may recover an interval total, but not its per-stage split.
                gap = True
                continue
            if sample is None:
                continue
            if last is None:
                issues.append({"offset": offset, "reason": "usage_baseline_missing"})
            else:
                before, after = last["total"], sample["total"]
                if any(after[k] < before[k] for k in CORE):
                    issues.append({"offset": offset, "reason": "usage_counter_reset"})
                elif timestamp(sample["at"]) < timestamp(last["at"]):
                    issues.append({"offset": offset, "reason": "usage_clock_reversed"})
                elif all(after[k] == before[k] for k in CORE):
                    # Duplicate/rate-limit refresh: do not sum last_token_usage again.
                    continue
                else:
                    delta = {k: after[k] - before[k] for k in CORE}
                    for key in DETAIL:
                        delta[key] = after[key] - before[key] if after[key] is not None and before[key] is not None and after[key] >= before[key] else None
                    for detail, parent in (("cached_input_tokens", "input_tokens"), ("cache_write_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
                        if delta[detail] is not None and delta[detail] > delta[parent]:
                            delta[detail] = None
                            issues.append({"offset": offset, "reason": "usage_detail_delta_invalid"})
                    samples.append({"offset": offset, "from": last["at"], "at": sample["at"], "tokens": delta,
                                    "before": before, "after": after,
                                    "calculation": "after_minus_before", "gap": gap})
            last = sample
            gap = False
        position = stream.tell()
        return {"offset": position, "anchor": anchor(stream, position), "last": last, "gap": gap}, samples, issues, position < size
