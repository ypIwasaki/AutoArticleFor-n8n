"""Read-only weekly report listing and detail views for an explicit project."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from article_artifact_formats import (
    WEEKLY_REPORT_FILENAME_PATTERN,
    WEEKLY_REPORT_FRONT_MATTER_PATTERN,
    weekly_report_metadata,
)


def weekly_report_summary(markdown: str) -> str:
    body = WEEKLY_REPORT_FRONT_MATTER_PATTERN.sub("", markdown, count=1)
    body = re.sub(r"^#.*$", "", body, count=1, flags=re.MULTILINE)
    for line in body.splitlines():
        text = line.strip().lstrip("- ").strip()
        if text and not text.startswith("#") and not text.startswith("|"):
            return re.sub(r"\s+", " ", text)[:180]
    return ""


def load_weekly_report(project_root: Path, week_start: str) -> dict[str, Any]:
    filename = f"{week_start}.md"
    if not WEEKLY_REPORT_FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("Invalid weekly report identifier")
    path = project_root / "content" / "weekly-reports" / filename
    if not path.exists():
        raise FileNotFoundError("Weekly report was not found")
    markdown = path.read_text(encoding="utf-8")
    return {**weekly_report_metadata(markdown, path), "markdown": markdown}


def weekly_reports_payload(project_root: Path) -> dict[str, Any]:
    reports: list[dict[str, str]] = []
    report_directory = project_root / "content" / "weekly-reports"
    for path in sorted(report_directory.glob("????-??-??.md"), reverse=True):
        if not WEEKLY_REPORT_FILENAME_PATTERN.fullmatch(path.name):
            continue
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue
        reports.append({
            **weekly_report_metadata(markdown, path),
            "summary": weekly_report_summary(markdown),
        })
    return {"reports": reports}
