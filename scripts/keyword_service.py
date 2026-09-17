"""Keyword management shared independently of the dashboard HTTP server.

Project files and the n8n runtime database are explicit dependencies. Manual
changes affect config only; automatic changes go through the existing webhooks.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
from urllib import error, request

import talent_dashboard_data as dashboard_data


# Server requests use separate service instances but must serialize mutations.
_MUTATION_LOCK = threading.RLock()


def keyword_identity(value: Any) -> str:
    """Match keywords while preserving the spelling saved by the user."""
    return " ".join(str(value or "").strip().replace("！", "!").split()).casefold()


def validate_keyword(value: Any) -> str:
    keyword = str(value or "").strip()
    if len(keyword) < 2 or len(keyword) > 30:
        raise ValueError("Keyword must contain 2 to 30 characters")
    if re.search(r"[|/\\\r\n]", keyword) or re.match(r"^https?:", keyword, flags=re.IGNORECASE):
        raise ValueError("Keyword contains unsupported characters")
    return keyword


def _find_keyword_index(keywords: list[str], target: str) -> int | None:
    target_identity = keyword_identity(target)
    for index, keyword in enumerate(keywords):
        if keyword_identity(keyword) == target_identity:
            return index
    return None


def _post_keyword_webhook(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send the existing JSON contract and retain its HTTP error semantics."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    webhook_request = request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
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


def call_keyword_management_webhook(
    operation: str, keyword: str, previous_keyword: str
) -> dict[str, Any]:
    webhook_url = os.environ.get(
        "N8N_KEYWORD_MANAGEMENT_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/keyword-management/update",
    )
    result = _post_keyword_webhook(webhook_url, {
        "operation": operation,
        "keyword": keyword,
        "previousKeyword": previous_keyword,
        "source": "talent-dashboard",
    })
    if result.get("status") == "rejected":
        raise ValueError(str(result.get("reason") or "n8n rejected the keyword"))
    return result


class KeywordService:
    def __init__(self, project_root: Path, runtime_database: Path):
        self.project_root = Path(project_root)
        self.runtime_database = Path(runtime_database)

    def load_config(self) -> dict[str, Any]:
        path = self.config_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config/keywords.json must contain an object")
        return data

    def load_automatic_keywords(self) -> tuple[list[str], str | None]:
        path = self.runtime_database
        if not path.exists():
            return [], f"n8n database was not found: {path}"

        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                row = connection.execute(
                    "SELECT staticData FROM workflow_entity WHERE name = ?",
                    ("Daily Keyword News Summary",),
                ).fetchone()
            if row is None:
                raise RuntimeError("Daily Keyword News Summary workflow was not found")
            static_data = json.loads(row[0] or "{}")
            global_data = static_data.get("global", {}) if isinstance(static_data, dict) else {}
            keywords = global_data.get("autoKeywords", []) if isinstance(global_data, dict) else []
            return [str(term).strip() for term in keywords if str(term).strip()], None
        except (OSError, sqlite3.Error, json.JSONDecodeError, RuntimeError) as exc:
            return [], str(exc)

    def load_registered_keywords(self) -> tuple[list[str], str, str | None]:
        try:
            payload, source = dashboard_data.load_dashboard_records(
                self.project_root, legacy_database=self.runtime_database
            )
            source_error: str | None = None
        except Exception as exc:  # Keep keyword visibility available without n8n.
            payload, source = dashboard_data.load_from_proposals(self.project_root)
            source_error = str(exc)

        keywords: dict[str, str] = {}
        for row in payload.get("talents", []):
            if str(row.get("status", "")).strip().casefold() == "rejected":
                continue
            keyword = str(row.get("display_name", "")).strip()
            if (
                len(keyword) < 1
                or len(keyword) > 80
                or re.search(r"[|/\\\r\n]", keyword)
                or re.match(r"^https?:", keyword, flags=re.IGNORECASE)
            ):
                continue
            keywords.setdefault(keyword_identity(keyword), keyword)

        return sorted(keywords.values(), key=lambda item: (item.casefold(), item)), source, source_error

    def load_latest_candidates(self) -> tuple[str, list[dict[str, Any]]]:
        candidate_dir = self.project_root / "content" / "ai-keyword-candidates"
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

    def candidate_payload(self) -> dict[str, Any]:
        candidate_date, candidates = self.load_latest_candidates()
        config = self.load_config()
        manual = [str(term).strip() for term in config.get("manualKeywords", []) if str(term).strip()]
        automatic, runtime_error = self.load_automatic_keywords()
        talent_keywords, talent_keyword_source, talent_keyword_error = self.load_registered_keywords()
        current_by_identity: dict[str, str] = {}
        for term in [*manual, *automatic, *talent_keywords]:
            current_by_identity.setdefault(keyword_identity(term), term)

        for candidate in candidates:
            existing = current_by_identity.get(keyword_identity(candidate["keyword"]))
            if existing:
                candidate["state"] = "added"
            elif candidate["recommended"]:
                candidate["state"] = "eligible"
            else:
                candidate["state"] = "not_recommended"
            candidate["existingKeyword"] = existing or ""

        return {
            "candidateDate": candidate_date,
            "candidates": candidates,
            "manualKeywords": manual,
            "automaticKeywords": automatic,
            "talentKeywords": talent_keywords,
            "talentKeywordSource": talent_keyword_source,
            "talentKeywordError": talent_keyword_error,
            "currentKeywordCount": len(current_by_identity),
            "runtimeError": runtime_error,
        }

    def config_path(self) -> Path:
        return self.project_root / "config" / "keywords.json"

    def save_config(self, config: dict[str, Any]) -> None:
        path = self.config_path()
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(path)

    def management_payload(self) -> dict[str, Any]:
        config = self.load_config()
        manual = [str(term).strip() for term in config.get("manualKeywords", []) if str(term).strip()]
        automatic, runtime_error = self.load_automatic_keywords()
        talent_keywords, talent_keyword_source, talent_keyword_error = self.load_registered_keywords()
        return {
            "manualKeywords": manual,
            "automaticKeywords": automatic,
            "talentKeywords": talent_keywords,
            "talentKeywordSource": talent_keyword_source,
            "talentKeywordError": talent_keyword_error,
            "excludedKeywords": [str(term).strip() for term in config.get("excludedKeywords", []) if str(term).strip()],
            "maxAutoKeywords": max(0, int(config.get("maxAutoKeywords", 30) or 0)),
            "runtimeError": runtime_error,
        }

    def manage_manual_keyword(
        self, operation: str, keyword: str, previous_keyword: str
    ) -> dict[str, Any]:
        with _MUTATION_LOCK:
            config = self.load_config()
            manual_keywords = [
                str(term).strip()
                for term in config.get("manualKeywords", [])
                if str(term).strip()
            ]
            existing_index = _find_keyword_index(
                manual_keywords, previous_keyword or keyword
            )

            if operation == "add":
                keyword = validate_keyword(keyword)
                duplicate_index = _find_keyword_index(manual_keywords, keyword)
                if duplicate_index is not None:
                    return {
                        "status": "already_added",
                        "keyword": manual_keywords[duplicate_index],
                    }
                manual_keywords.append(keyword)
                status = "added"
            elif operation == "edit":
                keyword = validate_keyword(keyword)
                if existing_index is None:
                    raise ValueError("The manual keyword no longer exists")
                existing_keyword = manual_keywords[existing_index]
                if keyword_identity(existing_keyword) == keyword_identity(keyword):
                    return {"status": "unchanged", "keyword": existing_keyword}
                if _find_keyword_index(manual_keywords, keyword) is not None:
                    raise ValueError("The keyword already exists in the manual list")
                manual_keywords[existing_index] = keyword
                status = "updated"
            elif operation == "remove":
                if existing_index is None:
                    return {
                        "status": "already_removed",
                        "keyword": previous_keyword or keyword,
                    }
                if len(manual_keywords) <= 1:
                    raise ValueError("At least one manual keyword must remain")
                keyword = manual_keywords.pop(existing_index)
                status = "removed"
            else:
                raise ValueError("Unsupported keyword operation")

            config["manualKeywords"] = manual_keywords
            self.save_config(config)
            return {"status": status, "keyword": keyword}

    def manage_keyword(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            return self.manage_manual_keyword(operation, keyword, previous_keyword)
        with _MUTATION_LOCK:
            return call_keyword_management_webhook(operation, keyword, previous_keyword)

    def add_candidate(self, keyword: str) -> dict[str, Any]:
        payload = self.candidate_payload()
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

        webhook_url = os.environ.get(
            "N8N_KEYWORD_CANDIDATE_WEBHOOK_URL",
            "http://127.0.0.1:5678/webhook/keyword-candidate/add",
        )
        return _post_keyword_webhook(webhook_url, {
            "keyword": candidate["keyword"],
            "candidateDate": payload["candidateDate"],
            "source": "talent-dashboard",
        })
