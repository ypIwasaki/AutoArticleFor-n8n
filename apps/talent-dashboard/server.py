#!/usr/bin/env python3
"""Local server for the Talent Index dashboard."""

from __future__ import annotations

import argparse
import json
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
from talent_dashboard_presenter import build_dashboard_payload
import weekly_report_reader as weekly_reports
from talent_dashboard_data import database_path, normalise_row, quoted_table_name
from article_feedback_service import (
    article_publisher_label,
    canonical_article_title,
    feedback_is_rejected,
    source_domain_for_article,
)
STATIC_ROOT = APP_ROOT / "web"


def load_from_n8n() -> tuple[dict[str, Any], str]:
    return dashboard_data.load_dashboard_records(PROJECT_ROOT)


def load_from_proposals() -> tuple[dict[str, Any], str]:
    return dashboard_data.load_from_proposals(PROJECT_ROOT)


def load_classification_proposals() -> list[dict[str, Any]]:
    return dashboard_data.load_classification_proposals(PROJECT_ROOT)

def load_classification_taxonomy() -> dict[str, Any]:
    return dashboard_data.load_classification_taxonomy(PROJECT_ROOT)


def load_official_talent_registry() -> dict[str, Any]:
    return dashboard_data.load_official_talent_registry(PROJECT_ROOT)


def load_article_summaries() -> dict[str, dict[str, Any]]:
    return dashboard_data.load_article_summaries(PROJECT_ROOT)


def load_article_capture_metadata() -> dict[str, dict[str, str]]:
    return dashboard_data.load_article_capture_metadata(PROJECT_ROOT)


def load_weekly_report(week_start: str) -> dict[str, Any]:
    return weekly_reports.load_weekly_report(PROJECT_ROOT, week_start)


def weekly_reports_payload() -> dict[str, Any]:
    return weekly_reports.weekly_reports_payload(PROJECT_ROOT)


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

    return build_dashboard_payload(
        payload,
        source=source,
        source_error=error,
        article_summaries=load_article_summaries(),
        classification_taxonomy=load_classification_taxonomy(),
        official_registry=load_official_talent_registry(),
        classification_proposals=load_classification_proposals(),
        generated_at=datetime.now(timezone.utc),
    )


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
