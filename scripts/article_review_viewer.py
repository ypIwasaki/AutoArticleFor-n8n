#!/usr/bin/env python3
"""Local web viewer and reviewer for content/article-review Markdown files."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from article_review_common import (
    PROJECT_ROOT,
    REASON_CODES,
    REVIEW_END,
    REVIEW_START,
    REVIEW_VALUES,
    clean_text,
    parse_frontmatter,
    parse_review_block,
)

DEFAULT_REVIEW_ROOT = PROJECT_ROOT / "content" / "article-review"
STATIC_ROOT = PROJECT_ROOT / "article-review-viewer"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
MAX_NOTE_LENGTH = 10_000
MAX_REQUEST_BYTES = 64 * 1024


class ReviewStoreError(ValueError):
    """A review request is invalid."""


class ReviewConflictError(RuntimeError):
    """The Markdown changed after it was opened."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def safe_web_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return url


def youtube_video_id(value: Any) -> str:
    """Return a validated video ID for common YouTube URL forms."""
    url = safe_web_url(value)
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path_parts = [part for part in parsed.path.split("/") if part]
    candidate = ""

    if host == "youtu.be" and path_parts:
        candidate = path_parts[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"embed", "shorts", "live"}:
            candidate = path_parts[1]
    elif host == "youtube-nocookie.com":
        if len(path_parts) >= 2 and path_parts[0] == "embed":
            candidate = path_parts[1]

    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", candidate):
        return ""
    return candidate


def youtube_embed_url(value: Any) -> str:
    video_id = youtube_video_id(value)
    return f"https://www.youtube-nocookie.com/embed/{video_id}?rel=0" if video_id else ""


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def without_frontmatter(markdown: str) -> str:
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return markdown
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return markdown


def without_review_block(markdown: str) -> str:
    start = markdown.find(REVIEW_START)
    if start < 0:
        return markdown.rstrip()
    heading = markdown.rfind("\n## ユーザーレビュー", 0, start)
    return markdown[: heading if heading >= 0 else start].rstrip()


def overview_from_markdown(markdown: str) -> str:
    body = without_review_block(without_frontmatter(markdown))
    match = re.search(r"^## 内容の概要\s*$\n(.*?)(?=^##\s|\Z)", body, re.MULTILINE | re.DOTALL)
    text = match.group(1) if match else body
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_~|\`-]+", " ", text)
    return clean_text(text)[:280]


def markdown_section(markdown: str, heading: str) -> str:
    body = without_review_block(without_frontmatter(markdown))
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def video_metadata_from_markdown(markdown: str, fallback_title: str) -> dict[str, str]:
    extracted = markdown_section(markdown, "抽出本文")
    values: dict[str, str] = {}
    for line in extracted.splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?([^:：]+)[：:]\s*(.+?)\s*$", line)
        if match:
            values[clean_text(match.group(1))] = clean_text(match.group(2))

    overview = markdown_section(markdown, "内容の概要")
    overview = re.split(r"^\s*-\s*キーワード割当", overview, maxsplit=1, flags=re.MULTILINE)[0]
    description = overview.strip()
    return {
        "title": values.get("タイトル") or fallback_title,
        "channel": values.get("投稿者・チャンネル") or values.get("チャンネル") or "",
        "provider": values.get("配信元") or "YouTube",
        "description": description,
    }


class ReviewStore:
    def __init__(self, review_root: Path = DEFAULT_REVIEW_ROOT) -> None:
        self.review_root = review_root.resolve()
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._keyword_cache: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def dates(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not self.review_root.exists():
            return result
        for path in sorted(self.review_root.glob("????-??-??"), reverse=True):
            if not path.is_dir() or not DATE_PATTERN.fullmatch(path.name):
                continue
            article_dir = path / "articles"
            result.append(
                {
                    "date": path.name,
                    "article_count": sum(1 for _ in article_dir.glob("*.md"))
                    if article_dir.exists()
                    else 0,
                }
            )
        return result

    def _date_dir(self, run_date: str) -> Path:
        if not DATE_PATTERN.fullmatch(run_date):
            raise ReviewStoreError("日付の形式が不正です")
        path = (self.review_root / run_date).resolve()
        if path.parent != self.review_root or not path.is_dir():
            raise ReviewStoreError("指定されたレビュー日がありません")
        return path

    def _article_path(self, run_date: str, article_key: str) -> Path:
        date_dir = self._date_dir(run_date)
        if not KEY_PATTERN.fullmatch(article_key):
            raise ReviewStoreError("記事キーが不正です")
        article_dir = (date_dir / "articles").resolve()
        path = (article_dir / f"{article_key}.md").resolve()
        if path.parent != article_dir or not path.is_file():
            raise ReviewStoreError("指定された記事がありません")
        return path

    def keyword_labels(self, run_date: str) -> dict[str, str]:
        if run_date in self._keyword_cache:
            return self._keyword_cache[run_date]
        date_dir = self._date_dir(run_date)
        labels: dict[str, str] = {}
        keyword_root = date_dir / "by-keyword"
        if not keyword_root.exists():
            self._keyword_cache[run_date] = labels
            return labels
        for folder in keyword_root.iterdir():
            if not folder.is_dir():
                continue
            label = folder.name
            index_path = folder / "index.md"
            try:
                first_line = index_path.read_text(encoding="utf-8").splitlines()[0]
                match = re.match(r"^# キーワード:\s*(.+)$", first_line)
                if match:
                    label = clean_text(match.group(1))
            except (OSError, IndexError):
                pass
            labels[folder.name] = label
        self._keyword_cache[run_date] = labels
        return labels

    def _summary(self, path: Path, labels: dict[str, str]) -> dict[str, Any]:
        markdown = path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(markdown)
        review, review_errors = parse_review_block(markdown)
        matched_ids = frontmatter.get("matched_keyword_ids", [])
        primary_ids = frontmatter.get("primary_keyword_ids", [])
        if not isinstance(matched_ids, list):
            matched_ids = []
        if not isinstance(primary_ids, list):
            primary_ids = []
        stat = path.stat()
        original_url = safe_web_url(frontmatter.get("original_url"))
        resolved_url = safe_web_url(frontmatter.get("resolved_url"))
        display_url = resolved_url or original_url
        video_id = youtube_video_id(display_url)
        title = clean_text(frontmatter.get("title") or path.stem)
        return {
            "article_key": clean_text(frontmatter.get("article_key") or path.stem),
            "title": title,
            "publisher": clean_text(
                frontmatter.get("publisher_label") or frontmatter.get("publisher")
            ),
            "source_domain": clean_text(frontmatter.get("source_domain")),
            "original_url": original_url,
            "resolved_url": resolved_url,
            "embed_url": youtube_embed_url(display_url) if video_id else display_url,
            "is_youtube": bool(video_id),
            "published_at": clean_text(frontmatter.get("published_at")),
            "content_status": clean_text(frontmatter.get("content_status") or "unknown"),
            "content_completeness": clean_text(frontmatter.get("content_completeness")),
            "content_length": int(frontmatter.get("content_length") or 0),
            "keyword_assignment": clean_text(
                frontmatter.get("keyword_assignment") or "unmatched"
            ),
            "matched_keyword_ids": matched_ids,
            "primary_keyword_ids": primary_ids,
            "matched_keywords": [
                {"id": identifier, "label": labels.get(identifier, identifier)}
                for identifier in matched_ids
            ],
            "primary_keywords": [
                {"id": identifier, "label": labels.get(identifier, identifier)}
                for identifier in primary_ids
            ],
            "decision": review.get("decision", "pending"),
            "keyword_check": review.get("keyword_check", "pending"),
            "content_check": review.get("content_check", "pending"),
            "reason_code": review.get("reason_code", ""),
            "note": review.get("note", ""),
            "reviewed_at": review.get("reviewed_at", ""),
            "review_errors": review_errors,
            "overview": overview_from_markdown(markdown),
            "mtime_ns": str(stat.st_mtime_ns),
        }

    def articles(self, run_date: str, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not refresh and run_date in self._cache:
                rows = self._cache[run_date]
            else:
                date_dir = self._date_dir(run_date)
                labels = self.keyword_labels(run_date)
                rows = [
                    self._summary(path, labels)
                    for path in sorted((date_dir / "articles").glob("*.md"))
                ]
                rows.sort(key=lambda row: (row["published_at"], row["title"]), reverse=True)
                rows.sort(key=lambda row: row["decision"] != "pending")
                self._cache[run_date] = rows
            labels = self.keyword_labels(run_date)
            return {
                "date": run_date,
                "articles": rows,
                "keywords": [
                    {"id": identifier, "label": label}
                    for identifier, label in sorted(
                        labels.items(), key=lambda item: item[1].casefold()
                    )
                ],
            }

    def article(self, run_date: str, article_key: str) -> dict[str, Any]:
        path = self._article_path(run_date, article_key)
        labels = self.keyword_labels(run_date)
        markdown = path.read_text(encoding="utf-8")
        result = self._summary(path, labels)
        result["date"] = run_date
        result["body_markdown"] = without_review_block(without_frontmatter(markdown))
        result["video_metadata"] = (
            video_metadata_from_markdown(markdown, result["title"]) if result["is_youtube"] else None
        )
        return result

    def update_review(
        self,
        run_date: str,
        article_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        decision = clean_text(payload.get("decision"))
        keyword_check = clean_text(payload.get("keyword_check"))
        content_check = clean_text(payload.get("content_check"))
        reason_code = clean_text(payload.get("reason_code"))
        note = str(payload.get("note") or "").replace("\r\n", "\n").replace("\r", "\n")
        if decision not in REVIEW_VALUES["decision"]:
            raise ReviewStoreError("記事判定が不正です")
        if keyword_check not in REVIEW_VALUES["keyword_check"]:
            raise ReviewStoreError("キーワード判定が不正です")
        if content_check not in REVIEW_VALUES["content_check"]:
            raise ReviewStoreError("内容判定が不正です")
        if reason_code not in REASON_CODES:
            raise ReviewStoreError("不可理由が不正です")
        if decision == "rejected" and not reason_code:
            raise ReviewStoreError("不可にする場合は理由を選択してください")
        if len(note) > MAX_NOTE_LENGTH:
            raise ReviewStoreError(f"メモは{MAX_NOTE_LENGTH}文字以内にしてください")
        if "\x00" in note:
            raise ReviewStoreError("メモに使用できない文字が含まれています")

        path = self._article_path(run_date, article_key)
        with self._lock:
            current_mtime = str(path.stat().st_mtime_ns)
            expected_mtime = str(payload.get("expected_mtime_ns") or "")
            if expected_mtime and expected_mtime != current_mtime:
                raise ReviewConflictError(
                    "別の場所でMarkdownが更新されています。再読み込みしてください"
                )
            markdown = path.read_text(encoding="utf-8")
            start = markdown.find(REVIEW_START)
            end = markdown.find(REVIEW_END)
            if start < 0 or end < start:
                raise ReviewStoreError("Markdownにレビュー欄がありません")
            reviewed = any(
                (
                    decision != "pending",
                    keyword_check != "pending",
                    content_check != "pending",
                    bool(note.strip()),
                )
            )
            review_block = "\n".join(
                [
                    REVIEW_START,
                    "~~~yaml",
                    f"decision: {decision}",
                    f"keyword_check: {keyword_check}",
                    f"content_check: {content_check}",
                    f"reason_code: {json.dumps(reason_code, ensure_ascii=False)}",
                    f"note: {json.dumps(note, ensure_ascii=False)}",
                    f"reviewed_at: {json.dumps(utc_now() if reviewed else '', ensure_ascii=False)}",
                    "~~~",
                    REVIEW_END,
                ]
            )
            updated = markdown[:start] + review_block + markdown[end + len(REVIEW_END) :]
            atomic_write(path, updated)
            self._cache.pop(run_date, None)
        return self.article(run_date, article_key)


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        store: ReviewStore,
    ) -> None:
        super().__init__(address, handler)
        self.store = store


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {format_string % args}\n"
        )

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https:; connect-src 'self'; frame-src https: http:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ReviewStoreError("リクエストサイズが不正です") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ReviewStoreError("リクエストサイズが不正です")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewStoreError("JSONが不正です") from exc
        if not isinstance(payload, dict):
            raise ReviewStoreError("JSONオブジェクトを送信してください")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/dates":
                dates = self.server.store.dates()
                self.send_json(
                    {
                        "dates": dates,
                        "default_date": dates[0]["date"] if dates else "",
                    }
                )
                return
            if parsed.path == "/api/articles":
                query = parse_qs(parsed.query)
                run_date = (query.get("date") or [""])[0]
                refresh = (query.get("refresh") or ["0"])[0] == "1"
                self.send_json(self.server.store.articles(run_date, refresh=refresh))
                return
            match = re.fullmatch(r"/api/article/([^/]+)/([^/]+)", parsed.path)
            if match:
                self.send_json(
                    self.server.store.article(
                        unquote(match.group(1)), unquote(match.group(2))
                    )
                )
                return
            self.serve_static(parsed.path)
        except ReviewStoreError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except FileNotFoundError:
            self.send_error_json("ファイルが見つかりません", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_json(f"読み込みに失敗しました: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/article/([^/]+)/([^/]+)/review", parsed.path)
        if not match:
            self.send_error_json("APIが見つかりません", HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json()
            article = self.server.store.update_review(
                unquote(match.group(1)), unquote(match.group(2)), payload
            )
            self.send_json({"saved": True, "article": article})
        except ReviewConflictError as exc:
            self.send_error_json(str(exc), HTTPStatus.CONFLICT)
        except ReviewStoreError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_error_json(f"保存に失敗しました: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path).lstrip("/")
        static_root = STATIC_ROOT.resolve()
        path = (static_root / relative).resolve()
        if path.parent != static_root or not path.is_file():
            self.send_error_json("ページが見つかりません", HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("安全のためlocalhost以外にはバインドできません")
    if not STATIC_ROOT.is_dir():
        raise SystemExit(f"画面ファイルがありません: {STATIC_ROOT}")
    store = ReviewStore(args.review_root)
    server = ReviewServer((args.host, args.port), ReviewRequestHandler, store)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Article Review Viewer: {url}", flush=True)
    print("終了するには Ctrl+C を押してください。", flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
