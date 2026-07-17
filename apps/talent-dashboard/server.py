#!/usr/bin/env python3
"""Read-only local server for the Talent Index dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parents[1]
STATIC_ROOT = APP_ROOT / "web"
TABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
SOURCE_NOTES_HEADING = "## Source-by-source Notes"


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


def load_from_n8n() -> tuple[dict[str, Any], str]:
    path = database_path()
    if not path.exists():
        raise FileNotFoundError(f"n8n database was not found: {path}")

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, name FROM data_table WHERE name IN ('talents', 'articles', 'article_talents')"
        ).fetchall()
        identifiers = {row["name"]: row["id"] for row in rows}
        missing = {"talents", "articles", "article_talents"}.difference(identifiers)
        if missing:
            raise RuntimeError(f"Missing n8n Data Tables: {', '.join(sorted(missing))}")

        payload: dict[str, Any] = {}
        for name in ("talents", "articles", "article_talents"):
            table = quoted_table_name(identifiers[name])
            payload[name] = [normalise_row(row) for row in connection.execute(f"SELECT * FROM {table}")]
        return payload, "n8n-data-tables"
    finally:
        connection.close()


def load_from_proposals() -> tuple[dict[str, Any], str]:
    proposal_dir = PROJECT_ROOT / "content" / "talent-index-proposals"
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
    }, "proposal-files"


def source_note_items(markdown: str) -> list[str]:
    """Return the top-level bullets beneath the source-by-source heading."""
    if SOURCE_NOTES_HEADING not in markdown:
        return []

    section = markdown.split(SOURCE_NOTES_HEADING, 1)[1]
    section = re.split(r"^##\s+", section, maxsplit=1, flags=re.MULTILINE)[0]
    items: list[str] = []
    current: str | None = None
    for line in section.splitlines():
        if line.startswith("- "):
            if current:
                items.append(current)
            current = line[2:].strip()
        elif current and line.strip():
            current = f"{current} {line.strip()}"
    if current:
        items.append(current)
    return items


def load_article_summaries() -> dict[str, dict[str, Any]]:
    """Map source-note URLs to their manually reviewed AI summaries."""
    summary_dir = PROJECT_ROOT / "content" / "article-summaries"
    summaries: dict[str, dict[str, Any]] = {}

    for path in sorted(summary_dir.glob("????-??-??.md")):
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue

        for item in source_note_items(markdown):
            links = MARKDOWN_LINK_PATTERN.findall(item)
            if not links:
                continue
            without_links = MARKDOWN_LINK_PATTERN.sub("", item).strip()
            summary_parts = re.split(r"[:：]", without_links, maxsplit=1)
            if len(summary_parts) < 2 or not summary_parts[1].strip():
                continue
            entry = {
                "text": re.sub(r"\s+", " ", summary_parts[1]).strip(),
                "summary_date": path.stem,
                "source_titles": [title for title, _ in links],
            }
            for _, url in links:
                summaries[url.strip()] = entry

    return summaries


def date_key(value: Any) -> str:
    return str(value or "")[:10]


def build_dashboard() -> dict[str, Any]:
    error: str | None = None
    try:
        payload, source = load_from_n8n()
    except Exception as exc:  # Fallback keeps the dashboard usable without n8n.
        payload, source = load_from_proposals()
        error = str(exc)

    talents = payload["talents"]
    articles = payload["articles"]
    relations = payload["article_talents"]
    article_summaries = load_article_summaries()
    article_map = {str(article.get("article_key", "")): article for article in articles}
    talent_map = {str(talent.get("talent_id", "")): talent for talent in talents}

    relation_counts: dict[str, int] = {}
    article_talents: dict[str, list[dict[str, Any]]] = {}
    talent_articles: dict[str, list[dict[str, Any]]] = {}
    enriched_relations: list[dict[str, Any]] = []
    for relation in relations:
        talent_id = str(relation.get("talent_id", ""))
        article_key = str(relation.get("article_key", ""))
        relation_counts[talent_id] = relation_counts.get(talent_id, 0) + 1
        article_talents.setdefault(article_key, []).append(talent_map.get(talent_id, {}))
        talent_articles.setdefault(talent_id, []).append(article_map.get(article_key, {}))
        enriched_relations.append(
            {
                **relation,
                "talent": talent_map.get(talent_id, {}),
                "article": article_map.get(article_key, {}),
            }
        )

    enriched_talents = [
        {**talent, "article_count": relation_counts.get(str(talent.get("talent_id", "")), 0)}
        for talent in talents
    ]
    enriched_articles = []
    for article in articles:
        summary = article_summaries.get(str(article.get("url", "")).strip(), {})
        enriched_articles.append(
            {
                **article,
                "talents": [talent for talent in article_talents.get(str(article.get("article_key", "")), []) if talent],
                "ai_summary": summary.get("text", ""),
                "summary_date": summary.get("summary_date", ""),
                "summary_source_titles": summary.get("source_titles", []),
            }
        )

    daily_volume: dict[str, int] = {}
    for article in enriched_articles:
        key = date_key(article.get("published_at") or article.get("last_seen_at"))
        if key:
            daily_volume[key] = daily_volume.get(key, 0) + 1

    organizations = sorted(
        {str(talent.get("organization", "")).strip() for talent in enriched_talents if str(talent.get("organization", "")).strip()},
        key=str.lower,
    )
    status_counts: dict[str, int] = {}
    for talent in enriched_talents:
        status = str(talent.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "sourceError": error,
        "summary": {
            "talents": len(enriched_talents),
            "articles": len(enriched_articles),
            "relations": len(enriched_relations),
            "searchEnabled": sum(1 for talent in enriched_talents if talent.get("search_enabled")),
            "articleSummaries": sum(1 for article in enriched_articles if article.get("ai_summary")),
            "statusCounts": status_counts,
            "dailyVolume": [{"date": date, "count": daily_volume[date]} for date in sorted(daily_volume)],
            "organizations": organizations,
        },
        "talents": sorted(enriched_talents, key=lambda item: (str(item.get("display_name", "")).lower(), str(item.get("talent_id", "")))),
        "articles": sorted(enriched_articles, key=lambda item: str(item.get("published_at") or item.get("last_seen_at") or ""), reverse=True),
        "relations": sorted(enriched_relations, key=lambda item: str(item.get("last_seen_at", "")), reverse=True),
        "talentArticles": talent_articles,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/dashboard":
            payload = json.dumps(build_dashboard(), ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if urlparse(self.path).path == "/api/health":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[talent-dashboard] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Talent Index dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Talent Index dashboard: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
