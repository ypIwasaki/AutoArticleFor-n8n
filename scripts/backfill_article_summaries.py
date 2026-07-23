#!/usr/bin/env python3
"""Rebuild saved article summaries from the linked article bodies.

The project normally creates an AI instruction file for each daily run.  This
tool is for historical data: it resolves Google News intermediary URLs, fetches
the publisher page, extracts a short factual digest from its text, and rebuilds
the per-day Markdown files.  It never creates a summary from an RSS title or
excerpt alone.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib import error, request
from urllib.parse import urlencode, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = Path(os.environ.get("N8N_DATABASE_PATH", "~/.n8n/database.sqlite")).expanduser()
STATE_PATH = PROJECT_ROOT / "content" / "article-body-captures" / "backfill-state.json"
SUMMARY_DIRECTORY = PROJECT_ROOT / "content" / "article-summaries"
RECORD_DIRECTORY = PROJECT_ROOT / "content" / "structured-records"
KEYWORD_CONFIG_PATH = PROJECT_ROOT / "config" / "keywords.json"
TABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
GOOGLE_NEWS_HOST = "news.google.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "li",
    "blockquote",
    "figcaption",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "header", "aside"}
NOISE_PHRASES = (
    "cookie",
    "プライバシー",
    "個人情報保護",
    "広告",
    "関連記事",
    "この記事をシェア",
    "続きを読む",
    "会員登録",
    "ログイン",
    "メニュー",
    "ランキング",
)
VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "tiktok.com", "www.tiktok.com"}
FEED_NON_ARTICLE_SOURCES = (" - t.co", " - YouTube", " - Mshale")
SHORTLINK_OR_SOCIAL_HOSTS = {"t.co", "x.com", "www.x.com", "twitter.com", "www.twitter.com"}


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    excerpt: str
    source: str
    published_at: str
    last_seen_at: str
    run_date: str


class CaptureError(RuntimeError):
    """A fetch failure which should be retained as an unverified result."""


class TextBlockParser(HTMLParser):
    """Small dependency-free HTML text extractor for publisher article pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.skip_depth = 0
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "meta":
            key = (attributes.get("name") or attributes.get("property") or "").casefold()
            if key in {"description", "og:description", "twitter:description", "og:title"}:
                self.meta[key] = attributes.get("content", "").strip()
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if not self.skip_depth and tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.current.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        value = clean_text(" ".join(self.current))
        self.current = []
        if value:
            self.blocks.append(value)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def comparable_text(value: str) -> str:
    """Remove presentation-only characters when comparing page headings."""
    return re.sub(r"[\s\-‐‑–—―:：;；,，、。.!！?？'\"“”‘’()（）［］【】]", "", clean_text(value)).casefold()


def markdown_text(value: str) -> str:
    return clean_text(value).replace("[", "［").replace("]", "］")


def markdown_url(value: str) -> str:
    return value.replace(")", "%29")


def without_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def quoted_table_name(table_id: str) -> str:
    if not TABLE_ID_PATTERN.fullmatch(table_id):
        raise ValueError("Invalid n8n data table identifier")
    return f'"data_table_user_{table_id}"'


def database_rows(database_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT id FROM data_table WHERE name = 'articles'").fetchone()
        if row is None:
            raise RuntimeError("n8n Data Table 'articles' was not found")
        table = quoted_table_name(row["id"])
        return [dict(item) for item in connection.execute(f"SELECT * FROM {table}")]
    finally:
        connection.close()


def structured_run_dates() -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for path in sorted(RECORD_DIRECTORY.glob("????-??-??.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("recordType") != "article":
                continue
            url = str(record.get("article", {}).get("url", "")).strip()
            run_date = str(record.get("runDate", "")).strip()
            if url and run_date:
                result[url].add(run_date)
    return result


def closest_run_date(last_seen_at: str, available_dates: list[str]) -> str:
    if not available_dates:
        return datetime.now(timezone.utc).date().isoformat()
    date = str(last_seen_at or "")[:10]
    later = [candidate for candidate in available_dates if candidate >= date]
    return later[0] if later else available_dates[-1]


def load_articles(database_path: Path) -> list[Article]:
    runs_by_url = structured_run_dates()
    available_dates = sorted({date for dates in runs_by_url.values() for date in dates})
    articles: dict[str, Article] = {}
    for row in database_rows(database_path):
        url = str(row.get("url", "")).strip()
        if not url:
            continue
        run_dates = sorted(runs_by_url.get(url, ()))
        run_date = run_dates[-1] if run_dates else closest_run_date(str(row.get("last_seen_at", "")), available_dates)
        articles[url] = Article(
            url=url,
            title=str(row.get("title", "")).strip(),
            excerpt=str(row.get("excerpt", "")).strip(),
            source=str(row.get("source", "")).strip(),
            published_at=str(row.get("published_at", "")).strip(),
            last_seen_at=str(row.get("last_seen_at", "")).strip(),
            run_date=run_date,
        )
    return sorted(articles.values(), key=lambda item: (item.run_date, item.published_at, item.url), reverse=True)


def load_keywords() -> list[str]:
    try:
        data = json.loads(KEYWORD_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    values = data.get("manualKeywords", data.get("keywords", [])) if isinstance(data, dict) else data
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
        elif isinstance(value, dict) and str(value.get("keyword", "")).strip():
            result.append(str(value["keyword"]).strip())
    return result


def article_id_from_google_url(url: str) -> str:
    article_id = urlparse(url).path.rstrip("/").split("/")[-1]
    if not article_id:
        raise CaptureError("Google News URLから記事IDを取得できませんでした")
    return article_id


def http_bytes(url: str, *, data: bytes | None = None, content_type: str | None = None) -> tuple[bytes, str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request_object = request.Request(url, data=data, headers=headers)
    for attempt in range(2):
        try:
            with request.urlopen(request_object, timeout=15) as response:
                content_type_value = response.headers.get("Content-Type", "")
                return response.read(2_500_000), response.geturl(), content_type_value
        except error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < 1:
                time.sleep(1)
                continue
            raise CaptureError(f"HTTP {exc.code}") from exc
        except error.URLError as exc:
            if attempt < 1:
                time.sleep(1)
                continue
            raise CaptureError(f"接続失敗: {exc.reason}") from exc
    raise CaptureError("接続失敗")


def google_decoding_params(article_id: str) -> tuple[str, str]:
    body, _, _ = http_bytes(f"https://news.google.com/articles/{article_id}")
    page = body.decode("utf-8", "replace")
    signature_match = re.search(r'data-n-a-sg="([^"]+)"', page)
    timestamp_match = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not signature_match or not timestamp_match:
        raise CaptureError("Google Newsの配信元URL復元パラメータを取得できませんでした")
    return signature_match.group(1), timestamp_match.group(1)


def resolve_google_url(url: str) -> str:
    article_id = article_id_from_google_url(url)
    signature, timestamp = google_decoding_params(article_id)
    inner_request = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
        'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{article_id}",{timestamp},"{signature}"]'
    )
    payload = json.dumps([[["Fbv4je", inner_request]]], separators=(",", ":"))
    data = urlencode({"f.req": payload}).encode("utf-8")
    response, _, _ = http_bytes(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je",
        data=data,
        content_type="application/x-www-form-urlencoded;charset=UTF-8",
    )
    chunks = response.decode("utf-8", "replace").split("\n\n")
    if len(chunks) < 2:
        raise CaptureError("Google Newsの配信元URL復元応答を解析できませんでした")
    try:
        rows = json.loads(chunks[1])
        payload_value = next(row[2] for row in rows if len(row) > 2 and row[1] == "Fbv4je")
        final_url = json.loads(payload_value)[1]
    except (IndexError, KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise CaptureError("Google Newsの配信元URL復元結果を解析できませんでした") from exc
    if not isinstance(final_url, str) or not final_url.startswith(("http://", "https://")):
        raise CaptureError("Google Newsの配信元URLが不正です")
    return final_url


def meaningful_blocks(page: str, article_title: str) -> tuple[list[str], str]:
    parser = TextBlockParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception as exc:
        raise CaptureError("記事ページの本文を解析できませんでした") from exc

    title_key = clean_text(article_title).casefold()
    headline = re.split(r"\s[-－]\s", article_title, maxsplit=1)[0]
    compact_title_key = comparable_text(headline)
    blocks: list[str] = []
    seen: set[str] = set()
    for raw in parser.blocks:
        block = clean_text(raw)
        key = block.casefold()
        if len(block) < 35 or len(block) > 1_200 or key in seen:
            continue
        compact_block_key = comparable_text(block)
        if title_key and (key == title_key or key.startswith(title_key)):
            continue
        if compact_title_key and (
            compact_block_key == compact_title_key
            or compact_block_key.startswith(compact_title_key)
            or (len(compact_block_key) >= 45 and compact_block_key in compact_title_key)
        ):
            continue
        if any(phrase.casefold() in key for phrase in NOISE_PHRASES):
            continue
        if len(re.findall(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]", block)) < 25:
            continue
        seen.add(key)
        blocks.append(block)
    return blocks, clean_text(parser.meta.get("description") or parser.meta.get("og:description", ""))


def split_sentences(value: str) -> list[str]:
    parts = re.split(r"(?<=[。！？])\s*|(?<=[.!?])(?=\s|$)", clean_text(value))
    result: list[str] = []
    for part in parts:
        part = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日\([^)]*\)\s*\d{1,2}:\d{2}\s*", "", part.strip())
        if len(part) >= 25:
            result.append(part)
    return result


def extract_summary(blocks: Iterable[str], keywords: Iterable[str] = ()) -> str:
    candidates: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        for sentence in split_sentences(block):
            key = sentence.casefold()
            if key in seen:
                continue
            seen.add(key)
            if len(sentence) > 240:
                sentence = sentence[:237].rstrip("、。 ") + "。"
            candidates.append(sentence)

    if not candidates:
        return ""
    matched = [
        index
        for index, sentence in enumerate(candidates)
        if any(keyword.casefold() in sentence.casefold() for keyword in keywords)
    ]
    selected_indexes = {0}
    selected_indexes.update(matched[:2])
    for index in range(len(candidates)):
        if len(selected_indexes) >= 3:
            break
        selected_indexes.add(index)
    return " ".join(candidates[index] for index in sorted(selected_indexes)[:3])


def matched_keywords(article: Article, keywords: Iterable[str], body_text: str = "") -> list[str]:
    haystack = f"{article.title}\n{article.excerpt}\n{body_text}".casefold()
    return [keyword for keyword in keywords if keyword.casefold() in haystack]


def capture_article(article: Article, keywords: list[str]) -> dict[str, Any]:
    if article.title.rstrip().endswith(FEED_NON_ARTICLE_SOURCES):
        return capture_result(article, "unverified", None, "配信元が動画・SNS・短縮URLのため記事本文を確認できませんでした")
    original_host = urlparse(article.url).netloc.casefold()
    try:
        resolved_url = resolve_google_url(article.url) if original_host == GOOGLE_NEWS_HOST else article.url
        host = without_www(urlparse(resolved_url).netloc.casefold())
        if host in VIDEO_HOSTS:
            return capture_result(article, "unverified", resolved_url, "動画ページのため本文を確認できませんでした")
        if host in SHORTLINK_OR_SOCIAL_HOSTS:
            return capture_result(article, "unverified", resolved_url, "SNSまたは短縮URLのため記事本文を確認できませんでした")

        body, final_url, content_type = http_bytes(resolved_url)
        if "html" not in content_type.casefold():
            return capture_result(article, "unverified", final_url, "HTML記事ページではありませんでした")
        page = body.decode("utf-8", "replace")
        blocks, description = meaningful_blocks(page, article.title)
        summary = extract_summary(blocks, keywords)
        body_text = "\n".join(blocks)
        if len(body_text) < 300:
            return capture_result(article, "unverified", final_url, "本文として十分なテキストを取得できませんでした", body_length=len(body_text))
        if len(split_sentences(summary)) < 2 or len(summary) < 90:
            return capture_result(article, "unverified", final_url, "本文から要点を複数文で抽出できませんでした", body_length=len(body_text))
        return {
            **capture_result(article, "verified", final_url, None, body_length=len(body_text)),
            "summary": summary,
            "description": description,
            "content_hash": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
            "source_host": without_www(urlparse(final_url).netloc.casefold()),
            "matched_keywords": matched_keywords(article, keywords, body_text),
        }
    except CaptureError as exc:
        return capture_result(article, "unverified", None, str(exc))


def capture_result(
    article: Article,
    status: str,
    resolved_url: str | None,
    reason: str | None,
    *,
    body_length: int = 0,
) -> dict[str, Any]:
    return {
        "original_url": article.url,
        "run_date": article.run_date,
        "title": article.title,
        "published_at": article.published_at,
        "status": status,
        "resolved_url": resolved_url,
        "reason": reason,
        "body_length": body_length,
        "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def load_state() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    return entries if isinstance(entries, dict) else {}


def save_state(entries: dict[str, dict[str, Any]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "entries": entries,
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_note(article: Article, entry: dict[str, Any], index: int) -> str:
    title = markdown_text(article.title or "無題の記事")
    lines = [f"{index}. [{title}]({markdown_url(article.url)})"]
    if entry.get("status") == "verified":
        source_host = markdown_text(str(entry.get("source_host", "不明")))
        lines.append(f"   - 本文確認: 確認済み（確認元: {source_host}、本文要点を自動抽出）")
        lines.append(f"   - 要約: {markdown_text(str(entry.get('summary', '')))}")
        keywords = entry.get("matched_keywords") or ["該当キーワードは本文確認後に要確認"]
        lines.append(f"   - 関連キーワード: {', '.join(markdown_text(str(keyword)) for keyword in keywords)}")
        lines.append("   - 重要度: 中")
        if entry.get("resolved_url"):
            lines.append(f"   - 根拠: [記事URL]({markdown_url(str(entry['resolved_url']))})")
    else:
        reason = markdown_text(str(entry.get("reason") or "本文を確認できませんでした"))
        lines.append(f"   - 本文確認: 未確認（{reason}）")
        if entry.get("resolved_url"):
            lines.append(f"   - 根拠: [記事URL]({markdown_url(str(entry['resolved_url']))})")
    return "\n".join(lines)


def render_summary(run_date: str, articles: list[Article], entries: dict[str, dict[str, Any]]) -> str:
    verified = [entry for article in articles if (entry := entries.get(article.url, {})).get("status") == "verified"]
    unverified_count = len(articles) - len(verified)
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        "# Daily Article Summary",
        "",
        "> **本文確認状況:** このファイルは保存済み記事を再取得して作成したバックフィルです。要約は確認できた本文からの自動要点抽出であり、タイトル・RSS抜粋だけによる要約は含めません。",
        "",
        "## Executive Summary",
        "",
        f"対象 {len(articles)} 件のうち {len(verified)} 件でリンク先本文を取得し、{unverified_count} 件は本文を確認できませんでした。",
        "本文確認済みの記事は下記の Source-by-source Notes にのみ要約を記載しています。未確認の記事は推測で補わず、理由だけを残しています。",
        "",
        "## Important Topics",
        "",
        "| Topic | Summary | Sources |",
        "| --- | --- | --- |",
        "| 本文確認済み記事 | 個別の要約は Source-by-source Notes を参照。自動抽出のため、公開前の編集レビューを推奨します。 | 本文確認済み記事のみ |",
        "",
        "## Source-by-source Notes",
        "",
    ]
    lines.extend(source_note(article, entries.get(article.url, {}), index) for index, article in enumerate(articles, start=1))
    lines.extend(
        [
            "",
            "## Noise or Low-relevance Items",
            "",
            "- 本文確認済みでも、動画ページ、ゲーム攻略、単発配信告知などは運用上の判断により別途除外してください。",
            "",
            "## Items to Monitor Next",
            "",
            "- 本文未確認になった配信元は、ログイン・ペイウォール・robots制限・動画のみかを確認して再取得を検討してください。",
            "",
            f"<!-- Generated by scripts/backfill_article_summaries.py at {generated}. -->",
            "",
        ]
    )
    return "\n".join(lines)


def write_summaries(articles: list[Article], entries: dict[str, dict[str, Any]]) -> None:
    by_run_date: dict[str, list[Article]] = defaultdict(list)
    for article in articles:
        by_run_date[article.run_date].append(article)
    for run_date, day_articles in sorted(by_run_date.items()):
        day_articles.sort(key=lambda item: (item.published_at, item.url), reverse=True)
        path = SUMMARY_DIRECTORY / f"{run_date}.md"
        path.write_text(render_summary(run_date, day_articles, entries), encoding="utf-8")
        print(f"wrote {path.relative_to(PROJECT_ROOT)} ({len(day_articles)} articles)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch saved article bodies and regenerate verified article summaries.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH, help="Path to n8n database.sqlite")
    parser.add_argument("--limit", type=int, help="Process only this many articles; cannot be used with --write")
    parser.add_argument("--retry-unverified", action="store_true", help="Re-fetch cached unverified articles")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch every article and replace cached results")
    parser.add_argument("--sleep", type=float, default=0.15, help="Pause between articles in seconds (default: 0.15)")
    parser.add_argument("--write", action="store_true", help="Overwrite daily Markdown only after all stored articles have a result")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.limit is not None and args.write:
        parser.error("--limit cannot be combined with --write; partial results must not replace daily summaries")
    if not args.database.expanduser().exists():
        parser.error(f"n8n database was not found: {args.database}")

    articles = load_articles(args.database.expanduser())
    keywords = load_keywords()
    entries = load_state()
    pending = [
        article
        for article in articles
        if args.refresh or article.url not in entries or (args.retry_unverified and entries[article.url].get("status") != "verified")
    ]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(f"articles={len(articles)} cached={len(entries)} pending={len(pending)}")
    for index, article in enumerate(pending, start=1):
        entries[article.url] = capture_article(article, keywords)
        save_state(entries)
        status = entries[article.url].get("status", "unverified")
        print(f"[{index}/{len(pending)}] {status}: {article.title[:90]}")
        if args.sleep > 0 and index < len(pending):
            time.sleep(args.sleep)

    all_results = sum(1 for article in articles if article.url in entries)
    verified = sum(1 for article in articles if entries.get(article.url, {}).get("status") == "verified")
    print(f"complete={all_results}/{len(articles)} verified={verified} unverified={all_results - verified}")
    if args.write:
        if all_results != len(articles):
            print("refusing to write daily summaries because not all articles have a captured result", file=sys.stderr)
            return 2
        write_summaries(articles, entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
