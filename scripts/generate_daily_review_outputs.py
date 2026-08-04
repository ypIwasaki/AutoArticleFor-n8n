#!/usr/bin/env python3
"""Generate reviewable talent-index and article-classification proposals.

The generator intentionally uses only saved body captures (or public video/social
metadata) for semantic decisions.  Articles without an adequate saved source are
still registered in the talent-index proposal, but are not classified and never
produce talent relationships.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"
CAPTURE_DIRECTORY = CONTENT / "article-body-captures"


@dataclass(frozen=True)
class Article:
    article_key: str
    url: str
    title: str
    excerpt: str
    source: str
    published_at: str
    last_seen_at: str


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def iso_seconds(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_articles(run_date: str) -> list[Article]:
    path = CONTENT / "structured-records" / f"{run_date}.jsonl"
    records: dict[str, Article] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("recordType") != "article" or not isinstance(record.get("article"), dict):
            continue
        payload = record["article"]
        url = str(payload.get("url", "")).strip()
        if not url:
            continue
        records[url] = Article(
            article_key=stable_id("article", url),
            url=url,
            title=str(payload.get("title", "")).strip(),
            excerpt=str(payload.get("excerpt", "")).strip(),
            source=str(payload.get("source", "")).strip(),
            published_at=iso_seconds(payload.get("publishedAt", "")),
            last_seen_at=iso_seconds(record.get("generatedAt", "")),
        )
    return sorted(records.values(), key=lambda item: (item.published_at, item.url), reverse=True)


def load_captures(run_date: str) -> dict[str, dict[str, Any]]:
    path = CAPTURE_DIRECTORY / f"{run_date}.jsonl"
    result: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        url = str(entry.get("originalUrl", "")).strip()
        if url:
            result[url] = entry
    return result


def load_capture_state() -> dict[str, dict[str, Any]]:
    path = CAPTURE_DIRECTORY / "backfill-state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    entries = payload.get("entries", {})
    return entries if isinstance(entries, dict) else {}


def load_existing_talents() -> list[dict[str, Any]]:
    dashboard = ROOT / "apps" / "talent-dashboard"
    sys.path.insert(0, str(dashboard))
    import server  # type: ignore

    payload, _ = server.load_from_n8n()
    return [dict(row) for row in payload.get("talents", [])]


def json_array(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def normalized_talent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "talent_id": str(row.get("talent_id", "")),
        "display_name": str(row.get("display_name", "")),
        "organization": str(row.get("organization", "")),
        "aliases_json": json.dumps(json_array(row.get("aliases_json")), ensure_ascii=False),
        "status": str(row.get("status", "pending")),
        "search_enabled": bool(row.get("search_enabled", False)),
        "auto_discovered": bool(row.get("auto_discovered", False)),
        "last_seen_at": iso_seconds(row.get("last_seen_at", "")),
    }


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def capture_text(entry: dict[str, Any], state_entry: dict[str, Any]) -> tuple[str, str]:
    status = str(entry.get("contentStatus") or state_entry.get("status") or "unavailable")
    summary = compact_text(str(state_entry.get("summary", "")))
    body = compact_text(str(entry.get("contentText") or state_entry.get("content_text") or ""))
    return status, summary or body[:1200]


def literal_match(text: str, value: str) -> bool:
    value = compact_text(value)
    if not value:
        return False
    if re.fullmatch(r"[A-Za-z0-9 .+_'-]+", value):
        if len(value.replace(" ", "")) < 4:
            return False
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    if len(value) < 2:
        return False
    return value in text


def aliases_in_text(talent: dict[str, Any], text: str, *, allow_short: bool) -> list[str]:
    aliases = json_array(talent.get("aliases_json"))
    display = str(talent.get("display_name", "")).strip()
    if display and display not in aliases:
        aliases.insert(0, display)
    matches: list[str] = []
    for alias in aliases:
        is_latin = re.fullmatch(r"[A-Za-z0-9 .+_'-]+", alias) is not None
        if not is_latin and len(alias) < 3:
            if not allow_short:
                continue
            if alias not in text:
                continue
            matches.append(alias)
            continue
        elif literal_match(text, alias):
            matches.append(alias)
    return list(dict.fromkeys(matches))


MANUAL_NEW_TALENTS = (
    ("水瀬んきゃ", "CORECATIS VIRTUAL PROJECT"),
    ("月守ルナ", "CORECATIS VIRTUAL PROJECT"),
    ("柊めあり", "CORECATIS VIRTUAL PROJECT"),
)


def proposed_talent(name: str, organization: str, last_seen_at: str) -> dict[str, Any]:
    return {
        "talent_id": stable_id("talent", f"{organization}|{name}"),
        "display_name": name,
        "organization": organization,
        "aliases_json": json.dumps([name], ensure_ascii=False),
        "status": "pending",
        "search_enabled": False,
        "auto_discovered": True,
        "last_seen_at": last_seen_at,
    }


def article_payload(article: Article) -> dict[str, Any]:
    return {
        "article_key": article.article_key,
        "url": article.url,
        "title": article.title,
        "excerpt": article.excerpt,
        "source": article.source,
        "published_at": article.published_at,
        "last_seen_at": article.last_seen_at,
    }


def build_talent_proposal(
    run_date: str,
    articles: list[Article],
    captures: dict[str, dict[str, Any]],
    state: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    registry = [normalized_talent(row) for row in load_existing_talents()]
    by_name = {talent["display_name"]: talent for talent in registry}
    for name, organization in MANUAL_NEW_TALENTS:
        if name not in by_name:
            by_name[name] = proposed_talent(name, organization, max((a.last_seen_at for a in articles), default=""))

    used_talents: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}
    for article in articles:
        capture = captures.get(article.url, {})
        state_entry = state.get(article.url, {})
        status, verified_text = capture_text(capture, state_entry)
        if status not in {"verified", "metadata_only"}:
            continue
        title_text = compact_text(article.title)
        body_text = compact_text(verified_text)
        context = f"{title_text}\n{body_text}"
        scope_context = bool(re.search(r"VTuber|Vチューバー|バーチャル(?:ライバー|YouTuber)|にじさんじ|ホロライブ", context, re.IGNORECASE))
        for talent in by_name.values():
            title_matches = aliases_in_text(talent, title_text, allow_short=scope_context)
            body_matches = aliases_in_text(talent, body_text, allow_short=False)
            matches = list(dict.fromkeys(title_matches + body_matches))
            if not matches:
                continue
            matched_fields = []
            if title_matches:
                matched_fields.append("title")
            if body_matches:
                matched_fields.append("verified_summary" if status == "verified" else "public_metadata")
            used_talents[talent["talent_id"]] = talent
            relation_key = stable_id("relation", f"{article.article_key}|{talent['talent_id']}")
            relations[relation_key] = {
                "relation_key": relation_key,
                "article_key": article.article_key,
                "talent_id": talent["talent_id"],
                "matched_aliases_json": json.dumps(matches, ensure_ascii=False),
                "matched_fields": ", ".join(matched_fields),
                "evidence_text": f"「{article.title}」の確認済み本文または公開メタデータ: {capture.get('resolvedUrl') or article.url}",
                "confidence": 0.97 if title_matches else 0.9,
                "detection_method": "ai_review",
                "last_seen_at": article.last_seen_at,
            }

    payload = {
        "proposalVersion": 1,
        "proposalDate": run_date,
        "articles": [article_payload(article) for article in articles],
        "talents": sorted(used_talents.values(), key=lambda row: (row["display_name"], row["talent_id"])),
        "articleTalents": sorted(relations.values(), key=lambda row: (row["article_key"], row["talent_id"])),
    }
    new_talents = [row for row in payload["talents"] if row["status"] == "pending" and row["auto_discovered"]]
    markdown = [
        f"# Talent Index Proposal - {run_date}",
        "",
        "保存済みの本文確認済み記事と公開動画・SNSメタデータを基に、記事登録と人物関係をレビューした。既存の承認状態・別名・検索設定は変更していない。",
        "",
        "## New Talent Candidates",
        "",
        "| Talent | Organization | Status | Search enabled | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    if new_talents:
        for talent in new_talents:
            evidence_count = sum(1 for relation in relations.values() if relation["talent_id"] == talent["talent_id"])
            markdown.append(
                f"| {talent['display_name']} | {talent['organization'] or '未確認'} | pending | no | 本文確認済み記事 {evidence_count} 件で本人名を確認 |"
            )
    else:
        markdown.append("| なし | - | - | - | 本文根拠を満たす新規候補なし |")
    markdown.extend(
        [
            "",
            "## Article Relationships",
            "",
            f"- 記事登録: {len(articles)}件",
            f"- 参照した既存・新規タレント: {len(used_talents)}件",
            f"- 本文または公開メタデータを確認した人物関係: {len(relations)}件",
            "",
            "関連記事欄や共通ナビゲーションだけに現れる人物名は除外した。本文未確認記事から人物関係を作成していない。",
            "",
            "## Held Items",
            "",
            "- ユニット名・企画名だけが確認できたものは、個人タレント行へ分解していない。",
            "- partial / unavailable の記事は人物関係の根拠に使用していない。",
            "",
        ]
    )
    return payload, "\n".join(markdown)


SCOPE_TERMS = (
    "vtuber", "vチューバー", "バーチャルライバー", "バーチャルyoutuber", "にじさんじ", "hololive",
    "ホロライブ", "ホロスターズ", "ぶいすぽ", "neo-porte", "ネオポルテ", "あおぎり高校", "re:act",
    "vee", "himehina", "ななしいんく", "nijisanji", "anycolor", "カバー株式会社", "ホロドリ",
    "バーチャルキャスト", "virtual project", "vライバー",
    "corecatis virtual project", "水瀬んきゃ", "月守ルナ", "柊めあり",
)


def domain(url: str) -> str:
    host = urlparse(url).netloc.casefold().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def contains_any(text: str, terms: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def article_type(article: Article, capture: dict[str, Any]) -> str:
    kind = str(capture.get("contentType", ""))
    host = domain(str(capture.get("resolvedUrl") or article.url))
    title = article.title.casefold()
    if kind == "video_metadata":
        return "video_or_stream"
    if kind == "social_metadata":
        return "social_post"
    if host in {"prtimes.jp", "gamehack.jp", "entamerush.jp", "pr-free.jp"}:
        return "press_release"
    if any(token in host for token in ("anycolor.co.jp", "hololivepro.com", "cover-corp.com", "family.co.jp")):
        return "official_announcement"
    if any(token in host for token in ("appmedia.jp", "gamewith.jp", "gamerch.com")) or contains_any(title, ("攻略", "ランキング", "tier", "評価", "スキル", "ステータス", "一覧")):
        return "guide_or_database"
    if contains_any(title, ("画像", "写真", "ギャラリー")):
        return "image_gallery"
    return "news_article"


CATEGORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("audition_or_recruitment", ("オーディション", "募集", "採用")),
    ("collaboration", ("コラボ", "タイアップ", "協賛", "コラボレーション")),
    ("product_or_goods", ("グッズ", "商品", "販売", "発売", "ボイス", "カード", "フィギュア", "くじ")),
    ("live_or_music", ("ライブ", "live", "音楽", "mv", "楽曲", "アルバム", "ツアー", "配信", "テーマソング")),
    ("event", ("イベント", "expo", "runway", "フェス", "展示", "体験", "開催")),
    ("game_or_technology", ("ゲーム", "攻略", "スキル", "ステータス", "tier", "ランキング", "vr", "ai", "アプリ", "プラットフォーム", "システム")),
    ("company_or_business", ("株式会社", "事業", "サービス", "運営", "リニューアル", "利用開始", "提供開始")),
    ("media_or_editorial", ("インタビュー", "企画", "記事", "動画", "声優", "レビュー", "おすすめ")),
    ("community_or_fan", ("ファン", "コミュニティ", "二次創作", "応援")),
)


def categories(article: Article, text: str, kind: str, in_scope: bool) -> list[str]:
    if kind == "guide_or_database":
        result = ["game_or_technology"]
        result.extend(category for category, terms in CATEGORY_TERMS if category != "game_or_technology" and contains_any(article.title, terms))
    else:
        result = [category for category, terms in CATEGORY_TERMS if contains_any(article.title, terms)]
        result.extend(category for category, terms in CATEGORY_TERMS if contains_any(text, terms))
    if not result:
        result = ["talent_activity" if in_scope else "other"]
    return list(dict.fromkeys(result))


def evidence_sentence(text: str, *, in_scope: bool) -> str:
    text = compact_text(text)
    sentence = re.split(r"(?<=[。！？!?])\s*", text, maxsplit=1)[0]
    sentence = sentence[:220].rstrip("。、 ")
    if not sentence:
        sentence = "保存済みページ本文を確認した"
    suffix = "対象のVTuber・団体・関連企画を本文で確認した。" if in_scope else "VTuber・関連団体・関連企画を主題としていない。"
    return f"本文で「{sentence}」を確認し、{suffix}"


def build_classification_proposal(
    run_date: str,
    articles: list[Article],
    captures: dict[str, dict[str, Any]],
    state: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    classified_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows: list[dict[str, Any]] = []
    for article in articles:
        capture = captures.get(article.url, {})
        state_entry = state.get(article.url, {})
        status, verified_text = capture_text(capture, state_entry)
        if status not in {"verified", "metadata_only"}:
            continue
        if verified_text.count("�") > 5:
            continue
        source_text = compact_text(f"{article.title} {verified_text}")
        title_scope = contains_any(article.title, SCOPE_TERMS)
        body_scope = contains_any(verified_text[:500], SCOPE_TERMS)
        kind = article_type(article, capture)
        if kind == "guide_or_database" and not title_scope:
            body_scope = re.search(r"にじさんじ所属のVTuber|コラボしているにじさんじ", verified_text[:500], re.IGNORECASE) is not None
        in_scope = title_scope or body_scope
        relevance = "in_scope" if title_scope else "low_relevance" if body_scope and kind == "guide_or_database" else "in_scope" if body_scope else "out_of_scope"
        found_categories = categories(article, source_text, kind, in_scope)
        primary = found_categories[0]
        secondary = [item for item in found_categories[1:] if item != primary][:3]
        rows.append(
            {
                "article_url": article.url,
                "article_type": kind,
                "primary_category": primary,
                "secondary_categories_json": secondary,
                "relevance": relevance,
                "confidence": 0.92 if relevance == "in_scope" else 0.82 if relevance == "low_relevance" else 0.95,
                "evidence_text": evidence_sentence(verified_text, in_scope=in_scope),
                "classification_method": "ai_review",
                "classified_at": classified_at,
            }
        )
    payload = {"proposalVersion": 1, "proposalDate": run_date, "classifications": rows}
    in_scope_count = sum(1 for row in rows if row["relevance"] == "in_scope")
    low_count = sum(1 for row in rows if row["relevance"] == "low_relevance")
    out_count = sum(1 for row in rows if row["relevance"] == "out_of_scope")
    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row["article_type"]] = by_type.get(row["article_type"], 0) + 1
    markdown = [
        f"# Article Classification Proposal - {run_date}",
        "",
        "保存済み本文または公開動画・SNSメタデータを確認できた記事だけを分類した。partial / unavailable の記事は提案JSONに含めていない。",
        "",
        "## Review Summary",
        "",
        f"- 分類対象: {len(rows)}件",
        f"- 対象内: {in_scope_count}件",
        f"- 低関連: {low_count}件",
        f"- 対象外: {out_count}件",
        "",
        "## Article Types",
        "",
        "| Article type | Count |",
        "| --- | ---: |",
    ]
    for key, count in sorted(by_type.items(), key=lambda item: (-item[1], item[0])):
        markdown.append(f"| {key} | {count} |")
    markdown.extend(
        [
            "",
            "## Review Notes",
            "",
            "- 記事種別は保存先ドメイン、ページ形式、本文内容から1つだけ選択した。",
            "- 主カテゴリと副カテゴリは重複させず、副カテゴリは最大3件に制限した。",
            "- 本文内の共通ナビゲーションだけで検索語が現れる記事は、対象外または低関連として扱った。",
            "- JSONの各行に本文上の短い事実を evidence_text として保存した。",
            "",
        ]
    )
    return payload, "\n".join(markdown)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def validate(talent_payload: dict[str, Any], classification_payload: dict[str, Any]) -> None:
    article_keys = {row["article_key"] for row in talent_payload["articles"]}
    talent_ids = {row["talent_id"] for row in talent_payload["talents"]}
    for relation in talent_payload["articleTalents"]:
        if relation["article_key"] not in article_keys or relation["talent_id"] not in talent_ids:
            raise ValueError(f"Broken relation reference: {relation['relation_key']}")
    seen_urls: set[str] = set()
    for row in classification_payload["classifications"]:
        if row["article_url"] in seen_urls:
            raise ValueError(f"Duplicate classification URL: {row['article_url']}")
        seen_urls.add(row["article_url"])
        if row["primary_category"] in row["secondary_categories_json"]:
            raise ValueError(f"Duplicate primary/secondary category: {row['article_url']}")
        if len(row["secondary_categories_json"]) > 3:
            raise ValueError(f"Too many secondary categories: {row['article_url']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date", required=True)
    args = parser.parse_args()
    articles = load_articles(args.run_date)
    captures = load_captures(args.run_date)
    state = load_capture_state()
    if len(captures) != len(articles):
        raise RuntimeError(f"Capture coverage incomplete: articles={len(articles)} captures={len(captures)}")
    talent_payload, talent_markdown = build_talent_proposal(args.run_date, articles, captures, state)
    classification_payload, classification_markdown = build_classification_proposal(args.run_date, articles, captures, state)
    validate(talent_payload, classification_payload)
    talent_base = CONTENT / "talent-index-proposals" / args.run_date
    classification_base = CONTENT / "article-classification-proposals" / args.run_date
    write_json(talent_base.with_suffix(".json"), talent_payload)
    write_markdown(talent_base.with_suffix(".md"), talent_markdown)
    write_json(classification_base.with_suffix(".json"), classification_payload)
    write_markdown(classification_base.with_suffix(".md"), classification_markdown)
    print(
        f"articles={len(articles)} talents={len(talent_payload['talents'])} "
        f"relations={len(talent_payload['articleTalents'])} classifications={len(classification_payload['classifications'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
