#!/usr/bin/env python3
"""Create analysis records from existing daily digest Markdown files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_front_matter(text: str) -> dict[str, object]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not match:
        return {}

    metadata: dict[str, object] = {}
    active_list: str | None = None
    for raw_line in match.group(1).splitlines():
        if raw_line.startswith("  - ") and active_list:
            values = metadata.setdefault(active_list, [])
            if isinstance(values, list):
                values.append(unquote(raw_line[4:]))
            continue

        key, separator, value = raw_line.partition(":")
        if not separator:
            active_list = None
            continue
        key = key.strip()
        value = value.strip()
        if not value:
            metadata[key] = []
            active_list = key
            continue
        active_list = None
        if key == "articleCount":
            try:
                metadata[key] = int(value)
            except ValueError:
                metadata[key] = 0
        else:
            metadata[key] = unquote(value)
    return metadata


def parse_articles(text: str) -> list[dict[str, str]]:
    marker = "## Captured Articles"
    if marker not in text:
        return []

    section = text.split(marker, 1)[1]
    if "## LLM Prompt" in section:
        section = section.split("## LLM Prompt", 1)[0]

    articles: list[dict[str, str]] = []
    entries = re.split(r"(?m)^### \d+\.\s+", section)
    for entry in entries[1:]:
        lines = entry.strip().splitlines()
        if not lines:
            continue

        title = lines[0].strip()
        url = ""
        published_at = ""
        source = ""
        excerpt_lines: list[str] = []
        for line in lines[1:]:
            if line.startswith("- URL: "):
                url = line[len("- URL: "):].strip()
            elif line.startswith("- Published: "):
                published_at = line[len("- Published: "):].strip()
            elif line.startswith("- Source: "):
                source = line[len("- Source: "):].strip()
            else:
                excerpt_lines.append(line)

        articles.append(
            {
                "title": title,
                "url": url,
                "publishedAt": published_at,
                "source": source,
                "excerpt": "\n".join(excerpt_lines).strip(),
            }
        )
    return articles


def records_for_digest(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    metadata = parse_front_matter(text)
    articles = parse_articles(text)
    run_date = str(metadata.get("date") or path.stem)
    keywords = metadata.get("keywords") if isinstance(metadata.get("keywords"), list) else []
    sources = metadata.get("sources") if isinstance(metadata.get("sources"), list) else []
    article_count = metadata.get("articleCount")
    if not isinstance(article_count, int):
        article_count = len(articles)

    run_record: dict[str, object] = {
        "schemaVersion": 1,
        "recordType": "run",
        "runDate": run_date,
        "generatedAt": str(metadata.get("generatedAt") or ""),
        "period": {},
        "keywords": keywords,
        "searchSources": sources,
        "articleCount": article_count,
        "capturedArticleCount": len(articles),
        "requestSource": "markdown-backfill",
    }
    article_records = [
        {
            "schemaVersion": 1,
            "recordType": "article",
            "runDate": run_date,
            "generatedAt": run_record["generatedAt"],
            "keywords": keywords,
            "articleIndex": index,
            "article": article,
        }
        for index, article in enumerate(articles, start=1)
    ]
    return [run_record, *article_records]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill JSONL analysis records from saved daily digest Markdown files."
    )
    parser.add_argument(
        "--digest-dir",
        type=Path,
        default=PROJECT_ROOT / "content" / "daily-digests",
        help="Directory containing YYYY-MM-DD.md digest files.",
    )
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=PROJECT_ROOT / "content" / "structured-records",
        help="Directory in which JSONL records are written.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing JSONL files for the same date.",
    )
    args = parser.parse_args()

    digest_paths = sorted(
        path for path in args.digest_dir.glob("????-??-??.md") if path.is_file()
    )
    if not digest_paths:
        raise SystemExit(f"No dated digest files found in {args.digest_dir}")

    args.records_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    skipped = 0
    for digest_path in digest_paths:
        target = args.records_dir / f"{digest_path.stem}.jsonl"
        if target.exists() and not args.force:
            skipped += 1
            continue
        records = records_for_digest(digest_path)
        target.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        created += 1
        print(f"wrote {target.relative_to(PROJECT_ROOT)} ({len(records) - 1} articles)")

    print(f"created={created} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())