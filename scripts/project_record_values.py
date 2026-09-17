"""Shared record values for business writes and legacy import.

Identifiers, hashes, aliases and timestamp conventions are storage contracts.
Keep their representation stable so existing references remain valid.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
import uuid

import project_database as db


RECORD_NAMESPACE = uuid.UUID("520e98ee-e2d9-4c73-a2d5-535e16f6ce61")


def record_id(*parts: Any) -> str:
    """Return the existing deterministic identifier for these ordered parts."""
    return str(uuid.uuid5(RECORD_NAMESPACE, db.canonical(parts)))


def record_hash(value: Any) -> str:
    return db.checksum(db.canonical(value).encode("utf-8"))


def utc_timestamp(value: Any, default: str | None = None) -> str | None:
    """Format UTC, treating saved timestamps without a timezone as UTC."""
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        timestamp = datetime.fromtimestamp(value, timezone.utc)
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def json_array(value: Any) -> list[Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if decoded is None:
        return []
    if not isinstance(decoded, list):
        raise ValueError("Expected saved JSON array")
    return decoded


def stored_body_length(record: dict[str, Any]) -> Any:
    """Preserve the saved type and reject conflicting length aliases."""
    aliases = ("contentLength", "content_length", "body_length")
    saved_lengths = {name: record[name] for name in aliases if name in record}
    if not saved_lengths:
        return None
    first_length = next(iter(saved_lengths.values()))
    if any(
        type(length) != type(first_length) or length != first_length
        for length in saved_lengths.values()
    ):
        raise RuntimeError(
            "Conflicting saved body length aliases: " + ",".join(saved_lengths)
        )
    return first_length


def content_values(record: dict[str, Any]) -> dict[str, Any]:
    """Read saved capture formats without changing alias precedence or nulls."""
    def field(camel_case: str, snake_case: str, default: Any = "") -> Any:
        return record.get(camel_case, record.get(snake_case, default))

    return {
        "key": field("articleKey", "article_key"),
        "url": field("originalUrl", "original_url"),
        "text": field("contentText", "content_text") or "",
        "markdown": field("contentMarkdown", "content_markdown") or "",
        "metadata": field("pageMetadata", "page_metadata", {}) or {},
        "non_content": field("nonContentText", "non_content_text") or "",
        "scope": field("extractionScope", "extraction_scope") or "",
        "status": field("contentStatus", "content_status", record.get("status")),
        "stored_hash": field("contentHash", "content_hash"),
        "fetched": field("fetchedAt", "fetched_at", record.get("processed_at")),
        "resolved": field("resolvedUrl", "resolved_url"),
        "method": field("extractionMethod", "extraction_method"),
        "reason": field("failureReason", "failure_reason", record.get("reason")),
        "completeness": field("contentCompleteness", "content_completeness"),
        "retry": record.get("retry_after"),
        "stored_length": stored_body_length(record),
        "content_path": record.get("content_path"),
    }


def body_integrity(record: dict[str, Any]) -> str:
    """Keep missing or mismatched saved bodies on hold."""
    content = content_values(record)
    computed_hash = db.checksum(content["text"].encode("utf-8"))
    hash_mismatch = bool(
        content["stored_hash"] and content["stored_hash"] != computed_hash
    )
    if not content["text"] and (hash_mismatch or (content["stored_length"] or 0) > 0):
        return "held_missing_body"
    if hash_mismatch:
        return "held_hash_mismatch"
    return "consistent" if content["text"] else "no_body"
