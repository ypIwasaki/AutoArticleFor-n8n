#!/usr/bin/env python3
"""Generate canonical article review Markdown and keyword-grouped references."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from article_review_common import (
    KeywordCatalog, PROJECT_ROOT, canonical_title, clean_text, default_review_block, extract_review_block,
    keyword_matches, load_jsonl, markdown_text, markdown_url, publisher_label,
    render_frontmatter, safe_path_component, source_domain, stable_article_key, unique_strings,
)

STRUCTURED_DIR = PROJECT_ROOT / "content" / "structured-records"
CAPTURE_DIR = PROJECT_ROOT / "content" / "article-body-captures"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "content" / "article-review"


def latest_date() -> str:
    paths = sorted(STRUCTURED_DIR.glob("????-??-??.jsonl"))
    if not paths:
        raise RuntimeError("content/structured-records に日付別JSONLがありません")
    return paths[-1].stem


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def article_filename(article_key: str) -> str:
    if article_key and len(article_key) <= 120 and all(
        character.isalnum() or character in "-_." for character in article_key
    ):
        return article_key + ".md"
    return safe_path_component(article_key, "article") + ".md"


def load_source_records(run_date: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = load_jsonl(STRUCTURED_DIR / f"{run_date}.jsonl")
    run = next((record for record in records if record.get("recordType") == "run"), {})
    articles = [record for record in records if record.get("recordType") == "article"]
    return run, articles


def load_captures(run_date: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    captures = load_jsonl(CAPTURE_DIR / f"{run_date}.jsonl")
    by_url: dict[str, dict[str, Any]] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for capture in captures:
        url = clean_text(capture.get("originalUrl"))
        key = clean_text(capture.get("articleKey"))
        if url:
            by_url[url] = capture
        if key:
            by_key[key] = capture
    return by_url, by_key


def content_text_to_markdown(value: Any) -> str:
    paragraphs = [clean_text(line) for line in str(value or "").splitlines() if clean_text(line)]
    return "\n\n".join(paragraphs)


def capture_value(capture: dict[str, Any], camel: str, snake: str, default: Any = "") -> Any:
    return capture.get(camel, capture.get(snake, default))


def keyword_table(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "本文・タイトル・保存済み検索情報からキーワード一致を確認できませんでした。"
    lines = [
        "| キーワード | スコア | 一致場所 | 本文 | 共通領域 |",
        "| --- | ---: | --- | ---: | ---: |",
    ]
    for row in matches:
        locations = ", ".join(row["locations"]) or "なし"
        counts = row["counts"]
        lines.append(
            f"| {markdown_text(row['label'])} | {row['score']} | {locations} | "
            f"{counts['main_content']} | {counts['non_content']} |"
        )
    return "\n".join(lines)


def render_article(
    run_date: str,
    record: dict[str, Any],
    article: dict[str, Any],
    capture: dict[str, Any],
    match_result: dict[str, Any],
    review_block: str,
) -> str:
    title = clean_text(article.get("title")) or "無題の記事"
    original_url = clean_text(article.get("url"))
    resolved_url = clean_text(capture_value(capture, "resolvedUrl", "resolved_url"))
    content_status = clean_text(capture_value(capture, "contentStatus", "status", "unavailable"))
    completeness = clean_text(capture_value(capture, "contentCompleteness", "content_completeness"))
    content_text = str(capture_value(capture, "contentText", "content_text") or "")
    content_markdown = str(capture_value(capture, "contentMarkdown", "content_markdown") or "").strip()
    content_markdown = content_markdown or content_text_to_markdown(content_text)
    metadata = capture_value(capture, "pageMetadata", "page_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    summary = clean_text(
        capture.get("summary")
        or metadata.get("description")
        or metadata.get("og:description")
        or article.get("excerpt")
    )
    failure_reason = clean_text(capture_value(capture, "failureReason", "reason"))
    source_host = source_domain(resolved_url or original_url)
    frontmatter = render_frontmatter(
        {
            "schema_version": 1,
            "article_key": clean_text(capture.get("articleKey"))
            or clean_text(capture.get("article_key"))
            or stable_article_key(original_url),
            "run_date": run_date,
            "title": title,
            "original_url": original_url,
            "resolved_url": resolved_url,
            "publisher": clean_text(article.get("source")),
            "source_domain": source_host,
            "publisher_label": publisher_label(title, article.get("source")),
            "title_signature": canonical_title(title),
            "published_at": clean_text(article.get("publishedAt")),
            "fetched_at": clean_text(capture_value(capture, "fetchedAt", "processed_at")),
            "content_status": content_status,
            "content_completeness": completeness,
            "content_length": len(content_text),
            "keyword_assignment": match_result["assignment"],
            "matched_keyword_ids": [row["keyword_id"] for row in match_result["matches"]],
            "primary_keyword_ids": match_result["primary_keywords"],
            "keyword_match_method": clean_text(
                article.get("keywordMatchMethod")
                or record.get("keywordMatchMethod")
                or ("rss-query" if article.get("matchedSearchKeywords") else "inferred")
            ),
        }
    )
    target_url = markdown_url(resolved_url or original_url)
    lines = [
        frontmatter,
        f"# {markdown_text(title)}",
        "",
        f"- 元記事: [{target_url}]({target_url})",
        f"- 配信元: {markdown_text(article.get('source') or source_host or '不明')}",
        f"- 公開日時: {markdown_text(article.get('publishedAt') or '不明')}",
        f"- 本文取得状態: {markdown_text(content_status or 'unavailable')}",
        f"- 内容充足度: {markdown_text(completeness or '未判定')}",
        "",
        "## 内容の概要",
        "",
        summary or "自動概要を作成できませんでした。抽出本文または取得失敗理由を確認してください。",
        "",
        f"- キーワード割当: {match_result['assignment']}",
        "- 主キーワードID: " + (", ".join(match_result["primary_keywords"]) or "なし"),
        "",
        "## キーワード確認",
        "",
        keyword_table(match_result["matches"]),
        "",
    ]
    non_content_matches = [
        row for row in match_result["matches"] if row["counts"]["non_content"]
    ]
    if non_content_matches:
        lines.extend(
            [
                "### 共通領域で見つかったキーワード",
                "",
                *[
                    f"- {markdown_text(row['label'])}: {row['counts']['non_content']}回"
                    for row in non_content_matches
                ],
                "",
                "ナビゲーション、ヘッダー、サイドバー、フッターの一致です。"
                "本文との関連性を判断する際は弱い根拠として扱ってください。",
                "",
            ]
        )
    lines.extend(["## 抽出本文", "", content_markdown or "本文を抽出できませんでした。"])
    if failure_reason:
        lines.extend(["", "## 取得時の注記", "", failure_reason])
    lines.extend(
        [
            "",
            "## ユーザーレビュー",
            "",
            "> 下のYAMLブロックだけを編集してください。"
            "decisionがrejectedの場合はreason_codeが必要です。",
            "",
            review_block,
            "",
        ]
    )
    return "\n".join(lines)


def render_reference(item: dict[str, Any], match: dict[str, Any]) -> str:
    canonical = "../../articles/" + item["file_name"]
    return "\n".join(
        [
            render_frontmatter(
                {
                    "generated": True,
                    "article_key": item["article_key"],
                    "canonical_path": canonical,
                    "keyword_id": match["keyword_id"],
                    "keyword_label": match["label"],
                    "match_score": match["score"],
                    "content_status": item["content_status"],
                }
            ),
            f"# {markdown_text(item['title'])}",
            "",
            f"- キーワード: {markdown_text(match['label'])}",
            f"- 一致場所: {', '.join(match['locations']) or 'なし'}",
            f"- 本文取得状態: {markdown_text(item['content_status'])}",
            f"- [記事本文とレビュー欄を開く]({canonical})",
            "",
            "> このファイルは自動生成された参照です。レビューは正本で行ってください。",
        ]
    )


def render_keyword_index(keyword: dict[str, Any], items: list[dict[str, Any]]) -> str:
    lines = [
        f"# キーワード: {markdown_text(keyword['label'])}",
        "",
        f"- キーワードID: {keyword['keyword_id']}",
        f"- 記事数: {len(items)}",
        "",
    ]
    for item in items:
        match = next(
            row for row in item["match_result"]["matches"]
            if row["keyword_id"] == keyword["keyword_id"]
        )
        link = f"./{item['file_name']}" if item.get("file_name") else markdown_url(item["url"])
        lines.append(
            f"- [{markdown_text(item['title'])}]({link})"
            f" — スコア {match['score']} / {', '.join(match['locations']) or '一致位置なし'}"
        )
    return "\n".join(lines)


def render_list(title: str, items: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", "", f"- 件数: {len(items)}", ""]
    if not items:
        lines.append("- 該当記事はありません。")
        return "\n".join(lines)
    for item in items:
        target = (
            f"./articles/{item['file_name']}" if item.get("file_name") else markdown_url(item["url"])
        )
        reason = item.get("failure_reason") or item.get("content_status") or ""
        lines.append(f"- [{markdown_text(item['title'])}]({target}) — {markdown_text(reason)}")
    return "\n".join(lines)


def generate(run_date: str, output_root: Path, clean_generated: bool = False) -> dict[str, int]:
    run, records = load_source_records(run_date)
    capture_by_url, capture_by_key = load_captures(run_date)
    output_dir = output_root / run_date
    articles_dir = output_dir / "articles"
    keyword_root = output_dir / "by-keyword"
    if clean_generated and keyword_root.exists():
        shutil.rmtree(keyword_root)
    articles_dir.mkdir(parents=True, exist_ok=True)
    keyword_root.mkdir(parents=True, exist_ok=True)

    catalog = KeywordCatalog.load()
    active_keywords = unique_strings(run.get("keywords", []))
    items: list[dict[str, Any]] = []
    by_keyword: dict[str, list[dict[str, Any]]] = defaultdict(list)
    keyword_labels: dict[str, dict[str, Any]] = {}

    for record in records:
        article = record.get("article", {})
        if not isinstance(article, dict):
            continue
        url = clean_text(article.get("url"))
        if not url:
            continue
        capture = capture_by_url.get(url, {})
        article_key = (
            clean_text(capture.get("articleKey"))
            or clean_text(capture.get("article_key"))
            or stable_article_key(url)
        )
        if not capture and article_key in capture_by_key:
            capture = capture_by_key[article_key]
        content_text = str(capture_value(capture, "contentText", "content_text") or "")
        content_markdown = str(capture_value(capture, "contentMarkdown", "content_markdown") or "")
        non_content_text = str(capture_value(capture, "nonContentText", "non_content_text") or "")
        search_keywords = article.get(
            "matchedSearchKeywords", record.get("matchedSearchKeywords", [])
        )
        rss_keywords = article.get("matchedRssKeywords", record.get("matchedRssKeywords", []))
        match_method = clean_text(
            article.get("keywordMatchMethod")
            or record.get("keywordMatchMethod")
            or ("rss-query" if search_keywords else "inferred")
        )
        match_result = keyword_matches(
            title=clean_text(article.get("title")),
            excerpt=clean_text(article.get("excerpt")),
            content_text=content_text,
            content_markdown=content_markdown,
            non_content_text=non_content_text,
            search_keywords=search_keywords,
            rss_keywords=rss_keywords,
            active_keywords=record.get("keywords", active_keywords),
            catalog=catalog,
            match_method=match_method,
        )
        content_status = clean_text(
            capture_value(capture, "contentStatus", "status", "unavailable")
        ) or "unavailable"
        file_name = article_filename(article_key)
        existing = articles_dir / file_name
        review_block = (
            extract_review_block(existing.read_text(encoding="utf-8"))
            if existing.exists()
            else None
        ) or default_review_block()
        atomic_write(
            existing,
            render_article(run_date, record, article, capture, match_result, review_block),
        )
        item = {
            "article_key": article_key,
            "title": clean_text(article.get("title")) or "無題の記事",
            "url": url,
            "file_name": file_name,
            "content_status": content_status,
            "failure_reason": clean_text(
                capture_value(capture, "failureReason", "reason", "本文キャプチャがありません")
            ),
            "match_result": match_result,
        }
        items.append(item)
        for match in match_result["matches"]:
            by_keyword[match["keyword_id"]].append(item)
            keyword_labels[match["keyword_id"]] = {
                "keyword_id": match["keyword_id"],
                "label": match["label"],
            }

    for keyword_id, keyword_items in by_keyword.items():
        definition = keyword_labels[keyword_id]
        keyword_dir = keyword_root / keyword_id
        keyword_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(keyword_dir / "index.md", render_keyword_index(definition, keyword_items))
        for item in keyword_items:
            match = next(
                row for row in item["match_result"]["matches"] if row["keyword_id"] == keyword_id
            )
            atomic_write(keyword_dir / item["file_name"], render_reference(item, match))

    multiple = [item for item in items if len(item["match_result"]["matches"]) > 1]
    ambiguous = [item for item in items if item["match_result"]["assignment"] == "tie"]
    unmatched = [item for item in items if not item["match_result"]["matches"]]
    partial = [item for item in items if item["content_status"] == "partial"]
    unavailable = [
        item for item in items
        if item["content_status"] in {"unavailable", "unverified"}
    ]
    atomic_write(output_dir / "_multiple-keywords.md", render_list("複数キーワード記事", multiple))
    atomic_write(output_dir / "_ambiguous.md", render_list("主キーワード同点記事", ambiguous))
    atomic_write(output_dir / "_unmatched.md", render_list("キーワード未一致記事", unmatched))
    atomic_write(output_dir / "partial.md", render_list("部分取得記事", partial))
    atomic_write(output_dir / "unavailable.md", render_list("取得不能記事", unavailable))
    index = "\n".join(
        [
            f"# Article Review - {run_date}",
            "",
            f"- 取得記事: {len(items)}件",
            f"- 内容確認用Markdown: {sum(bool(item['file_name']) for item in items)}件",
            f"- キーワードフォルダ: {len(by_keyword)}件",
            f"- 複数キーワード: {len(multiple)}件",
            f"- 主キーワード同点: {len(ambiguous)}件",
            f"- キーワード未一致: {len(unmatched)}件",
            f"- 取得不能: {len(unavailable)}件",
            "",
            "## 確認手順",
            "",
            "1. by-keyword/<keyword-id>/index.mdから記事を選ぶ。",
            "2. 参照Markdownからarticles/<article-key>.mdを開く。",
            "3. 抽出本文とキーワード確認表を読み、末尾のユーザーレビュー欄を編集する。",
            "4. scripts/apply_article_markdown_reviews.pyでドライラン後に一括反映する。",
        ]
    )
    atomic_write(output_dir / "index.md", index)
    return {
        "articles": len(items),
        "review_files": sum(bool(item["file_name"]) for item in items),
        "keyword_folders": len(by_keyword),
        "unavailable": len(unavailable),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="run_date", help="YYYY-MM-DD。省略時は最新日")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--clean-generated",
        action="store_true",
        help="対象日のby-keyword参照を削除してから再生成する。記事正本は削除しない。",
    )
    args = parser.parse_args()
    run_date = args.run_date or latest_date()
    result = generate(run_date, args.output_root, args.clean_generated)
    print(json.dumps({"date": run_date, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
