#!/usr/bin/env python3
"""Record scoped Codex usage and produce daily, metadata-only Markdown reports."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

import codex_usage_log as usage
from autoarticle_progress import ALL_STEPS, Blocked, Progress, now, read_json, timestamp, today, write_json

ROOT = Path(__file__).resolve().parents[1]
STEPS = ALL_STEPS + ("common", "prepare", "capture", "shared-review", "apply", "report", "preflight", "investigation")


def date_value(value):
    if dt.date.fromisoformat(value).isoformat() != value:
        raise Blocked("invalid_date")
    return value


class Tracker:
    def __init__(self, root=ROOT, clock=now):
        self.root = Path(root).resolve()
        self.clock = clock
        self.directory = self.root / ".operation-state/token-usage"
        self.binding = self.directory / "binding.json"

    def source_file(self, key):
        if not re.fullmatch(r"source-[0-9a-f]{16}", key):
            raise Blocked("invalid_usage_source_key")
        return self.directory / (key + ".json")

    def save(self, source):
        write_json(self.source_file(source["key"]), source)

    def load(self, path):
        source = read_json(path)
        if not isinstance(source, dict) or source.get("schemaVersion") != 1 or not all(isinstance(source.get(k), list) for k in ("windows", "spans", "samples", "issues")):
            raise Blocked("invalid_token_usage_state")
        self.source_file(source["key"])
        return source

    def new_source(self, thread_id, log_path, reason=None):
        key = "source-" + hashlib.sha256(thread_id.encode()).hexdigest()[:16]
        source = {"schemaVersion": 1, "key": key, "threadId": thread_id, "logPath": str(log_path) if log_path else None,
                  "cursor": None, "windows": [], "spans": [], "samples": [], "issues": [],
                  "sourceStatus": reason or "not_read", "lastReadAt": None}
        return source

    def bind(self, thread_id=None, log_path=None, codex_home=None):
        thread_id = thread_id or os.environ.get("CODEX_THREAD_ID") or os.environ.get("AUTOARTICLE_CODEX_THREAD_ID")
        if not thread_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            raise Blocked("current_thread_id_required")
        caller = os.environ.get("CODEX_THREAD_ID")
        if caller and caller != thread_id:
            raise Blocked("token_binding_does_not_match_current_thread")
        log_path = log_path or os.environ.get("AUTOARTICLE_CODEX_SESSION_LOG")
        if not log_path:
            home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
            matches = list((home / "sessions").glob("**/*" + thread_id + ".jsonl"))
            if len(matches) != 1:
                raise Blocked("token_log_missing_or_ambiguous_use_explicit_path")
            log_path = matches[0]
        path = Path(log_path).expanduser().resolve()
        usage.identity(path, thread_id)
        source = self.new_source(thread_id, path)
        state_file = self.source_file(source["key"])
        if state_file.exists():
            source = self.load(state_file)
            # A caller may supply the same log via a different host mount path.
            source["logPath"] = str(path)
        if source["cursor"] is None:
            source["cursor"], issues = usage.initial_cursor(path, thread_id)
            source["issues"].extend({"reason": reason, "at": self.clock()} for reason in issues)
        self.refresh(source)
        self.save(source)
        write_json(self.binding, {"schemaVersion": 1, "sourceKey": source["key"]})
        return {"status": "bound", "source": source["key"], "measurementStatus": source["sourceStatus"]}

    def selected(self, create=False):
        if not self.binding.exists():
            if not create:
                return None
            try:
                self.bind()
            except (Blocked, OSError):
                # Keep missing measurements visible without blocking article operations.
                thread_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("AUTOARTICLE_CODEX_THREAD_ID") or "unbound"
                source = self.new_source(thread_id, None, "log_not_bound")
                path = self.source_file(source["key"])
                if path.exists():
                    source = self.load(path)
                self.save(source)
                write_json(self.binding, {"schemaVersion": 1, "sourceKey": source["key"]})
        binding = read_json(self.binding)
        if binding.get("schemaVersion") != 1:
            raise Blocked("invalid_token_binding")
        source = self.load(self.source_file(binding["sourceKey"]))
        caller = os.environ.get("CODEX_THREAD_ID") or os.environ.get("AUTOARTICLE_CODEX_THREAD_ID")
        if caller and caller != source["threadId"]:
            raise Blocked("token_binding_does_not_match_current_thread")
        return source

    def refresh(self, source):
        at = self.clock()
        if not source["logPath"] or source["cursor"] is None:
            source["sourceStatus"] = "log_not_bound"
            return
        try:
            cursor, samples, issues, pending = usage.advance(source["logPath"], source["threadId"], source["cursor"])
            source["cursor"] = cursor
            source["samples"].extend(samples)
            source["issues"].extend(dict(issue, at=at) for issue in issues)
            source["sourceStatus"] = "backlog_or_partial_line" if pending else "readable"
            source["lastReadAt"] = at
        except (Blocked, OSError) as error:
            reason = str(error) if isinstance(error, Blocked) else "token_log_unavailable"
            source["sourceStatus"] = reason
            if not source["issues"] or source["issues"][-1]["reason"] != reason:
                source["issues"].append({"reason": reason, "at": at})

    @staticmethod
    def active_span(source):
        return next((s for s in reversed(source["spans"]) if s["end"] is None), None)

    @staticmethod
    def active_window(source):
        return next((w for w in reversed(source["windows"]) if w["end"] is None), None)

    def begin(self, step, run_date):
        if step not in STEPS:
            raise Blocked("unknown_token_step")
        date_value(run_date)
        source = self.selected(create=True)
        self.refresh(source)
        at = self.clock()
        active = self.active_span(source)
        if active and active["step"] == step and active["runDate"] == run_date:
            self.save(source)
            return {"status": "already_measuring", "step": step, "spanId": active["id"], "measurementStatus": source["sourceStatus"]}
        if active:
            active.update(end=at, outcome="switched")
        window = self.active_window(source)
        if window and window["runDate"] != run_date:
            window["end"] = at
            window = None
        if window is None:
            window = {"id": uuid.uuid4().hex, "start": at, "end": None, "runDate": run_date}
            source["windows"].append(window)
        span = {"id": uuid.uuid4().hex, "windowId": window["id"], "step": step, "runDate": run_date, "start": at, "end": None, "outcome": "measuring"}
        source["spans"].append(span)
        self.save(source)
        return {"status": "measuring", "step": step, "spanId": span["id"], "measurementStatus": source["sourceStatus"]}

    def end(self, step=None, run_date=None, outcome="ended", checkpoint=False):
        source = self.selected()
        if source is None:
            return {"status": "not_measured", "reason": "begin_not_recorded"}
        self.refresh(source)
        active = self.active_span(source)
        if active is None:
            self.save(source)
            return {"status": "not_measuring"}
        if (step and active["step"] != step) or (run_date and active["runDate"] != run_date):
            if checkpoint:
                self.save(source)
                return {"status": "not_measured", "reason": "checkpoint_span_mismatch"}
            raise Blocked("token_step_or_target_date_mismatch")
        active.update(end=self.clock(), outcome=outcome)
        self.save(source)
        return {"status": "ended", "step": active["step"], "spanId": active["id"], "measurementStatus": source["sourceStatus"]}

    def finish(self):
        source = self.selected()
        if source is None:
            return {"status": "not_measured"}
        self.refresh(source)
        at = self.clock()
        active = self.active_span(source)
        if active:
            active.update(end=at, outcome="finished")
        window = self.active_window(source)
        if window:
            window["end"] = at
        self.save(source)
        return {"status": "measurement_closed", "measurementStatus": source["sourceStatus"]}

    def reports(self, work_date=None):
        from token_usage_report import build_report, save_report, report_dates
        if work_date:
            date_value(work_date)
        source = self.selected()
        if source:
            self.refresh(source)
            self.save(source)
        sources = [self.load(p) for p in sorted(self.directory.glob("source-*.json"))]
        dates = [work_date] if work_date else report_dates(sources, self.clock())
        paths = []
        for day in dates:
            report = build_report(sources, day, self.clock())
            paths.append(str(save_report(self.root, report)))
        return {"reports": paths, "measurementStatus": source["sourceStatus"] if source else "not_bound", "reportCount": len(paths)}


def add_arguments(parser):
    subs = parser.add_subparsers(dest="token_command", required=True)
    bind = subs.add_parser("bind", help="Select this task's log once; never guess the most recent task")
    bind.add_argument("--thread-id")
    bind.add_argument("--session-log")
    bind.add_argument("--codex-home")
    begin = subs.add_parser("begin", help="Start a step; switch closes the previous step")
    begin.add_argument("step", choices=STEPS)
    end = subs.add_parser("end", help="End a step without claiming operational completion")
    end.add_argument("step", choices=STEPS, nargs="?")
    subs.add_parser("finish", help="Close this measurement window (final reply afterwards is outside scope)")
    report = subs.add_parser("report", help="Refresh delayed samples and rebuild daily Markdown/JSON")
    report.add_argument("--work-date")


def execute(args, root=ROOT, run_date=None):
    run_date = date_value(run_date or today())
    tracker = Tracker(root)
    with Progress(root, run_date, "token-usage").lock():
        command = args.token_command
        if command == "bind":
            result = tracker.bind(args.thread_id, args.session_log, args.codex_home)
        elif command == "begin":
            result = tracker.begin(args.step, run_date)
        elif command == "end":
            result = tracker.end(args.step, run_date)
        elif command == "finish":
            result = tracker.finish()
        else:
            return tracker.reports(args.work_date)
        result.update(tracker.reports())
        return result


def checkpoint_finished(root, step, run_date):
    """Best-effort telemetry AFTER the real checkpoint has committed its evidence."""
    tracker = Tracker(root)
    if not tracker.binding.exists():
        return {"status": "not_measured", "reason": "begin_not_recorded"}
    try:
        with Progress(root, run_date, "token-usage").lock():
            result = tracker.end(step, run_date, outcome="checkpoint_saved", checkpoint=True)
            result.update(tracker.reports())
            return result
    except (Blocked, OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "measurement_attention", "reason": str(error) if isinstance(error, Blocked) else type(error).__name__}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Article target date, not the Markdown work date")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result = execute(args, run_date=args.date)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (Blocked, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error) if isinstance(error, Blocked) else type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
