#!/usr/bin/env python3
"""Shared keyword grouping and Markdown review helpers."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEYWORD_ALIASES_PATH = PROJECT_ROOT / "config" / "keyword-aliases.json"
REVIEW_START = "<!-- article-review:start -->"
REVIEW_END = "<!-- article-review:end -->"
REVIEW_VALUES = {
    "decision": {"pending", "approved", "rejected", "needs_refetch"},
    "keyword_check": {"pending", "correct", "incorrect", "uncertain"},
    "content_check": {"pending", "correct", "incorrect", "uncertain"},
}
REASON_CODES = {"", "irrelevant", "suspicious_source", "unavailable", "outdated"}
KIND_RANK = {"talent": 4, "unit": 3, "organization": 2, "industry": 1, "keyword": 0}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalize_keyword(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_text(value)
        key = normalize_keyword(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def stable_article_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def safe_path_component(value: Any, prefix: str = "item") -> str:
    text = unicodedata.normalize("NFKC", clean_text(value))
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:48]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{ascii_slug}-{digest}" if ascii_slug else f"{prefix}-{digest}"


def markdown_text(value: Any) -> str:
    return clean_text(value).replace("[", "［").replace("]", "］")


def markdown_url(value: Any) -> str:
    return str(value or "").strip().replace(")", "%29")


def source_domain(value: Any) -> str:
    hostname = urlparse(str(value or "")).hostname or ""
    hostname = hostname.casefold()
    return hostname[4:] if hostname.startswith("www.") else hostname


def canonical_title(value: Any) -> str:
    value = re.sub(r"\s+(?:-|｜|–|—)\s+\S.*$", "", clean_text(value))
    return re.sub(r"\s+", " ", value).strip().casefold()


def publisher_label(title: Any, source: Any = "") -> str:
    source_text = clean_text(source)
    if source_text:
        return source_text.casefold()
    match = re.search(r"\s+(?:-|｜|–|—)\s+(.+)$", clean_text(title))
    return clean_text(match.group(1)).casefold() if match else ""


@dataclass(frozen=True)
class KeywordDefinition:
    id: str
    label: str
    aliases: tuple[str, ...]
    kind: str = "keyword"
    priority: int = 0


class KeywordCatalog:
    def __init__(self, definitions: Iterable[KeywordDefinition]) -> None:
        self.definitions = list(definitions)
        self.by_id = {definition.id: definition for definition in self.definitions}
        self.by_alias: dict[str, KeywordDefinition] = {}
        for definition in self.definitions:
            for alias in (definition.label, *definition.aliases):
                self.by_alias[normalize_keyword(alias)] = definition

    @classmethod
    def load(cls, path: Path = KEYWORD_ALIASES_PATH) -> "KeywordCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"keywords": []}
        definitions: list[KeywordDefinition] = []
        for row in payload.get("keywords", []):
            if not isinstance(row, dict) or not clean_text(row.get("id")):
                continue
            label = clean_text(row.get("label") or row["id"])
            definitions.append(
                KeywordDefinition(
                    id=clean_text(row["id"]),
                    label=label,
                    aliases=tuple(unique_strings(row.get("aliases", []))),
                    kind=clean_text(row.get("kind") or "keyword"),
                    priority=int(row.get("priority") or 0),
                )
            )
        return cls(definitions)

    def resolve(self, term: Any, *, kind: str = "keyword") -> KeywordDefinition:
        text = clean_text(term)
        known = self.by_alias.get(normalize_keyword(text))
        if known:
            return known
        identifier = "kw-" + hashlib.sha1(normalize_keyword(text).encode("utf-8")).hexdigest()[:12]
        existing = self.by_id.get(identifier)
        if existing:
            return existing
        definition = KeywordDefinition(identifier, text, (text,), kind, 0)
        self.by_id[identifier] = definition
        self.by_alias[normalize_keyword(text)] = definition
        self.definitions.append(definition)
        return definition


def text_occurrences(text: Any, aliases: Iterable[str]) -> int:
    haystack = normalize_keyword(text)
    return sum(
        haystack.count(normalize_keyword(alias))
        for alias in unique_strings(aliases)
        if normalize_keyword(alias)
    )


def keyword_matches(
    *,
    title: str,
    excerpt: str,
    content_text: str,
    content_markdown: str,
    non_content_text: str,
    search_keywords: Iterable[str],
    rss_keywords: Iterable[str],
    active_keywords: Iterable[str],
    catalog: KeywordCatalog,
    match_method: str = "rss-query",
) -> dict[str, Any]:
    explicit_search = unique_strings(search_keywords)
    explicit_rss = unique_strings(rss_keywords)
    candidates = unique_strings([*explicit_search, *explicit_rss])
    heading_text = "\n".join(
        line.lstrip("# ") for line in content_markdown.splitlines() if line.startswith("#")
    )
    strong_visible = normalize_keyword("\n".join((title, excerpt, heading_text)))
    content_visible = normalize_keyword("\n".join((content_text, content_markdown)))
    for term in active_keywords:
        term_key = normalize_keyword(term)
        if not term_key:
            continue
        if term_key in strong_visible or (len(term_key) >= 3 and term_key in content_visible):
            candidates.append(clean_text(term))
    candidates = unique_strings(candidates)

    grouped_terms: dict[str, list[str]] = {}
    definitions: dict[str, KeywordDefinition] = {}
    for term in candidates:
        definition = catalog.resolve(term)
        definitions[definition.id] = definition
        grouped_terms.setdefault(definition.id, []).append(term)

    rows: list[dict[str, Any]] = []
    search_keys = {normalize_keyword(value) for value in explicit_search}
    rss_keys = {normalize_keyword(value) for value in explicit_rss}
    for identifier, terms in grouped_terms.items():
        definition = definitions[identifier]
        aliases = unique_strings([definition.label, *definition.aliases, *terms])
        search_hit = any(normalize_keyword(alias) in search_keys for alias in aliases)
        rss_hit = any(normalize_keyword(alias) in rss_keys for alias in aliases)
        title_count = text_occurrences(title, aliases)
        excerpt_count = text_occurrences(excerpt, aliases)
        heading_count = text_occurrences(heading_text, aliases)
        body_count = text_occurrences(content_text, aliases)
        non_content_count = text_occurrences(non_content_text, aliases)
        score = 0
        locations: list[str] = []
        if search_hit:
            score += 60
            locations.append("search_query")
        if rss_hit:
            score += 20
            locations.append("rss_title_or_excerpt")
        if title_count:
            score += 120 + min(title_count - 1, 2) * 10
            locations.append("title")
        if excerpt_count:
            score += 35 + min(excerpt_count - 1, 2) * 5
            locations.append("excerpt")
        if heading_count:
            score += 80 + min(heading_count - 1, 2) * 5
            locations.append("heading")
        if body_count:
            score += 40 + min(body_count, 3) * 5
            locations.append("main_content")
        if non_content_count:
            locations.append("non_content")
        rows.append(
            {
                "keyword_id": identifier,
                "label": definition.label,
                "aliases": aliases,
                "kind": definition.kind,
                "priority": definition.priority,
                "score": score,
                "locations": locations,
                "counts": {
                    "title": title_count,
                    "excerpt": excerpt_count,
                    "heading": heading_count,
                    "main_content": body_count,
                    "non_content": non_content_count,
                },
                "search_origin": search_hit,
                "match_method": match_method if search_hit else "literal-content",
            }
        )
    rows.sort(
        key=lambda row: (
            -row["score"],
            -KIND_RANK.get(row["kind"], 0),
            -row["priority"],
            row["label"],
        )
    )
    if not rows:
        return {"matches": [], "primary_keywords": [], "assignment": "unmatched"}
    best = rows[0]
    best_key = (best["score"], KIND_RANK.get(best["kind"], 0), best["priority"])
    primary = [
        row["keyword_id"]
        for row in rows
        if (row["score"], KIND_RANK.get(row["kind"], 0), row["priority"]) == best_key
    ]
    assignment = "tie" if len(primary) > 1 else "multiple" if len(rows) > 1 else "single"
    return {"matches": rows, "primary_keywords": primary, "assignment": assignment}


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_frontmatter(values: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.extend(["---", ""])
    return "\n".join(lines)


def parse_frontmatter(markdown: str) -> dict[str, Any]:
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, Any] = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        line = lines[index]
        match = re.match(r"^([A-Za-z0-9_]+):(?:\s*(.*))?$", line)
        if not match:
            index += 1
            continue
        key, raw = match.group(1), (match.group(2) or "").strip()
        if raw:
            try:
                result[key] = json.loads(raw)
            except json.JSONDecodeError:
                result[key] = raw
        else:
            values: list[Any] = []
            index += 1
            while index < len(lines) and re.match(r"^\s+-\s+", lines[index]):
                item = re.sub(r"^\s+-\s+", "", lines[index]).strip()
                try:
                    values.append(json.loads(item))
                except json.JSONDecodeError:
                    values.append(item)
                index += 1
            result[key] = values
            continue
        index += 1
    return result


def default_review_block() -> str:
    return "\n".join(
        [
            REVIEW_START,
            "~~~yaml",
            "decision: pending",
            "keyword_check: pending",
            "content_check: pending",
            'reason_code: ""',
            'note: ""',
            'reviewed_at: ""',
            "~~~",
            REVIEW_END,
        ]
    )


def extract_review_block(markdown: str) -> str | None:
    start = markdown.find(REVIEW_START)
    end = markdown.find(REVIEW_END)
    if start < 0 or end < start:
        return None
    return markdown[start : end + len(REVIEW_END)]


def parse_review_block(markdown: str) -> tuple[dict[str, str], list[str]]:
    block = extract_review_block(markdown)
    if not block:
        return {}, ["レビュー欄がありません"]
    fence = re.search(r"~~~yaml\s*(.*?)\s*~~~", block, re.DOTALL)
    if not fence:
        return {}, ["レビュー欄のYAMLブロックが壊れています"]
    values: dict[str, str] = {}
    errors: list[str] = []
    for line in fence.group(1).splitlines():
        if not line.strip():
            continue
        match = re.match(r"^([a-z_]+):\s*(.*)$", line.strip())
        if not match:
            errors.append(f"解釈できない行: {line.strip()}")
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw.startswith(('"', "'")):
            try:
                raw = json.loads(raw) if raw.startswith('"') else raw.strip("'")
            except json.JSONDecodeError:
                errors.append(f"{key} の引用符が壊れています")
                continue
        values[key] = str(raw)
    for key, allowed in REVIEW_VALUES.items():
        value = values.get(key, "")
        if value not in allowed:
            errors.append(f"{key} は {', '.join(sorted(allowed))} のいずれかにしてください")
    reason = values.get("reason_code", "")
    if reason not in REASON_CODES:
        errors.append("reason_code が不正です")
    if values.get("decision") == "rejected" and not reason:
        errors.append("rejected には reason_code が必要です")
    return values, errors


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not path.exists():
        return result
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            result.append(value)
    return result
