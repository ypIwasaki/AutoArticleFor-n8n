#!/usr/bin/env python3
"""Validate article Markdown reviews and optionally apply them through n8n."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from article_review_common import (
    PROJECT_ROOT,
    canonical_title,
    clean_text,
    parse_frontmatter,
    parse_review_block,
)
from sync_workflow_to_n8n import load_env_file


DEFAULT_REVIEW_ROOT = PROJECT_ROOT / "content" / "article-review"
DEFAULT_WEBHOOK = "http://127.0.0.1:5678/webhook/article-feedback/reject"


def latest_date(review_root: Path) -> str:
    dates = sorted(path.name for path in review_root.glob("????-??-??") if path.is_dir())
    if not dates:
        raise RuntimeError("content/article-review に日付別レビューがありません")
    return dates[-1]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def read_reviews(review_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    reviews: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_keys: dict[str, Path] = {}
    articles_dir = review_dir / "articles"
    if not articles_dir.exists():
        return [], [f"{articles_dir} がありません"]
    for path in sorted(articles_dir.glob("*.md")):
        markdown = path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(markdown)
        review, review_errors = parse_review_block(markdown)
        article_key = clean_text(frontmatter.get("article_key"))
        article_url = clean_text(frontmatter.get("original_url"))
        if not article_key:
            review_errors.append("article_key がありません")
        if not article_url:
            review_errors.append("original_url がありません")
        if article_key in seen_keys:
            review_errors.append(f"article_key が {seen_keys[article_key].name} と重複しています")
        elif article_key:
            seen_keys[article_key] = path
        if review_errors:
            errors.extend(f"{path.name}: {message}" for message in review_errors)
            continue
        reviews.append(
            {
                "path": path,
                "article_key": article_key,
                "article_url": article_url,
                "title": clean_text(frontmatter.get("title")),
                "source_domain": clean_text(frontmatter.get("source_domain")),
                "publisher_label": clean_text(frontmatter.get("publisher_label")),
                "title_signature": clean_text(frontmatter.get("title_signature"))
                or canonical_title(frontmatter.get("title")),
                **review,
            }
        )
    return reviews, errors


def feedback_payload(review: dict[str, Any]) -> dict[str, str]:
    decision = review["decision"]
    reason = review.get("reason_code", "")
    return {
        "articleKey": review["article_key"],
        "articleUrl": review["article_url"],
        "decision": decision,
        "reasonCode": reason if decision == "rejected" else "approved",
        "sourceDomain": review["source_domain"] if reason == "suspicious_source" else "",
        "publisherLabel": review["publisher_label"] if reason == "suspicious_source" else "",
        "titleSignature": review["title_signature"] if reason == "irrelevant" else "",
        "source": "article-review-markdown",
    }


def call_webhook(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    webhook_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(webhook_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"n8n returned HTTP {exc.code}: {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Could not connect to n8n: {exc.reason}") from exc
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("n8n returned invalid JSON") from exc
    if not isinstance(result, dict) or not result.get("accepted"):
        raise RuntimeError(str(result.get("reason") if isinstance(result, dict) else result))
    return result


def print_summary(run_date: str, reviews: list[dict[str, Any]]) -> Counter[str]:
    counts = Counter(review["decision"] for review in reviews)
    print(f"レビュー日: {run_date}")
    print(f"レビューMarkdown: {len(reviews)}件")
    print(f"未評価: {counts['pending']}件")
    print(f"可: {counts['approved']}件")
    print(f"不可: {counts['rejected']}件")
    print(f"再取得: {counts['needs_refetch']}件")
    if counts["rejected"]:
        print("\n不可として反映予定:")
        for review in reviews:
            if review["decision"] == "rejected":
                print(f"- {review['article_key']}: {review['reason_code']} / {review['title']}")
    return counts


def apply_reviews(
    run_date: str,
    reviews: list[dict[str, Any]],
    webhook_url: str,
    timeout: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for review in reviews:
        if review["decision"] not in {"approved", "rejected"}:
            continue
        try:
            response = call_webhook(webhook_url, feedback_payload(review), timeout)
            applied.append(
                {
                    "articleKey": review["article_key"],
                    "decision": review["decision"],
                    "reasonCode": review.get("reason_code", ""),
                    "sourceFile": str(review["path"].relative_to(PROJECT_ROOT)),
                    "response": response,
                }
            )
            print(f"applied {review['decision']}: {review['article_key']}")
        except Exception as exc:
            failures.append({"articleKey": review["article_key"], "error": str(exc)})
            print(f"error {review['article_key']}: {exc}")
    refetch = [
        {
            "recordType": "article-refetch-request",
            "articleKey": review["article_key"],
            "originalUrl": review["article_url"],
            "reviewNote": review.get("note", ""),
            "requestedAt": datetime.now(timezone.utc).isoformat(),
        }
        for review in reviews
        if review["decision"] == "needs_refetch"
    ]
    if refetch:
        refetch_path = PROJECT_ROOT / "content" / "article-refetch-requests" / f"{run_date}.jsonl"
        atomic_write(
            refetch_path,
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in refetch),
        )
    results_path = PROJECT_ROOT / "content" / "article-review-results" / f"{run_date}.json"
    atomic_write(
        results_path,
        json.dumps(
            {
                "schemaVersion": 1,
                "reviewDate": run_date,
                "appliedAt": datetime.now(timezone.utc).isoformat(),
                "applied": applied,
                "failures": failures,
                "refetchRequests": len(refetch),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return applied, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="run_date", help="YYYY-MM-DD。省略時は最新日")
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--webhook-url")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--apply", action="store_true", help="検証後にn8nへ反映する")
    args = parser.parse_args()

    run_date = args.run_date or latest_date(args.review_root)
    reviews, validation_errors = read_reviews(args.review_root / run_date)
    if validation_errors:
        print("レビュー入力エラー:")
        for message in validation_errors:
            print(f"- {message}")
        print("エラーがあるため、1件も反映しません。")
        return 2

    print_summary(run_date, reviews)
    if not args.apply:
        print("\nドライランです。反映する場合は --apply を付けて再実行してください。")
        return 0

    load_env_file(args.env_file)
    webhook_url = args.webhook_url or os.environ.get(
        "N8N_ARTICLE_FEEDBACK_WEBHOOK_URL", DEFAULT_WEBHOOK
    )
    applied, failures = apply_reviews(run_date, reviews, webhook_url, args.timeout)
    print(f"\n反映完了: {len(applied)}件 / 失敗: {len(failures)}件")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
