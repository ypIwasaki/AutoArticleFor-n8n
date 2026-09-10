"""Read-only verification for proposal application to this local n8n database."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path

from autoarticle_progress import Blocked, read_json


def proposal(progress, kind):
    step = "talent-review" if kind == "talent" else "classification-review"
    value = read_json(progress.path(progress.outputs(step)[0]))
    fields = ("articles", "talents", "articleTalents") if kind == "talent" else ("classifications",)
    if value.get("proposalVersion") != 1 or value.get("proposalDate") != progress.date or any(not isinstance(value.get(f), list) for f in fields):
        raise Blocked("invalid_proposal_contract_or_date")
    if any(not isinstance(row, dict) for f in fields for row in value[f]):
        raise Blocked("invalid_proposal_rows")
    return value


def tables(base, remote_workflow):
    if urllib.parse.urlsplit(base).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise Blocked("db_verification_requires_local_n8n")
    path = Path(os.environ.get("N8N_DATABASE_PATH") or str(Path(os.environ.get("N8N_USER_FOLDER", "~/.n8n")).expanduser() / "database.sqlite")).expanduser().resolve()
    if not path.is_file():
        raise Blocked("n8n_database_missing")
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        identity = db.execute('SELECT name FROM workflow_entity WHERE id = ?', (str(remote_workflow["id"]),)).fetchall()
        if len(identity) != 1 or identity[0]["name"] != remote_workflow["name"]:
            raise Blocked("local_db_workflow_identity_mismatch")
        names = ("articles", "talents", "article_talents", "article_classifications")
        rows = db.execute("SELECT id, name FROM data_table WHERE name IN (?,?,?,?)", names).fetchall()
        identifiers, result = {}, {}
        for row in rows:
            if row["name"] in result or not re.fullmatch(r"[A-Za-z0-9_]+", row["id"]):
                raise Blocked("ambiguous_or_invalid_data_table")
            identifiers[row["name"]] = row["id"]
            result[row["name"]] = [dict(r) for r in db.execute('SELECT * FROM "data_table_user_%s"' % row["id"])]
        targets = {"Upsert Proposed Articles": "articles", "Upsert Proposed Talents": "talents", "Upsert Proposed Relations": "article_talents", "Upsert Article Classifications": "article_classifications"}
        for node in remote_workflow["nodes"]:
            if node["name"] not in targets:
                continue
            target = node["parameters"]["dataTableId"]
            expected = identifiers.get(targets[node["name"]])
            actual = target.get("value")
            if actual == "={{ $env.N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID }}":
                actual = os.environ.get("N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID")
            if not expected or (target.get("mode") == "id" and actual != expected) or (target.get("mode") == "name" and actual != targets[node["name"]]):
                raise Blocked("workflow_data_table_target_mismatch")
        return result


def expected(kind, value, current):
    def text(row, field, default=""):
        return str(row.get(field) if row.get(field) is not None else default).strip()
    if kind == "talent":
        groups = []
        specs = (("articles", "article_key", "articles", ("article_key", "url", "title", "excerpt", "source", "published_at", "last_seen_at")),
                 ("talents", "talent_id", "talents", ("talent_id", "display_name", "organization", "aliases_json", "status", "search_enabled", "auto_discovered", "last_seen_at")),
                 ("article_talents", "relation_key", "articleTalents", ("relation_key", "article_key", "talent_id", "matched_aliases_json", "matched_fields", "evidence_text", "confidence", "detection_method", "last_seen_at")))
        for table, key, source, fields in specs:
            rows = []
            for raw in value[source]:
                # Explicit stored values keep the apply command deterministic: no server 'now' defaults.
                if any(f not in raw for f in fields):
                    raise Blocked("proposal_stored_fields_must_be_explicit")
                row = {f: raw[f] if f.endswith("_json") or isinstance(raw[f], (bool, int, float)) else text(raw, f) for f in fields}
                if table == "articles":
                    row["excerpt"] = row["excerpt"][:2000]
                    row["published_at"] = row["published_at"] or None
                if not row[key] or not row["last_seen_at"]:
                    raise Blocked("proposal_key_or_timestamp_missing")
                rows.append(row)
            if len({r[key] for r in rows}) != len(rows):
                raise Blocked("duplicate_proposal_keys")
            groups.append((table, key, rows))
        return groups
    by_url = {r["url"]: r["article_key"] for r in current.get("articles", [])}
    keys = set(by_url.values())
    rows = []
    for raw in value["classifications"]:
        key = raw.get("article_key") or by_url.get(raw.get("article_url"))
        if not key or key not in keys or (raw.get("article_url") and by_url.get(raw["article_url"]) != key):
            raise Blocked("classification_article_not_registered")
        fields = ("article_type", "primary_category", "secondary_categories_json", "relevance", "confidence", "evidence_text", "classification_method", "classified_at")
        if any(f not in raw for f in fields) or not raw["classified_at"]:
            raise Blocked("proposal_stored_fields_must_be_explicit")
        row = {f: raw[f] for f in fields}
        row.update(article_key=key, evidence_text=str(raw["evidence_text"]).strip()[:2000])
        rows.append(row)
    if not rows or len({r["article_key"] for r in rows}) != len(rows):
        raise Blocked("classifications_empty_or_duplicate")
    return [("article_classifications", "article_key", rows)]


def equal(field, actual, wanted):
    if field in ("published_at", "last_seen_at", "classified_at") and actual != wanted:
        # n8n Date values are persisted in SQLite as UTC, at millisecond precision.
        # Restrict this normalization to timestamp columns; text stays exact.
        def instant(value, stored=False):
            if not isinstance(value, str):
                raise ValueError("not a timestamp")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                if not stored or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", value):
                    raise ValueError("ambiguous timezone")
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=parsed.microsecond // 1000 * 1000)
        try:
            return instant(actual, stored=True) == instant(wanted)
        except (ValueError, TypeError, OverflowError):
            return False
    if field.endswith("_json"):
        try:
            actual = json.loads(actual) if isinstance(actual, str) else actual
            wanted = json.loads(wanted) if isinstance(wanted, str) else wanted
        except ValueError:
            return False
    return actual == wanted


def preflight(kind, value, current):
    groups = expected(kind, value, current)
    if kind == "talent":
        registry = {r["talent_id"]: r for r in current.get("talents", [])}
        for row in value["talents"]:
            old = registry.get(row.get("talent_id"), {})
            if row.get("status", "pending") != old.get("status", "pending") or row.get("search_enabled", False) != old.get("search_enabled", False):
                raise Blocked("talent_approval_or_search_change_requires_separate_review")
    return groups


def verify(kind, value, current):
    for table, key, proposed in expected(kind, value, current):
        for row in proposed:
            matches = [r for r in current.get(table, []) if r.get(key) == row[key]]
            if len(matches) != 1:
                raise Blocked("db_rows_missing_or_ambiguous")
            if any(not equal(field, matches[0].get(field), wanted) for field, wanted in row.items()):
                raise Blocked("db_content_mismatch")
