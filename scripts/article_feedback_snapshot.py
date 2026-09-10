"""Complete machine-readable feedback, separate from example-only guidance."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REASONS = {"suspicious_source", "irrelevant", "unavailable", "outdated"}


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".weekly-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_snapshot(payload: dict, generated_at, instruction: str) -> dict:
    articles = {str(row.get("article_key") or ""): row for row in payload.get("articles", [])}
    issues, rows = [], []
    complete = payload.get("_article_feedback_available", "article_feedback" in payload) is True
    for index, source in enumerate(payload.get("article_feedback", [])):
        if not isinstance(source, dict):
            issues.append(f"row {index}: not an object")
            continue
        article = articles.get(str(source.get("article_key") or ""), {})
        url = str(article.get("url") or source.get("article_url") or "").strip()
        flag = str(source.get("is_rejected", "")).strip().casefold()
        reason = str(source.get("reason_code") or "").strip()
        if not urlparse(url).hostname or urlparse(url).scheme not in ("http", "https") or flag not in ("0", "1", "true", "false", "yes", "no"):
            issues.append(f"row {index}: unresolved URL or invalid decision")
            continue
        rejected = flag in ("1", "true", "yes")
        if rejected and reason not in REASONS:
            issues.append(f"row {index}: unknown rejection reason")
            continue
        try:
            reviewed = datetime.fromisoformat(str(source.get("reviewed_at") or "").replace("Z", "+00:00"))
            if reviewed.tzinfo is None:
                reviewed = reviewed.replace(tzinfo=timezone.utc)
            if reviewed > generated_at:
                raise ValueError("future review")
        except (ValueError, TypeError):
            issues.append(f"row {index}: missing, invalid or future review timestamp")
            continue
        if rejected and reason == "suspicious_source" and not (source.get("source_domain") or source.get("publisher_label")):
            issues.append(f"row {index}: suspicious source has no domain or publisher")
            continue
        rows.append({"articleUrl": url, "decision": "rejected" if rejected else "approved",
                     "reasonCode": reason if rejected else "approved",
                     "sourceDomain": str(source.get("source_domain") or "").strip(),
                     "publisherLabel": str(source.get("publisher_label") or "").strip(),
                     "reviewedAt": str(source.get("reviewed_at") or "")})
    return {"schemaVersion": 1, "snapshotDate": generated_at.date().isoformat(),
            "generatedAt": generated_at.isoformat(), "complete": complete and not issues,
            "source": "n8n-data-tables", "feedback": rows, "issues": issues,
            "instructionHash": hashlib.sha256(instruction.encode("utf-8")).hexdigest()}
