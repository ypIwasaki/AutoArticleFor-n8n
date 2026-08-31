#!/usr/bin/env python3
"""Run article capture, Markdown generation, and review validation by date."""

from __future__ import annotations

import argparse
import os
import json
import subprocess
import sys
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STRUCTURED_DIR = ROOT / "content" / "structured-records"
STATUS_PATH = ROOT / "content" / "article-review-backfill-status.json"
PROGRESS_PATH = ROOT / "content" / "article-review-backfill-progress.json"
CAPTURE_STATE_PATH = ROOT / "content" / "article-body-captures" / "backfill-state.json"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def available_dates(start_date: str, end_date: str) -> tuple[list[str], list[str]]:
    all_dates = sorted(path.stem for path in STRUCTURED_DIR.glob("????-??-??.jsonl"))
    selected = [
        date for date in all_dates
        if (not start_date or date >= start_date) and (not end_date or date <= end_date)
    ]
    return all_dates, selected


def run(command: list[str]) -> int:
    print("$ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT).returncode


def load_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def process_is_running(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def bootstrap_progress(path: Path, since: str) -> int:
    progress = load_status(path)
    try:
        capture_state = json.loads(CAPTURE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        capture_state = {}
    entries = capture_state.get("entries", {}) if isinstance(capture_state, dict) else {}
    completed = set(progress.get("completedUrls", []))
    before = len(completed)
    if isinstance(entries, dict) and since:
        for url, entry in entries.items():
            if (
                isinstance(entry, dict)
                and str(entry.get("processed_at") or "") >= since
            ):
                completed.add(str(url))
    progress.setdefault("schemaVersion", 1)
    progress.setdefault("dates", {})
    progress["completedUrls"] = sorted(completed)
    progress["bootstrappedAt"] = now()
    atomic_write(path, progress)
    return len(completed) - before

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--sync-contents", action="store_true")
    parser.add_argument("--status-file", type=Path, default=STATUS_PATH)
    parser.add_argument("--progress-file", type=Path, default=PROGRESS_PATH)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="完了済み日付と処理済みURLを飛ばして前回の続きから再開する",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="実行中のバックフィルへ割り込みシグナルを送り安全に停止する",
    )
    args = parser.parse_args()

    all_dates, selected_dates = available_dates(args.start_date, args.end_date)
    if args.stop:
        running = load_status(args.status_file)
        pid = running.get("runnerPid")
        pgid = running.get("runnerPgid")
        if not pid or not process_is_running(pid):
            print("実行中の再開対応バックフィルはありません。", flush=True)
            return 1
        try:
            os.killpg(int(pgid or pid), signal.SIGINT)
        except ProcessLookupError:
            print("バックフィルプロセスは既に終了しています。", flush=True)
            return 1
        print(f"停止シグナルを送信しました (PID {pid})。", flush=True)
        return 0
    if not all_dates:
        raise RuntimeError("対象のstructured-record日付がありません")
    previous = load_status(args.status_file) if args.resume else {}
    previous_pid = previous.get("runnerPid")
    if (
        args.resume
        and previous.get("state") == "running"
        and previous_pid
        and int(previous_pid) != os.getpid()
        and process_is_running(previous_pid)
    ):
        raise RuntimeError(f"バックフィルは既に実行中です (PID {previous_pid})")

    if args.resume:
        completed = [
            date for date in previous.get("completedDates", []) if date in all_dates
        ]
    else:
        completed = [
            date for date in all_dates if args.start_date and date < args.start_date
        ]
    dates = [date for date in selected_dates if date not in completed]
    status: dict[str, Any] = {
        "schemaVersion": 2,
        "state": "running",
        "startedAt": previous.get("startedAt") or now(),
        "resumedAt": now() if args.resume and previous else "",
        "resumeCount": int(previous.get("resumeCount") or 0) + (1 if args.resume else 0),
        "runnerPid": os.getpid(),
        "updatedAt": now(),
        "range": {"start": all_dates[0], "end": all_dates[-1]},
        "runnerPgid": os.getpgrp(),
        "targetDates": all_dates,
        "completedDates": completed,
        "failures": [],
        "failureHistory": [
            *previous.get("failureHistory", []),
            *previous.get("failures", []),
        ] if previous else [],
        "currentDate": "",
        "stage": "starting",
        "progressFile": str(args.progress_file),
    }
    atomic_write(args.status_file, status)

    if args.resume and previous.get("startedAt"):
        bootstrapped = bootstrap_progress(args.progress_file, str(previous["startedAt"]))
        print(
            f"チェックポイントへ既存実行分を{bootstrapped} URL追加しました。", flush=True
        )
    if not dates:
        status.update(
            state="complete",
            stage="finished",
            updatedAt=now(),
            finishedAt=now(),
            runnerPid=0,
        )
        atomic_write(args.status_file, status)
        print(f"=== ALREADY COMPLETE {len(completed)}/{len(all_dates)} ===", flush=True)
        return 0

    try:
        for date in dates:
            ordinal = all_dates.index(date) + 1
            print(f"=== START {date} ({ordinal}/{len(all_dates)}) ===", flush=True)
            status.update(currentDate=date, stage="capture", updatedAt=now())
            atomic_write(args.status_file, status)
            capture = [
                sys.executable,
                "scripts/capture_article_contents.py",
                "--run-date", date,
                "--retry-unverified",
                "--max-retries", str(max(1, args.max_retries)),
                "--progress-file", str(args.progress_file),
            ]
            if not args.sync_contents:
                capture.append("--no-sync-contents")
            exit_code = run(capture)
            if exit_code:
                status["failures"].append({"date": date, "stage": "capture", "exitCode": exit_code})
                status.update(updatedAt=now(), stage="capture_failed")
                atomic_write(args.status_file, status)
                print(f"=== CAPTURE FAILED {date} exit={exit_code} ===", flush=True)
                continue

            status.update(stage="markdown", updatedAt=now())
            atomic_write(args.status_file, status)
            exit_code = run([
                sys.executable,
                "scripts/generate_article_review_markdown.py",
                "--date", date,
                "--clean-generated",
            ])
            if exit_code:
                status["failures"].append({"date": date, "stage": "markdown", "exitCode": exit_code})
                status.update(updatedAt=now(), stage="markdown_failed")
                atomic_write(args.status_file, status)
                print(f"=== MARKDOWN FAILED {date} exit={exit_code} ===", flush=True)
                continue

            status.update(stage="review_validation", updatedAt=now())
            atomic_write(args.status_file, status)
            exit_code = run([
                sys.executable,
                "scripts/apply_article_markdown_reviews.py",
                "--date", date,
            ])
            if exit_code:
                status["failures"].append(
                    {"date": date, "stage": "review_validation", "exitCode": exit_code}
                )
                status.update(updatedAt=now(), stage="review_validation_failed")
                atomic_write(args.status_file, status)
                print(f"=== REVIEW FAILED {date} exit={exit_code} ===", flush=True)
                continue

            if date not in completed:
                completed.append(date)
                completed.sort()
            status.update(
                completedDates=completed,
                currentDate=date,
                stage="date_complete",
                updatedAt=now(),
            )
            atomic_write(args.status_file, status)
            print(f"=== COMPLETE {date} ({len(completed)}/{len(all_dates)}) ===", flush=True)

        status.update(
            state="complete" if not status["failures"] else "complete_with_failures",
            currentDate="",
            stage="finished",
            updatedAt=now(),
            finishedAt=now(),
            runnerPid=0,
        )
        atomic_write(args.status_file, status)
        print(
            f"=== ALL DONE completed={len(completed)} total={len(all_dates)} "
            f"failures={len(status['failures'])} ===",
            flush=True,
        )
        return 1 if status["failures"] else 0
    except BaseException as exc:
        status.update(
            state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            stage="interrupted",
            updatedAt=now(),
            runnerPid=0,
            error=str(exc),
        )
        atomic_write(args.status_file, status)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
