"""Read dashboard records independently of the HTTP server.

The explicit project root selects project DB data or the configured legacy route.
Project DB errors propagate; callers may use proposal fallback only in legacy mode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from article_artifact_formats import parsed_summaries
import project_readers as project


TABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def normalise_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, bool):
            result[key] = value
        elif key in {"search_enabled", "auto_discovered"}:
            result[key] = bool(value)
    return result



def database_path() -> Path:
    explicit = os.environ.get("N8N_DATABASE_PATH")
    if explicit:
        return Path(explicit).expanduser()

    user_folder = Path(os.environ.get("N8N_USER_FOLDER", "~/.n8n")).expanduser()
    return user_folder / "database.sqlite"



def quoted_table_name(table_id: str) -> str:
    if not TABLE_ID_PATTERN.fullmatch(table_id):
        raise ValueError("Invalid n8n data table identifier")
    return f'"data_table_user_{table_id}"'



def load_dashboard_records(project_root: Path) -> tuple[dict[str, Any], str]:
    if project.source(project_root, "dashboard") == "project-db":
        with project.reader(project_root) as reader:
            return reader.dashboard(), "project-db"
    path = database_path()
    if not path.exists():
        raise FileNotFoundError(f"n8n database was not found: {path}")

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, name FROM data_table "
            "WHERE name IN ('talents', 'articles', 'article_talents', 'article_classifications', 'article_feedback')"
        ).fetchall()
        identifiers = {row["name"]: row["id"] for row in rows}
        missing = {"talents", "articles", "article_talents"}.difference(identifiers)
        if missing:
            raise RuntimeError(f"Missing n8n Data Tables: {', '.join(sorted(missing))}")

        payload: dict[str, Any] = {}
        for name in ("talents", "articles", "article_talents"):
            table = quoted_table_name(identifiers[name])
            payload[name] = [normalise_row(row) for row in connection.execute(f"SELECT * FROM {table}")]
        if "article_classifications" in identifiers:
            table = quoted_table_name(identifiers["article_classifications"])
            payload["article_classifications"] = [
                normalise_row(row) for row in connection.execute(f"SELECT * FROM {table}")
            ]
        else:
            payload["article_classifications"] = []
        if "article_feedback" in identifiers:
            table = quoted_table_name(identifiers["article_feedback"])
            payload["article_feedback"] = [
                normalise_row(row) for row in connection.execute(f"SELECT * FROM {table}")
            ]
        else:
            payload["article_feedback"] = []
        payload["_article_feedback_available"] = "article_feedback" in identifiers
        return payload, "n8n-data-tables"
    finally:
        connection.close()



def load_from_proposals(project_root: Path) -> tuple[dict[str, Any], str]:
    proposal_dir = project_root / "content" / "talent-index-proposals"
    articles: dict[str, dict[str, Any]] = {}
    talents: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}

    for path in sorted(proposal_dir.glob("*.json")):
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        for article in proposal.get("articles", []):
            key = str(article.get("article_key", ""))
            if key:
                articles[key] = article
        for talent in proposal.get("talents", []):
            key = str(talent.get("talent_id", ""))
            if key:
                talents[key] = talent
        for relation in proposal.get("articleTalents", []):
            key = str(relation.get("relation_key", ""))
            if key:
                relations[key] = relation

    return {
        "articles": list(articles.values()),
        "talents": list(talents.values()),
        "article_talents": list(relations.values()),
        "article_classifications": load_classification_proposals(project_root),
        "article_feedback": [],
    }, "proposal-files"



def load_classification_proposals(project_root: Path) -> list[dict[str, Any]]:
    """Load reviewed classification proposals when no Data Table row exists yet."""
    if project.source(project_root, "dashboard") == "project-db":
        with project.reader(project_root) as reader:
            return [row for row, _ in reader.classifications().values()]
    proposal_dir = project_root / "content" / "article-classification-proposals"
    classifications: dict[str, dict[str, Any]] = {}
    for path in sorted(proposal_dir.glob("*.json")):
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in proposal.get("classifications", []):
            if not isinstance(item, dict):
                continue
            article_key = str(item.get("article_key", "")).strip()
            article_url = str(item.get("article_url", "")).strip()
            identifier = article_key or article_url
            if identifier:
                classifications[identifier] = item
    return list(classifications.values())



def load_classification_taxonomy(project_root: Path) -> dict[str, Any]:
    path = project_root / "config" / "article-classification-taxonomy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"articleTypes": [], "categories": [], "relevance": []}
    if not isinstance(data, dict):
        return {"articleTypes": [], "categories": [], "relevance": []}
    return {
        "articleTypes": data.get("articleTypes", []),
        "categories": data.get("categories", []),
        "relevance": data.get("relevance", []),
    }



def load_official_talent_registry(project_root: Path) -> dict[str, Any]:
    if project.source(project_root, "dashboard") == "project-db":
        with project.reader(project_root) as reader:
            documents = list(reader.documents("official-talent-registry"))
            return documents[-1][1] if documents else {"generatedAt": "", "talents": []}
    registry_dir = project_root / "content" / "official-talent-registry"
    for path in sorted(registry_dir.glob("????-??-??.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("talents"), list):
            return data
    return {"generatedAt": "", "talents": []}



def load_article_summaries(project_root: Path) -> dict[str, dict[str, Any]]:
    """Map source-note URLs to their manually reviewed AI summaries."""
    if project.source(project_root, "dashboard") == "project-db":
        with project.reader(project_root) as reader:
            return reader.summaries()
    summary_dir = project_root / "content" / "article-summaries"
    summaries: dict[str, dict[str, Any]] = {}

    for path in sorted(summary_dir.glob("????-??-??.md")):
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue

        for links, entry in parsed_summaries(markdown, path.stem):
            for _, url in links:
                summaries[url.strip()] = entry

    return summaries



def load_article_capture_metadata(project_root: Path) -> dict[str, dict[str, str]]:
    """Map saved article URLs to resolved source details captured during review."""
    if project.source(project_root, "dashboard") == "project-db":
        with project.reader(project_root) as reader:
            return reader.capture_metadata()
    path = project_root / "content" / "article-body-captures" / "backfill-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    if not isinstance(entries, dict):
        return {}

    metadata: dict[str, dict[str, str]] = {}
    for original_url, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        key = str(original_url or "").strip()
        if not key:
            continue
        metadata[key] = {
            "resolved_url": str(entry.get("resolved_url") or "").strip(),
            "source_host": str(entry.get("source_host") or "").strip(),
        }
    return metadata

