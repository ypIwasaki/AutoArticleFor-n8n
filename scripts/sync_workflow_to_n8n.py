#!/usr/bin/env python3
"""Sync a local n8n workflow JSON file to an existing n8n workflow via API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_WORKFLOW_FILE = "n8n/workflows/daily-keyword-news-summary.workflow.json"
UPDATE_FIELDS = ("name", "nodes", "connections", "settings")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


def normalize_api_base_url(raw_url: str) -> str:
    url = raw_url.rstrip("/")
    if not url:
        raise ValueError("n8n API base URL is empty")
    if not url.endswith("/api/v1"):
        url = f"{url}/api/v1"
    return url


def api_request(
    method: str,
    api_base_url: str,
    path: str,
    api_key: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
        "X-N8N-API-KEY": api_key,
    }

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"{api_base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"n8n API request failed: {method} {path} "
            f"returned HTTP {error.code}\n{details}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not connect to n8n API at {api_base_url}: {error.reason}"
        ) from error

    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def load_workflow(path: Path) -> dict[str, Any]:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    missing = [field for field in ("name", "nodes", "connections") if field not in workflow]
    if missing:
        raise ValueError(f"Workflow JSON is missing required fields: {', '.join(missing)}")
    return workflow


def build_update_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    payload = {field: workflow.get(field) for field in UPDATE_FIELDS if field in workflow}
    payload.setdefault("settings", {})
    return payload


def list_workflows(api_base_url: str, api_key: str) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        query = {"limit": "100"}
        if cursor:
            query["cursor"] = cursor
        path = f"/workflows?{urllib.parse.urlencode(query)}"
        response = api_request("GET", api_base_url, path, api_key)

        data = response.get("data", response if isinstance(response, list) else [])
        workflows.extend(data)

        cursor = response.get("nextCursor") if isinstance(response, dict) else None
        if not cursor:
            break

    return workflows


def find_workflow_id_by_name(
    api_base_url: str,
    api_key: str,
    workflow_name: str,
) -> str:
    matches = [
        workflow
        for workflow in list_workflows(api_base_url, api_key)
        if workflow.get("name") == workflow_name
    ]

    if not matches:
        raise RuntimeError(f"No n8n workflow found with name: {workflow_name}")
    if len(matches) > 1:
        ids = ", ".join(str(workflow.get("id")) for workflow in matches)
        raise RuntimeError(
            f"Multiple n8n workflows found with name {workflow_name!r}: {ids}. "
            "Set N8N_WORKFLOW_ID explicitly."
        )

    return str(matches[0]["id"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync a local n8n workflow JSON file to an existing n8n workflow."
    )
    parser.add_argument(
        "--workflow-file",
        default=os.environ.get("N8N_WORKFLOW_FILE", DEFAULT_WORKFLOW_FILE),
        help=f"Local workflow JSON file. Default: {DEFAULT_WORKFLOW_FILE}",
    )
    parser.add_argument(
        "--workflow-id",
        default=os.environ.get("N8N_WORKFLOW_ID"),
        help="Existing n8n workflow ID to update.",
    )
    parser.add_argument(
        "--workflow-name",
        default=os.environ.get("N8N_WORKFLOW_NAME"),
        help="Find the existing n8n workflow by exact name if --workflow-id is not set.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("N8N_API_BASE_URL") or os.environ.get("N8N_BASE_URL"),
        help="n8n base URL or API root. Example: http://localhost:5678",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("N8N_API_KEY"),
        help="n8n API key. Prefer setting N8N_API_KEY in .env instead.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional env file to load before reading options. Default: .env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the local workflow and print the intended target without updating n8n.",
    )
    parser.add_argument(
        "--active-state",
        choices=("preserve", "from-json", "activate", "deactivate", "ignore"),
        default=os.environ.get("N8N_ACTIVE_STATE", "preserve"),
        help=(
            "Workflow active state after update. "
            "Default: preserve the current n8n state."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List workflows in n8n and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))

    workflow_file_value = args.workflow_file
    if (
        workflow_file_value == DEFAULT_WORKFLOW_FILE
        and os.environ.get("N8N_WORKFLOW_FILE")
    ):
        workflow_file_value = os.environ["N8N_WORKFLOW_FILE"]

    active_state = args.active_state
    if active_state == "preserve" and os.environ.get("N8N_ACTIVE_STATE"):
        active_state = os.environ["N8N_ACTIVE_STATE"]
        if active_state not in {"preserve", "from-json", "activate", "deactivate", "ignore"}:
            raise ValueError(f"Invalid N8N_ACTIVE_STATE: {active_state}")

    workflow_file = Path(workflow_file_value)
    workflow = load_workflow(workflow_file)
    payload = build_update_payload(workflow)

    api_base_url = normalize_api_base_url(
        args.base_url or os.environ.get("N8N_API_BASE_URL") or os.environ.get("N8N_BASE_URL") or ""
    )
    api_key = args.api_key or os.environ.get("N8N_API_KEY")
    if not api_key:
        raise RuntimeError("N8N_API_KEY is required.")

    if args.list:
        for item in list_workflows(api_base_url, api_key):
            print(f"{item.get('id')}\t{item.get('name')}\tactive={item.get('active')}")
        return 0

    workflow_id = args.workflow_id or os.environ.get("N8N_WORKFLOW_ID")
    workflow_name = args.workflow_name or os.environ.get("N8N_WORKFLOW_NAME") or workflow["name"]
    if not workflow_id:
        workflow_id = find_workflow_id_by_name(api_base_url, api_key, workflow_name)

    if args.dry_run:
        print(f"Workflow file: {workflow_file}")
        print(f"Local workflow name: {workflow['name']}")
        print(f"Target n8n API: {api_base_url}")
        print(f"Target workflow ID: {workflow_id}")
        print(f"Active-state mode: {active_state}")
        print("Dry run only. No API update was sent.")
        return 0

    current = api_request("GET", api_base_url, f"/workflows/{workflow_id}", api_key)
    current_active = bool(current.get("active")) if isinstance(current, dict) else False

    updated = api_request("PUT", api_base_url, f"/workflows/{workflow_id}", api_key, payload)

    desired_active: bool | None
    if active_state == "preserve":
        desired_active = current_active
    elif active_state == "from-json":
        desired_active = bool(workflow.get("active"))
    elif active_state == "activate":
        desired_active = True
    elif active_state == "deactivate":
        desired_active = False
    else:
        desired_active = None

    if desired_active is True:
        api_request("POST", api_base_url, f"/workflows/{workflow_id}/activate", api_key)
    elif desired_active is False:
        api_request("POST", api_base_url, f"/workflows/{workflow_id}/deactivate", api_key)

    print(f"Updated n8n workflow {workflow_id}: {updated.get('name', workflow['name'])}")
    if desired_active is not None:
        print(f"Active state requested: {desired_active}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)

