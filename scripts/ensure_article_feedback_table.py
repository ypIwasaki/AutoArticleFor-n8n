#!/usr/bin/env python3
"""Create the n8n Data Table used to store rejected article feedback."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from sync_workflow_to_n8n import api_request, load_env_file, normalize_api_base_url


TABLE_NAME = "article_feedback"
COLUMNS = [
    {"name": "article_key", "type": "string"},
    {"name": "article_url", "type": "string"},
    {"name": "is_rejected", "type": "boolean"},
    {"name": "reason_code", "type": "string"},
    {"name": "source_domain", "type": "string"},
    {"name": "publisher_label", "type": "string"},
    {"name": "title_signature", "type": "string"},
    {"name": "reviewed_at", "type": "date"},
    {"name": "review_source", "type": "string"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ensure the article_feedback n8n Data Table exists.")
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args()


def list_tables(api_base_url: str, api_key: str) -> list[dict[str, Any]]:
    response = api_request("GET", api_base_url, "/data-tables", api_key)
    data = response.get("data", []) if isinstance(response, dict) else []
    return [item for item in data if isinstance(item, dict)]


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))
    api_base_url = normalize_api_base_url(
        os.environ.get("N8N_API_BASE_URL") or os.environ.get("N8N_BASE_URL") or ""
    )
    api_key = os.environ.get("N8N_API_KEY")
    if not api_key:
        raise RuntimeError("N8N_API_KEY is required.")

    existing = next((item for item in list_tables(api_base_url, api_key) if item.get("name") == TABLE_NAME), None)
    if existing:
        print(f"Data Table already exists: {TABLE_NAME} ({existing.get('id')})")
        return 0

    created = api_request(
        "POST",
        api_base_url,
        "/data-tables",
        api_key,
        {"name": TABLE_NAME, "columns": COLUMNS},
    )
    print(f"Created Data Table: {TABLE_NAME} ({created.get('id')})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}")
        raise SystemExit(1)
