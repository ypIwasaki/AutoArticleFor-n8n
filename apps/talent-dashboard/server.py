#!/usr/bin/env python3
"""Local server for the Talent Index dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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
SOURCE_NOTE_ITEM_PATTERN = re.compile(r"^(?:-\s+|\d+\.\s+)(.+)$")
SOURCE_NOTE_SUMMARY_PATTERN = re.compile(
    r"(?:^|\s)-\s*要約\s*[:：]\s*(.*?)(?=\s+-\s*(?:関連キーワード|重要度|根拠)\s*[:：]|$)"
)
BODY_VERIFIED_PATTERN = re.compile(r"(?:^|\s)-\s*本文確認\s*[:：]\s*確認済み(?:\s|（|\(|$)")
KEYWORD_MUTATION_LOCK = threading.RLock()
ARTICLE_FEEDBACK_MUTATION_LOCK = threading.RLock()


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
        "article_classifications": load_classification_proposals(),
        "article_feedback": [],
    }, "proposal-files"


def load_classification_proposals() -> list[dict[str, Any]]:
    """Load reviewed classification proposals when no Data Table row exists yet."""
    proposal_dir = PROJECT_ROOT / "content" / "article-classification-proposals"
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

def load_classification_taxonomy() -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "article-classification-taxonomy.json"
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
    registry_dir = PROJECT_ROOT / "content" / "official-talent-registry"
    for path in sorted(registry_dir.glob("????-??-??.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("talents"), list):
            return data
    return {"generatedAt": "", "talents": []}


def source_note_items(markdown: str) -> list[str]:
    """Return source-note items written as bullets or numbered Markdown lists."""
    if SOURCE_NOTES_HEADING not in markdown:
        return []

    section = markdown.split(SOURCE_NOTES_HEADING, 1)[1]
    section = re.split(r"^##\s+", section, maxsplit=1, flags=re.MULTILINE)[0]
    items: list[str] = []
    current: str | None = None
    for line in section.splitlines():
        match = SOURCE_NOTE_ITEM_PATTERN.match(line)
        if match:
            if current:
                items.append(current)
            current = match.group(1).strip()
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
            if not BODY_VERIFIED_PATTERN.search(without_links):
                continue
            summary_match = SOURCE_NOTE_SUMMARY_PATTERN.search(without_links)
            if not summary_match:
                continue
            entry = {
                "text": re.sub(r"\s+", " ", summary_match.group(1)).strip(),
                "summary_date": path.stem,
                "source_titles": [title for title, _ in links],
            }
            for _, url in links:
                summaries[url.strip()] = entry

    return summaries


def load_article_capture_metadata() -> dict[str, dict[str, str]]:
    """Map saved article URLs to resolved source details captured during review."""
    path = PROJECT_ROOT / "content" / "article-body-captures" / "backfill-state.json"
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


def canonical_article_title(value: Any) -> str:
    title = re.sub(r"\s+(?:-|｜|–|—)\s+\S.*$", "", str(value or "").strip())
    return re.sub(r"\s+", " ", title).casefold().strip()


def article_publisher_label(article: dict[str, Any]) -> str:
    source = str(article.get("source") or "").strip()
    if source:
        return re.sub(r"\s+", " ", source).casefold()
    title = str(article.get("title") or "").strip()
    match = re.search(r"\s+(?:-|｜|–|—)\s+(.+)$", title)
    return re.sub(r"\s+", " ", match.group(1)).casefold().strip() if match else ""


def source_domain_for_article(article: dict[str, Any], capture_metadata: dict[str, dict[str, str]]) -> str:
    url = str(article.get("url") or "").strip()
    capture = capture_metadata.get(url, {})
    host = str(capture.get("source_host") or "").strip()
    if not host:
        candidate = str(capture.get("resolved_url") or url).strip()
        try:
            host = urlparse(candidate).hostname or ""
        except ValueError:
            host = ""
    host = host.casefold().removeprefix("www.")
    return "" if host in {"", "news.google.com", "b.hatena.ne.jp"} else host


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


def load_registered_talent_keywords() -> tuple[list[str], str, str | None]:
    try:
        payload, source = load_from_n8n()
        source_error: str | None = None
    except Exception as exc:  # Keep keyword visibility available without n8n.
        payload, source = load_from_proposals()
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
    talent_keywords, talent_keyword_source, talent_keyword_error = load_registered_talent_keywords()
    current_by_identity: dict[str, str] = {}
    for term in [*manual, *automatic, *talent_keywords]:
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
        "talentKeywords": talent_keywords,
        "talentKeywordSource": talent_keyword_source,
        "talentKeywordError": talent_keyword_error,
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
    talent_keywords, talent_keyword_source, talent_keyword_error = load_registered_talent_keywords()
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


ARTICLE_FEEDBACK_REASONS = {
    "suspicious_source",
    "irrelevant",
    "unavailable",
    "outdated",
}
ARTICLE_FEEDBACK_DECISIONS = {"approved", "rejected"}


def feedback_is_rejected(value: Any) -> bool:
    return value is True or value == 1 or str(value or "").strip().casefold() in {"1", "true", "yes"}


FEEDBACK_REASON_LABELS = {
    "suspicious_source": "信頼できない情報源",
    "irrelevant": "調査対象と無関係",
    "unavailable": "ページ削除・取得不能",
    "outdated": "情報が古すぎる",
}
FEEDBACK_REASON_INSTRUCTIONS = {
    "suspicious_source": "同じ配信元・媒体名の新規記事を根拠として採用しない。信頼できる一次情報または別媒体で確認する。",
    "irrelevant": "タイトルだけで採用せず、対象タレント・組織・企画との明確な関連を本文で確認する。",
    "unavailable": "該当 URL は根拠に使わない。ページが利用できないことを記録し、代替の一次情報を探す。",
    "outdated": "該当 URL は現在の状況の根拠に使わない。公開日・更新日を確認し、より新しい一次情報または報道へ置き換える。",
}


def markdown_text(value: Any) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).replace("[", "\\[").replace("]", "\\]").strip()


def markdown_url(value: Any) -> str:
    return str(value or "").strip().replace(")", "%29")


def feedback_instruction_timestamp() -> datetime:
    return datetime.now(timezone(timedelta(hours=9), "JST"))


def build_article_feedback_instruction_markdown(
    payload: dict[str, Any],
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or feedback_instruction_timestamp()
    articles_by_key = {
        str(article.get("article_key") or "").strip(): article
        for article in payload.get("articles", [])
        if str(article.get("article_key") or "").strip()
    }
    feedback_rows = [
        feedback
        for feedback in payload.get("article_feedback", [])
        if str(feedback.get("article_key") or "").strip()
    ]
    feedback_rows.sort(
        key=lambda feedback: str(feedback.get("reviewed_at") or ""),
        reverse=True,
    )

    approved_count = sum(
        1 for feedback in feedback_rows if not feedback_is_rejected(feedback.get("is_rejected"))
    )
    rejected_by_reason: dict[str, list[dict[str, Any]]] = {
        reason: [] for reason in ARTICLE_FEEDBACK_REASONS
    }
    for feedback in feedback_rows:
        if not feedback_is_rejected(feedback.get("is_rejected")):
            continue
        reason = str(feedback.get("reason_code") or "").strip()
        if reason in rejected_by_reason:
            rejected_by_reason[reason].append(feedback)

    lines = [
        f"# 記事評価フィードバック指示書 - {generated_at.date().isoformat()}",
        "",
        f"- 更新日時: {generated_at.isoformat(timespec='seconds')}",
        "- 入力: n8n Data Table article_feedback",
        "- 用途: 記事の収集、本文確認、要約、分類を行うAIが、利用者の評価を次回以降の判断に反映するための補助指示書。",
        "",
        "## AIへの共通指示",
        "",
        "1. 可と判定された記事を根拠に使う場合も、本文・公開日・対象との関連を確認する。",
        "2. 不可と判定された記事は、以下の理由別ルールに従う。理由のない一般化や、未記載の媒体・記事への拡大適用はしない。",
        "3. ページ削除・取得不能 と 情報が古すぎる は、原則として該当URLだけを除外する。媒体全体を除外してはならない。",
        "4. 信頼できない情報源 は、記載された媒体・ドメインを根拠に使わず、代替の一次情報または別媒体を確認する。",
        "",
        "## 評価集計",
        "",
        f"- 可: {approved_count}件",
        f"- 不可: {sum(len(rows) for rows in rejected_by_reason.values())}件",
    ]
    for reason in ("suspicious_source", "irrelevant", "unavailable", "outdated"):
        lines.append(f"- 不可 / {FEEDBACK_REASON_LABELS[reason]}: {len(rejected_by_reason[reason])}件")

    lines.extend(["", "## 理由別の判断ルール"])
    for reason in ("suspicious_source", "irrelevant", "unavailable", "outdated"):
        lines.extend([
            "",
            f"### {FEEDBACK_REASON_LABELS[reason]}",
            "",
            FEEDBACK_REASON_INSTRUCTIONS[reason],
        ])
        examples = rejected_by_reason[reason][:10]
        if not examples:
            lines.append("")
            lines.append("- 該当する評価済み記事はありません。")
            continue

        lines.extend(["", "評価済みの代表記事:"])
        for feedback in examples:
            article_key = str(feedback.get("article_key") or "").strip()
            article = articles_by_key.get(article_key, {})
            title = markdown_text(article.get("title") or article_key or "記事タイトルなし")
            url = markdown_url(article.get("url") or feedback.get("article_url"))
            reviewed_at = str(feedback.get("reviewed_at") or "-")
            source_hint = str(feedback.get("source_domain") or feedback.get("publisher_label") or "").strip()
            article_link = f"[{title}]({url})" if url else title
            lines.append(f"- {article_link}")
            lines.append(f"  - 評価日時: {reviewed_at}")
            if source_hint:
                lines.append(f"  - 媒体・ドメイン: {source_hint}")

    if not feedback_rows:
        lines.extend([
            "",
            "## 評価済み記事",
            "",
            "- まだ評価はありません。通常の収集・本文確認・要約方針に従ってください。",
        ])

    return '\n'.join(lines) + '\n'


def write_article_feedback_instruction(
    payload: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> Path:
    if payload is None:
        payload, _ = load_from_n8n()
    generated_at = generated_at or feedback_instruction_timestamp()
    output_dir = PROJECT_ROOT / "content" / "article-feedback-instructions"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{generated_at.date().isoformat()}.md"
    temporary_path = output_path.with_suffix(".md.tmp")
    temporary_path.write_text(
        build_article_feedback_instruction_markdown(payload, generated_at),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return output_path


def call_article_feedback_webhook(feedback: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(feedback, ensure_ascii=False).encode("utf-8")
    webhook_url = os.environ.get(
        "N8N_ARTICLE_FEEDBACK_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/article-feedback/reject",
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
    if not isinstance(result, dict) or not result.get("accepted"):
        reason = result.get("reason") if isinstance(result, dict) else ""
        raise RuntimeError(str(reason or "n8n did not accept the article feedback"))
    return result


def evaluate_article(payload: dict[str, Any]) -> dict[str, Any]:
    article_key = str(payload.get("articleKey") or "").strip()
    decision = str(payload.get("decision") or "").strip().casefold()
    reason_code = str(payload.get("reasonCode") or "").strip().casefold()
    if not article_key:
        raise ValueError("articleKey is required")
    if decision not in ARTICLE_FEEDBACK_DECISIONS:
        raise ValueError("decision must be approved or rejected")
    if decision == "rejected" and reason_code not in ARTICLE_FEEDBACK_REASONS:
        raise ValueError("reasonCode must be suspicious_source, irrelevant, unavailable, or outdated")

    with ARTICLE_FEEDBACK_MUTATION_LOCK:
        try:
            data, _ = load_from_n8n()
        except Exception as exc:
            raise RuntimeError(f"n8n Data Tables are unavailable: {exc}") from exc
        article = next(
            (item for item in data.get("articles", []) if str(item.get("article_key") or "") == article_key),
            None,
        )
        if article is None:
            raise ValueError("The article no longer exists in the current Data Table")

        capture_metadata = load_article_capture_metadata()
        feedback = {
            "articleKey": article_key,
            "articleUrl": str(article.get("url") or "").strip(),
            "decision": decision,
            "reasonCode": reason_code if decision == "rejected" else "approved",
            "sourceDomain": source_domain_for_article(article, capture_metadata)
            if decision == "rejected" and reason_code == "suspicious_source"
            else "",
            "publisherLabel": article_publisher_label(article)
            if decision == "rejected" and reason_code == "suspicious_source"
            else "",
            "titleSignature": canonical_article_title(article.get("title"))
            if decision == "rejected" and reason_code == "irrelevant"
            else "",
            "source": "talent-dashboard",
        }
        result = call_article_feedback_webhook(feedback)
        instruction_path = write_article_feedback_instruction()
        result["feedbackInstructionFile"] = str(instruction_path.relative_to(PROJECT_ROOT))
        return result


def build_dashboard() -> dict[str, Any]:
    error: str | None = None
    try:
        payload, source = load_from_n8n()
    except Exception as exc:  # Fallback keeps the dashboard usable without n8n.
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
