#!/usr/bin/env python3
"""Local server for the Talent Index dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parents[1]
STATIC_ROOT = APP_ROOT / "web"
TABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
SOURCE_NOTES_HEADING = "## Source-by-source Notes"
KEYWORD_MUTATION_LOCK = threading.RLock()


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


def keyword_identity(value: Any) -> str:
    return " ".join(str(value or "").strip().replace("！", "!").split()).casefold()


def load_keyword_config() -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "keywords.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config/keywords.json must contain an object")
    return data


def load_keyword_runtime() -> tuple[list[str], str | None]:
    path = database_path()
    if not path.exists():
        return [], f"n8n database was not found: {path}"

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT staticData FROM workflow_entity WHERE name = ?",
            ("Daily Keyword News Summary",),
        ).fetchone()
        if row is None:
            raise RuntimeError("Daily Keyword News Summary workflow was not found")
        raw_static_data = row[0] or "{}"
        static_data = json.loads(raw_static_data)
        global_data = static_data.get("global", {}) if isinstance(static_data, dict) else {}
        keywords = global_data.get("autoKeywords", []) if isinstance(global_data, dict) else []
        return [str(term).strip() for term in keywords if str(term).strip()], None
    except (OSError, sqlite3.Error, json.JSONDecodeError, RuntimeError) as exc:
        return [], str(exc)
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def load_latest_keyword_candidates() -> tuple[str, list[dict[str, Any]]]:
    candidate_dir = PROJECT_ROOT / "content" / "ai-keyword-candidates"
    paths = sorted(candidate_dir.glob("????-??-??.md"))
    if not paths:
        raise FileNotFoundError("No AI keyword candidate file was found")

    path = paths[-1]
    section = path.read_text(encoding="utf-8")
    table = section.split("## Candidates", 1)
    if len(table) != 2:
        raise ValueError(f"Candidates table is missing in {path}")
    table_text = table[1].split("## Suggested Default Keywords", 1)[0]
    candidates: list[dict[str, Any]] = []
    for raw_line in table_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        columns = [cell.strip() for cell in line.strip("|").split("|")]
        if not columns or columns[0] in {"Candidate", "---"} or all(set(cell) <= {"-", ":"} for cell in columns):
            continue
        if len(columns) != 6:
            continue
        keyword, category, confidence, add, reason, evidence = columns
        try:
            confidence_value = float(confidence)
        except ValueError:
            confidence_value = 0.0
        candidates.append(
            {
                "keyword": keyword,
                "category": category,
                "confidence": confidence_value,
                "recommended": add.lower() == "yes",
                "reason": reason,
                "evidence": evidence,
            }
        )
    return path.stem, candidates


def keyword_candidates_payload() -> dict[str, Any]:
    candidate_date, candidates = load_latest_keyword_candidates()
    config = load_keyword_config()
    manual = [str(term).strip() for term in config.get("manualKeywords", []) if str(term).strip()]
    automatic, runtime_error = load_keyword_runtime()
    current_by_identity: dict[str, str] = {}
    for term in [*manual, *automatic]:
        current_by_identity.setdefault(keyword_identity(term), term)

    for candidate in candidates:
        existing = current_by_identity.get(keyword_identity(candidate["keyword"]))
        candidate["state"] = "added" if existing else ("eligible" if candidate["recommended"] else "not_recommended")
        candidate["existingKeyword"] = existing or ""

    return {
        "candidateDate": candidate_date,
        "candidates": candidates,
        "manualKeywords": manual,
        "automaticKeywords": automatic,
        "currentKeywordCount": len(current_by_identity),
        "runtimeError": runtime_error,
    }



def keyword_config_path() -> Path:
    return PROJECT_ROOT / "config" / "keywords.json"


def validate_keyword(value: Any) -> str:
    keyword = str(value or "").strip()
    if len(keyword) < 2 or len(keyword) > 30:
        raise ValueError("Keyword must contain 2 to 30 characters")
    if re.search(r"[|/\\\r\n]", keyword) or re.match(r"^https?:", keyword, flags=re.IGNORECASE):
        raise ValueError("Keyword contains unsupported characters")
    return keyword


def write_keyword_config(config: dict[str, Any]) -> None:
    path = keyword_config_path()
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def keyword_management_payload() -> dict[str, Any]:
    config = load_keyword_config()
    manual = [str(term).strip() for term in config.get("manualKeywords", []) if str(term).strip()]
    automatic, runtime_error = load_keyword_runtime()
    return {
        "manualKeywords": manual,
        "automaticKeywords": automatic,
        "excludedKeywords": [str(term).strip() for term in config.get("excludedKeywords", []) if str(term).strip()],
        "maxAutoKeywords": max(0, int(config.get("maxAutoKeywords", 30) or 0)),
        "runtimeError": runtime_error,
    }


def call_keyword_management_webhook(operation: str, keyword: str, previous_keyword: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "operation": operation,
            "keyword": keyword,
            "previousKeyword": previous_keyword,
            "source": "talent-dashboard",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    webhook_url = os.environ.get(
        "N8N_KEYWORD_MANAGEMENT_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/keyword-management/update",
    )
    webhook_request = request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(webhook_request, timeout=30) as response:
            raw_response = response.read().decode("utf-8")
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"n8n returned HTTP {exc.code}: {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Could not connect to n8n: {exc.reason}") from exc
    try:
        result = json.loads(raw_response) if raw_response else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("n8n returned an invalid response") from exc
    if not isinstance(result, dict):
        raise RuntimeError("n8n returned an unexpected response")
    if result.get("status") == "rejected":
        raise ValueError(str(result.get("reason") or "n8n rejected the keyword"))
    return result


def manage_manual_keyword(operation: str, keyword: str, previous_keyword: str) -> dict[str, Any]:
    with KEYWORD_MUTATION_LOCK:
        config = load_keyword_config()
        manual = [str(term).strip() for term in config.get("manualKeywords", []) if str(term).strip()]
        target_identity = keyword_identity(previous_keyword or keyword)
        index = next((position for position, term in enumerate(manual) if keyword_identity(term) == target_identity), None)

        if operation == "add":
            keyword = validate_keyword(keyword)
            if any(keyword_identity(term) == keyword_identity(keyword) for term in manual):
                return {"status": "already_added", "keyword": next(term for term in manual if keyword_identity(term) == keyword_identity(keyword))}
            manual.append(keyword)
            status = "added"
        elif operation == "edit":
            keyword = validate_keyword(keyword)
            if index is None:
                raise ValueError("The manual keyword no longer exists")
            if keyword_identity(manual[index]) == keyword_identity(keyword):
                return {"status": "unchanged", "keyword": manual[index]}
            if any(position != index and keyword_identity(term) == keyword_identity(keyword) for position, term in enumerate(manual)):
                raise ValueError("The keyword already exists in the manual list")
            manual[index] = keyword
            status = "updated"
        elif operation == "remove":
            if index is None:
                return {"status": "already_removed", "keyword": previous_keyword or keyword}
            if len(manual) <= 1:
                raise ValueError("At least one manual keyword must remain")
            keyword = manual.pop(index)
            status = "removed"
        else:
            raise ValueError("Unsupported keyword operation")

        config["manualKeywords"] = manual
        write_keyword_config(config)
        return {"status": status, "keyword": keyword}


def manage_keyword(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation", "")).strip().lower()
    scope = str(payload.get("scope", "")).strip().lower()
    keyword = str(payload.get("keyword", "")).strip()
    previous_keyword = str(payload.get("previousKeyword", "")).strip()
    if operation not in {"add", "edit", "remove"}:
        raise ValueError("operation must be add, edit, or remove")
    if scope not in {"manual", "automatic"}:
        raise ValueError("scope must be manual or automatic")
    if operation in {"add", "edit"}:
        keyword = validate_keyword(keyword)
    if operation in {"edit", "remove"} and not previous_keyword:
        raise ValueError("previousKeyword is required for edit and remove")

    if scope == "manual":
        return manage_manual_keyword(operation, keyword, previous_keyword)
    with KEYWORD_MUTATION_LOCK:
        return call_keyword_management_webhook(operation, keyword, previous_keyword)


def add_keyword_candidate(keyword: str) -> dict[str, Any]:
    payload = keyword_candidates_payload()
    normalized = keyword_identity(keyword)
    candidate = next(
        (item for item in payload["candidates"] if keyword_identity(item["keyword"]) == normalized),
        None,
    )
    if candidate is None:
        raise ValueError("The keyword is not in the latest AI candidate file")
    if not candidate["recommended"]:
        raise ValueError("Only candidates marked Add: yes can be added")
    if candidate["state"] == "added":
        return {"status": "already_added", "keyword": candidate["existingKeyword"] or candidate["keyword"]}

    body = json.dumps(
        {
            "keyword": candidate["keyword"],
            "candidateDate": payload["candidateDate"],
            "source": "talent-dashboard",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    webhook_url = os.environ.get(
        "N8N_KEYWORD_CANDIDATE_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/keyword-candidate/add",
    )
    webhook_request = request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(webhook_request, timeout=30) as response:
            raw_response = response.read().decode("utf-8")
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"n8n returned HTTP {exc.code}: {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Could not connect to n8n: {exc.reason}") from exc

    try:
        result = json.loads(raw_response) if raw_response else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("n8n returned an invalid response") from exc
    if not isinstance(result, dict):
        raise RuntimeError("n8n returned an unexpected response")
    return result


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

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > 8192:
            raise ValueError("Request body must be between 1 and 8192 bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/dashboard":
            self.send_json(HTTPStatus.OK, build_dashboard())
            return
        if path == "/api/keyword-candidates":
            try:
                self.send_json(HTTPStatus.OK, keyword_candidates_payload())
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/keywords":
            try:
                self.send_json(HTTPStatus.OK, keyword_management_payload())
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/health":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self.read_json_body()
            if path == "/api/keyword-candidates/add":
                keyword = str(payload.get("keyword", "")).strip()
                if not keyword:
                    raise ValueError("keyword is required")
                self.send_json(HTTPStatus.OK, add_keyword_candidate(keyword))
                return
            if path == "/api/keywords":
                self.send_json(HTTPStatus.OK, manage_keyword(payload))
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RuntimeError as exc:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

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
