"""Article evaluation and feedback outputs shared by the dashboard and CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

from article_feedback_snapshot import atomic_text, build_snapshot
import project_business_writes as business
import project_database as project_db
import project_readers
from talent_dashboard_data import load_article_capture_metadata, load_dashboard_records


ARTICLE_FEEDBACK_MUTATION_LOCK = threading.RLock()


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
    host = host.casefold()
    if host.startswith("www."):
        host = host[4:]
    return "" if host in {"", "news.google.com", "b.hatena.ne.jp"} else host



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
    project_root: Path,
    payload: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> Path:
    if payload is None:
        payload, _ = load_dashboard_records(project_root)
    generated_at = generated_at or feedback_instruction_timestamp()
    output_dir = project_root / "content" / "article-feedback-instructions"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{generated_at.date().isoformat()}.md"
    instruction = build_article_feedback_instruction_markdown(payload, generated_at)
    snapshot = build_snapshot(payload, generated_at, instruction)
    database = project_readers.path_for(project_root)
    # Standalone archives have no route configuration. Never consult the live DB
    # when a caller explicitly supplies an isolated project root.
    write_target = "legacy"
    if database.exists() or project_root.resolve() == project_db.ROOT:
        write_target = business.route("ai-reader", database)
    if write_target == "project-db":
        packet = {
            "day": generated_at.date().isoformat(),
            "directory": "article-feedback-instructions",
            "document": snapshot,
            "markdown": instruction,
        }
        request_hash = project_db.checksum(project_db.canonical(packet).encode("utf-8"))
        business.submit(
            "db-feedback-document-" + request_hash,
            "proposal",
            packet,
            path=database,
            root=project_root,
        )
    else:
        atomic_text(output_path, instruction)
        atomic_text(output_path.with_suffix(".json"), json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
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


def evaluate_article(payload: dict[str, Any], project_root: Path) -> dict[str, Any]:
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
            data, _ = load_dashboard_records(project_root)
        except Exception as exc:
            raise RuntimeError(f"n8n Data Tables are unavailable: {exc}") from exc
        article = next(
            (item for item in data.get("articles", []) if str(item.get("article_key") or "") == article_key),
            None,
        )
        if article is None:
            raise ValueError("The article no longer exists in the current Data Table")

        capture_metadata = load_article_capture_metadata(project_root)
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
        instruction_path = write_article_feedback_instruction(project_root)
        result["feedbackInstructionFile"] = str(instruction_path.relative_to(project_root))
        return result
