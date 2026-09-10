#!/usr/bin/env python3
"""Run a finite operation once, retaining logs and emitting bounded evidence.

This wrapper reports process completion, never semantic review completion.
No shell, retries, service startup, workflow selection or approval is inferred.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_JSON_BYTES = 4 * 1024 * 1024
WEEKLY_COUNTS = (
    "reportedArticles", "archivedRecords", "uniqueArticles", "duplicateRecords",
    "excludedArticles", "eligibleArticles", "classifiedArticles", "unclassifiedArticles",
    "invalidClassificationArticles", "unboundClassifications", "bodyReviewReadyArticles",
    "videoMetadataSummaries",
)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_result(path, result):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    os.replace(str(temporary), str(path))


def read_payload(path):
    """Accept a single JSON response or the last nonempty line, without huge reads."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - MAX_JSON_BYTES))
        raw = stream.read(MAX_JSON_BYTES)
    try:
        text = raw.decode("utf-8-sig")
        candidates = [text] if size <= MAX_JSON_BYTES else []
        candidates += [text.rstrip().rsplit("\n", 1)[-1]]
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if isinstance(value, dict):
                return value
    except UnicodeDecodeError:
        pass
    return None


def is_count(value):
    return type(value) is int and 0 <= value <= 10**15


def summarize_payload(payload, profile, run_date):
    """Only schema-known counts/flags are exposed; no body, tokens or error text."""
    summary = {"profile": profile, "resultStatus": "not_checked", "warningCount": None}
    if profile == "command":
        return summary
    if not isinstance(payload, dict):
        summary["resultStatus"] = "unrecognized"
        return summary
    if profile == "weekly":
        counts = payload.get("counts")
        valid = (
            payload.get("status") in ("ready", "provisional")
            and type(payload.get("checked")) is bool
            and isinstance(payload.get("coveredThrough"), str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["coveredThrough"])
            and (not run_date or payload["coveredThrough"] == run_date)
            and isinstance(counts, dict)
            and all(is_count(counts.get(key)) for key in WEEKLY_COUNTS if key != "videoMetadataSummaries")
            and (counts.get("videoMetadataSummaries") is None or is_count(counts["videoMetadataSummaries"]))
            and isinstance(payload.get("warnings"), list)
        )
        if not valid:
            summary["resultStatus"] = "unrecognized"
            return summary
        summary.update(
            resultStatus=payload["status"], checked=payload["checked"],
            coveredThrough=payload["coveredThrough"],
            counts={key: counts.get(key) for key in WEEKLY_COUNTS},
            warningCount=len(payload["warnings"]),
        )
    else:
        valid = (
            type(payload.get("saved")) is bool
            and isinstance(payload.get("date"), str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["date"])
            and (not run_date or payload["date"] == run_date)
            and is_count(payload.get("articleCount"))
            and is_count(payload.get("candidateCount"))
            and isinstance(payload.get("writtenFiles"), list)
        )
        if not valid:
            summary["resultStatus"] = "unrecognized"
            return summary
        summary.update(
            resultStatus="saved" if payload["saved"] else "not_saved",
            date=payload["date"],
            counts={"articles": payload["articleCount"], "candidates": payload["candidateCount"],
                    "writtenFiles": len(payload["writtenFiles"])},
        )
        # Counts only. Keywords/candidates and their evidence stay in the log/files.
        if isinstance(payload.get("autoAddedKeywords"), list):
            summary["counts"]["autoAddedKeywords"] = len(payload["autoAddedKeywords"])
        if is_count(payload.get("autoKeywordCount")):
            summary["counts"]["autoKeywords"] = payload["autoKeywordCount"]
    return summary


def stop_process(process):
    """Stop only the process/group created by this invocation."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # The parent may exit before its descendants.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait()


def run_operation(command, *, step, run_date=None, profile="command",
                  timeout=3600, log_dir=None):
    parent = Path(log_dir or ROOT / ".operation-logs")
    parent.mkdir(parents=True, exist_ok=True)
    # mkdtemp provides a private, collision-free directory. Never reuse old logs.
    directory = Path(tempfile.mkdtemp(prefix=step + "-", dir=str(parent))).resolve()
    stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
    result_path = directory / "result.json"
    result = {
        "resultVersion": 1, "step": step, "runDate": run_date,
        "startedAt": timestamp(), "processStatus": "running", "exitCode": None,
        "logs": {"stdout": str(stdout_path), "stderr": str(stderr_path), "result": str(result_path)},
    }
    save_result(result_path, result)
    began = time.monotonic()
    code, status, error_kind = 1, "launch_failed", None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    command, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                    shell=False, start_new_session=(os.name == "posix"),
                )
            except OSError as exc:
                code, error_kind = 127, type(exc).__name__
            else:
                try:
                    code = process.wait(timeout=timeout)
                    status = "exited_ok" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    stop_process(process)
                    code, status = 124, "timed_out"
                except KeyboardInterrupt:
                    stop_process(process)
                    code, status = 130, "interrupted"
    finally:
        result.update(
            finishedAt=timestamp(), processStatus=status, exitCode=code,
            elapsedSeconds=round(time.monotonic() - began, 2),
            stdoutBytes=stdout_path.stat().st_size if stdout_path.exists() else 0,
            stderrBytes=stderr_path.stat().st_size if stderr_path.exists() else 0,
        )
        if error_kind:
            result["errorKind"] = error_kind
        detail = summarize_payload(read_payload(stdout_path) if stdout_path.exists() else None, profile, run_date)
        result["summary"] = detail
        result["attentionRequired"] = (
            code != 0 or result["stderrBytes"] > 0
            or detail["resultStatus"] in ("unrecognized", "provisional", "not_saved")
            or bool(detail["warningCount"])
        )
        # All commands still need task-appropriate verification, including exit 0.
        result["verification"] = "command_result_only"
        save_result(result_path, result)
    return result, code if code >= 0 else 128 - code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", required=True, help="Short stable operation label, e.g. weekly-check")
    parser.add_argument("--run-date", help="Expected collection date, not today's date")
    parser.add_argument("--profile", choices=("command", "collection", "weekly"), default="command")
    parser.add_argument("--timeout", type=float, default=3600, help="Finite command deadline in seconds")
    parser.add_argument("--log-dir", type=Path, help="Default: project .operation-logs (Git ignored)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- executable arguments (no implicit shell)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.step):
        parser.error("--step must be 1-40 lowercase ASCII letters, digits or hyphens")
    if args.run_date:
        try:
            if date.fromisoformat(args.run_date).isoformat() != args.run_date:
                raise ValueError()
        except ValueError:
            parser.error("--run-date must be YYYY-MM-DD")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Specify -- executable arguments")
    result, code = run_operation(command, step=args.step, run_date=args.run_date,
                                 profile=args.profile, timeout=args.timeout, log_dir=args.log_dir)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        # Do not echo command arguments, environment values or raw API errors.
        print(json.dumps({"processStatus": "wrapper_failed", "errorKind": type(exc).__name__,
                          "verification": "not_completed"}))
        raise SystemExit(1)
