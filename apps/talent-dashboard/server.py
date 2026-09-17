#!/usr/bin/env python3
"""Local server for the Talent Index dashboard."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import article_feedback_service as feedback_service
import talent_dashboard_data as dashboard_data
from keyword_service import KeywordService
from talent_dashboard_data import database_path, normalise_row, quoted_table_name
from article_feedback_service import (
    article_publisher_label,
    canonical_article_title,
    feedback_is_rejected,
    source_domain_for_article,
)
STATIC_ROOT = APP_ROOT / "web"
from article_artifact_formats import (
    WEEKLY_REPORT_FILENAME_PATTERN,
    WEEKLY_REPORT_FRONT_MATTER_PATTERN,
    weekly_report_metadata,
)


def load_from_n8n() -> tuple[dict[str, Any], str]:
    return dashboard_data.load_dashboard_records(PROJECT_ROOT)


def load_from_proposals() -> tuple[dict[str, Any], str]:
    return dashboard_data.load_from_proposals(PROJECT_ROOT)


def load_classification_proposals() -> list[dict[str, Any]]:
    return dashboard_data.load_classification_proposals(PROJECT_ROOT)

def load_classification_taxonomy() -> dict[str, Any]:
    return dashboard_data.load_classification_taxonomy(PROJECT_ROOT)


def official_identity(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).casefold()


def value_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def load_official_talent_registry() -> dict[str, Any]:
    return dashboard_data.load_official_talent_registry(PROJECT_ROOT)


def load_article_summaries() -> dict[str, dict[str, Any]]:
    return dashboard_data.load_article_summaries(PROJECT_ROOT)


def load_article_capture_metadata() -> dict[str, dict[str, str]]:
    return dashboard_data.load_article_capture_metadata(PROJECT_ROOT)


def weekly_report_directory() -> Path:
    return PROJECT_ROOT / "content" / "weekly-reports"


def weekly_report_summary(markdown: str) -> str:
    body = WEEKLY_REPORT_FRONT_MATTER_PATTERN.sub("", markdown, count=1)
    body = re.sub(r"^#.*$", "", body, count=1, flags=re.MULTILINE)
    for line in body.splitlines():
        text = line.strip().lstrip("- ").strip()
        if text and not text.startswith("#") and not text.startswith("|"):
            return re.sub(r"\s+", " ", text)[:180]
    return ""


def load_weekly_report(week_start: str) -> dict[str, Any]:
    filename = f"{week_start}.md"
    if not WEEKLY_REPORT_FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("Invalid weekly report identifier")
    path = weekly_report_directory() / filename
    if not path.exists():
        raise FileNotFoundError("Weekly report was not found")
    markdown = path.read_text(encoding="utf-8")
    return {**weekly_report_metadata(markdown, path), "markdown": markdown}


def weekly_reports_payload() -> dict[str, Any]:
    reports: list[dict[str, str]] = []
    for path in sorted(weekly_report_directory().glob("????-??-??.md"), reverse=True):
        if not WEEKLY_REPORT_FILENAME_PATTERN.fullmatch(path.name):
            continue
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue
        reports.append({**weekly_report_metadata(markdown, path), "summary": weekly_report_summary(markdown)})
    return {"reports": reports}


def date_key(value: Any) -> str:
    return str(value or "")[:10]


def keyword_service() -> KeywordService:
    return KeywordService(PROJECT_ROOT, dashboard_data.database_path())


def keyword_candidates_payload() -> dict[str, Any]:
    return keyword_service().candidate_payload()


def keyword_management_payload() -> dict[str, Any]:
    return keyword_service().management_payload()


def manage_keyword(payload: dict[str, Any]) -> dict[str, Any]:
    return keyword_service().manage_keyword(payload)


def add_keyword_candidate(keyword: str) -> dict[str, Any]:
    return keyword_service().add_candidate(keyword)


def write_article_feedback_instruction(
    payload: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> Path:
    if payload is None:
        payload, _ = load_from_n8n()
    return feedback_service.write_article_feedback_instruction(
        PROJECT_ROOT, payload, generated_at
    )


def evaluate_article(payload: dict[str, Any]) -> dict[str, Any]:
    return feedback_service.evaluate_article(payload, PROJECT_ROOT)


def build_dashboard() -> dict[str, Any]:
    error: str | None = None
    try:
        payload, source = load_from_n8n()
    except Exception as exc:  # Legacy proposal fallback only; DB errors remain visible.
        import project_readers as project
        if project.source(PROJECT_ROOT, "dashboard") == "project-db":
            raise
        payload, source = load_from_proposals()
        error = str(exc)

    talents = payload["talents"]
    all_articles = payload["articles"]
    article_feedback_by_key = {
        str(feedback.get("article_key") or "").strip(): feedback
        for feedback in payload.get("article_feedback", [])
        if str(feedback.get("article_key") or "").strip()
    }
    rejected_feedback_by_key = {
        article_key: feedback
        for article_key, feedback in article_feedback_by_key.items()
        if feedback_is_rejected(feedback.get("is_rejected"))
    }
    articles = [
        article
        for article in all_articles
        if str(article.get("article_key") or "").strip() not in rejected_feedback_by_key
    ]
    visible_article_keys = {str(article.get("article_key") or "").strip() for article in articles}
    all_relations = payload["article_talents"]
    relations = [
        relation
        for relation in all_relations
        if str(relation.get("article_key") or "").strip() in visible_article_keys
    ]
    article_summaries = load_article_summaries()
    classification_taxonomy = load_classification_taxonomy()
    official_registry = load_official_talent_registry()
    registry_by_org_name: dict[tuple[str, str], dict[str, Any]] = {}
    registry_by_name: dict[str, list[dict[str, Any]]] = {}
    for registry_talent in official_registry.get("talents", []):
        if not isinstance(registry_talent, dict):
            continue
        organization = official_identity(registry_talent.get("organization"))
        names = [registry_talent.get("display_name"), *registry_talent.get("aliases", [])]
        for value in names:
            name = official_identity(value)
            if not name:
                continue
            registry_by_org_name[(organization, name)] = registry_talent
            matches = registry_by_name.setdefault(name, [])
            if registry_talent not in matches:
                matches.append(registry_talent)

    def official_record_for(talent: dict[str, Any]) -> dict[str, Any] | None:
        organization = official_identity(talent.get("organization"))
        names = [talent.get("display_name"), *value_list(talent.get("aliases_json"))]
        for value in names:
            name = official_identity(value)
            if not name:
                continue
            match = registry_by_org_name.get((organization, name))
            if match:
                return match
        for value in names:
            matches = registry_by_name.get(official_identity(value), [])
            if len(matches) == 1:
                return matches[0]
        return None

    enriched_talent_records: list[dict[str, Any]] = []
    for talent in talents:
        official = official_record_for(talent)
        official_fields = {}
        if official:
            official_fields = {
                "officialProfileUrl": str(official.get("profile_url") or ""),
                "officialRosterUrl": str(official.get("source_url") or ""),
                "officialGroupId": str(official.get("group_id") or ""),
                "officialGroupName": str(official.get("group_name") or ""),
                "officialRegistryUpdatedAt": str(official_registry.get("generatedAt") or ""),
            }
        enriched_talent_records.append({**talent, **official_fields})

    article_map = {str(article.get("article_key", "")): article for article in all_articles}
    article_key_by_url = {
        str(article.get("url", "")).strip(): str(article.get("article_key", "")).strip()
        for article in all_articles
        if str(article.get("url", "")).strip() and str(article.get("article_key", "")).strip()
    }
    classification_map: dict[str, dict[str, Any]] = {}
    for classification in load_classification_proposals():
        article_key = str(classification.get("article_key", "")).strip()
        if not article_key:
            article_key = article_key_by_url.get(str(classification.get("article_url", "")).strip(), "")
        if article_key:
            classification_map[article_key] = classification
    for classification in payload.get("article_classifications", []):
        article_key = str(classification.get("article_key", "")).strip()
        if article_key:
            classification_map[article_key] = classification
    talent_map = {str(talent.get("talent_id", "")): talent for talent in enriched_talent_records}

    relation_counts: dict[str, int] = {}
    article_talents: dict[str, list[dict[str, Any]]] = {}
    for relation in all_relations:
        article_key = str(relation.get("article_key", ""))
        talent_id = str(relation.get("talent_id", ""))
        article_talents.setdefault(article_key, []).append(talent_map.get(talent_id, {}))

    talent_articles: dict[str, list[dict[str, Any]]] = {}
    enriched_relations: list[dict[str, Any]] = []
    for relation in relations:
        talent_id = str(relation.get("talent_id", ""))
        article_key = str(relation.get("article_key", ""))
        relation_counts[talent_id] = relation_counts.get(talent_id, 0) + 1
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
        for talent in enriched_talent_records
    ]
    enriched_articles = []
    for article in all_articles:
        summary = article_summaries.get(str(article.get("url", "")).strip(), {})
        enriched_articles.append(
            {
                **article,
                "talents": [talent for talent in article_talents.get(str(article.get("article_key", "")), []) if talent],
                "ai_summary": summary.get("text", ""),
                "summary_date": summary.get("summary_date", ""),
                "summary_source_titles": summary.get("source_titles", []),
                "classification": classification_map.get(str(article.get("article_key", "")), {}),
                "feedback": article_feedback_by_key.get(str(article.get("article_key", "")), {}),
            }
        )

    visible_enriched_articles = [
        article
        for article in enriched_articles
        if not feedback_is_rejected((article.get("feedback") or {}).get("is_rejected"))
    ]

    daily_volume: dict[str, int] = {}
    for article in visible_enriched_articles:
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

    article_type_counts: dict[str, int] = {}
    primary_category_counts: dict[str, int] = {}
    relevance_counts: dict[str, int] = {}
    for article in visible_enriched_articles:
        classification = article.get("classification", {})
        if not classification:
            continue
        article_type = str(classification.get("article_type", "")).strip()
        primary_category = str(classification.get("primary_category", "")).strip()
        relevance = str(classification.get("relevance", "")).strip()
        if article_type:
            article_type_counts[article_type] = article_type_counts.get(article_type, 0) + 1
        if primary_category:
            primary_category_counts[primary_category] = primary_category_counts.get(primary_category, 0) + 1
        if relevance:
            relevance_counts[relevance] = relevance_counts.get(relevance, 0) + 1

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "sourceError": error,
        "summary": {
            "talents": len(enriched_talents),
            "articles": len(visible_enriched_articles),
            "rejectedArticles": len(rejected_feedback_by_key),
            "reviewedArticles": len(article_feedback_by_key),
            "relations": len(enriched_relations),
            "searchEnabled": sum(1 for talent in enriched_talents if talent.get("search_enabled")),
            "articleSummaries": sum(1 for article in visible_enriched_articles if article.get("ai_summary")),
            "articleClassifications": sum(1 for article in visible_enriched_articles if article.get("classification")),
            "statusCounts": status_counts,
            "articleTypeCounts": article_type_counts,
            "primaryCategoryCounts": primary_category_counts,
            "relevanceCounts": relevance_counts,
            "dailyVolume": [{"date": date, "count": daily_volume[date]} for date in sorted(daily_volume)],
            "organizations": organizations,
        },
        "classificationTaxonomy": classification_taxonomy,
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
        if path == "/api/weekly-reports":
            self.send_json(HTTPStatus.OK, weekly_reports_payload())
            return
        if path.startswith("/api/weekly-reports/"):
            try:
                week_start = path[len("/api/weekly-reports/"):]
                self.send_json(HTTPStatus.OK, load_weekly_report(week_start))
            except (OSError, ValueError) as exc:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
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
            if path == "/api/article-feedback":
                self.send_json(HTTPStatus.OK, evaluate_article(payload))
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
