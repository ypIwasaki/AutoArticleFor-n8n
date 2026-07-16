#!/usr/bin/env python3
"""Generate weekly trend and keyword-quality reports from saved JSONL records."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOISE_SOURCES = {"t.co", "mshale"}
GAME_GUIDE_PATTERN = re.compile(
    r"パワプロ|攻略|栄冠ナイン|育成理論|入手方法|おすすめ|効果|マネージャー|金特"
)
TOPIC_PATTERNS = {
    "商品・コラボ": re.compile(r"グッズ|コラボ|クリアファイル|アルバム|予約受付"),
    "募集・オーディション": re.compile(r"オーディション|募集開始|募集"),
    "イベント・ライブ": re.compile(r"ライブ|LIVE|周年|Anniversary|イベント"),
    "配信・番組": re.compile(r"番組|配信|歌枠|shorts|朝活"),
    "制作技術": re.compile(r"Live2D|VTuberモデル|テクスチャ"),
    "ゲーム攻略": GAME_GUIDE_PATTERN,
}


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def clean_cell(value: object) -> str:
    return str(value or "").replace("|", " ").replace("\n", " ").strip()


def format_table(headers: list[str], rows: list[list[object]]) -> list[str]:
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| " + " | ".join(clean_cell(value) for value in row) + " |"
        for row in rows
    ]
    return [header, divider, *(body or [["-" for _ in headers]])]


def article_text(article: dict[str, Any]) -> str:
    return "\n".join(
        str(article.get(field) or "")
        for field in ("title", "excerpt", "source")
    )


def infer_source(article: dict[str, Any]) -> str:
    source = str(article.get("source") or "").strip()
    if source:
        return source
    title = str(article.get("title") or "")
    if " - " in title:
        return title.rsplit(" - ", 1)[1].strip() or "(unknown)"
    return "(unknown)"


def noise_reasons(article: dict[str, Any]) -> list[str]:
    source = infer_source(article).casefold()
    text = article_text(article)
    reasons: list[str] = []
    if source in NOISE_SOURCES:
        reasons.append(f"{source}由来の単発投稿・切り抜き")
    if GAME_GUIDE_PATTERN.search(text):
        reasons.append("ゲーム攻略記事")
    if source == "appmedia":
        reasons.append("攻略系媒体")
    return reasons


def article_topics(article: dict[str, Any]) -> list[str]:
    text = article_text(article)
    return [name for name, pattern in TOPIC_PATTERNS.items() if pattern.search(text)]


def load_records(records_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []
    for path in sorted(records_dir.glob("????-??-??.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSONL: {path}:{line_number}: {error.msg}") from error
            record["_path"] = path
            if record.get("recordType") == "run":
                runs.append(record)
            elif record.get("recordType") == "article" and isinstance(record.get("article"), dict):
                articles.append(record)
    return runs, articles


def deduplicate_articles(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        article = record["article"]
        key = str(article.get("url") or "").strip()
        if not key:
            key = str(article.get("title") or "").casefold().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def parse_ai_candidates(candidate_dir: Path, target_week: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path in sorted(candidate_dir.glob("????-??-??.md")):
        candidate_date = parse_date(path.stem)
        if not candidate_date or week_label(candidate_date) != target_week:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 6 or cells[0] in {"Candidate", "---"}:
                continue
            try:
                confidence = float(cells[2])
            except ValueError:
                continue
            candidates.append(
                {
                    "term": cells[0],
                    "category": cells[1],
                    "confidence": confidence,
                    "add": cells[3].casefold() == "yes",
                    "date": candidate_date.isoformat(),
                }
            )
    return candidates


def aggregate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        term = candidate["term"]
        current = aggregated.setdefault(
            term,
            {
                "term": term,
                "categories": set(),
                "max_confidence": 0.0,
                "add_count": 0,
                "total_count": 0,
                "dates": set(),
            },
        )
        current["categories"].add(candidate["category"])
        current["max_confidence"] = max(current["max_confidence"], candidate["confidence"])
        current["add_count"] += int(candidate["add"])
        current["total_count"] += 1
        current["dates"].add(candidate["date"])
    return sorted(
        aggregated.values(),
        key=lambda item: (-item["add_count"], -item["max_confidence"], item["term"]),
    )


def grouped_runs(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        result[str(run.get("runDate"))].append(run)
    return result


def build_weekly_report(
    label: str,
    runs: list[dict[str, Any]],
    articles: list[dict[str, Any]],
    unique_articles: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    runs_by_date = grouped_runs(runs)
    articles_by_date = Counter(str(record.get("runDate")) for record in articles)
    reported_count = sum(int(run.get("articleCount") or 0) for run in runs)
    topic_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    source_noise_counts: Counter[str] = Counter()
    for record in unique_articles:
        article = record["article"]
        source = infer_source(article)
        source_counts[source] += 1
        if noise_reasons(article):
            source_noise_counts[source] += 1
        for topic in article_topics(article):
            topic_counts[topic] += 1

    dates = sorted(runs_by_date)
    daily_rows: list[list[object]] = []
    for run_date in dates:
        day_runs = runs_by_date[run_date]
        keywords = sorted(
            {
                str(keyword)
                for run in day_runs
                for keyword in run.get("keywords", [])
                if str(keyword).strip()
            }
        )
        daily_rows.append(
            [
                run_date,
                ", ".join(keywords) or "-",
                sum(int(run.get("articleCount") or 0) for run in day_runs),
                articles_by_date[run_date],
            ]
        )

    topic_rows = [[topic, count] for topic, count in topic_counts.most_common()]
    source_rows = [
        [source, count, source_noise_counts[source]]
        for source, count in source_counts.most_common(10)
    ]
    candidate_rows = [
        [
            candidate["term"],
            ", ".join(sorted(candidate["categories"])),
            f"{candidate['max_confidence']:.2f}",
            f"{candidate['add_count']}/{candidate['total_count']}",
            ", ".join(sorted(candidate["dates"])),
        ]
        for candidate in candidates
        if candidate["add_count"]
    ]

    lines = [
        f"# Weekly Trend Report - {label}",
        "",
        "## Overview",
        "",
        f"- 対象日: {dates[0]} から {dates[-1]}" if dates else "- 対象日: -",
        f"- 日次実行数: {len(runs)}",
        f"- ワークフロー報告の記事数合計: {reported_count}",
        f"- 分析用に保存された記事数: {len(articles)}",
        f"- 週内の重複を除いた記事数: {len(unique_articles)}",
        "",
        "## Daily Volume",
        "",
        *format_table(["Date", "Keywords", "Reported", "Archived"], daily_rows),
        "",
        "## Topic Signals",
        "",
        *format_table(["Topic", "Unique Articles"], topic_rows),
        "",
        "## Source Distribution",
        "",
        *format_table(["Source", "Unique Articles", "Noise Candidates"], source_rows),
        "",
        "## AI-selected Follow-up Keywords",
        "",
        *format_table(["Candidate", "Category", "Max Confidence", "Add Decisions", "Seen On"], candidate_rows),
        "",
        "## Interpretation Notes",
        "",
        "- 件数は取得記事の量を示し、話題の人気・閲覧数・SNS反応を直接示すものではありません。",
        "- トピック分類はタイトルと抜粋に含まれる語によるルールベース判定です。",
        "- 単発投稿・切り抜き・ゲーム攻略は、記事数に含めつつノイズ候補として別集計しています。",
        "",
    ]
    return "\n".join(lines)


def build_keyword_quality_report(
    label: str,
    runs: list[dict[str, Any]],
    unique_articles: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    keywords = sorted(
        {
            str(keyword)
            for run in runs
            for keyword in run.get("keywords", [])
            if str(keyword).strip()
        }
    )
    keyword_rows: list[list[object]] = []
    for keyword in keywords:
        matched = sum(keyword.casefold() in article_text(record["article"]).casefold() for record in unique_articles)
        total = len(unique_articles)
        rate = f"{(matched / total * 100):.1f}%" if total else "0.0%"
        keyword_rows.append([keyword, matched, total, rate])

    source_counts: Counter[str] = Counter()
    source_noise_counts: Counter[str] = Counter()
    noise_reason_counts: Counter[str] = Counter()
    noisy_articles = 0
    for record in unique_articles:
        article = record["article"]
        source = infer_source(article)
        source_counts[source] += 1
        reasons = noise_reasons(article)
        if reasons:
            noisy_articles += 1
            source_noise_counts[source] += 1
            noise_reason_counts.update(reasons)

    source_rows = [
        [source, count, source_noise_counts[source], f"{(source_noise_counts[source] / count * 100):.1f}%"]
        for source, count in source_counts.most_common(10)
    ]
    noise_rows = [[reason, count] for reason, count in noise_reason_counts.most_common()]
    candidate_rows: list[list[object]] = []
    for candidate in candidates:
        if candidate["add_count"]:
            action = "追加候補"
        elif candidate["max_confidence"] >= 0.70:
            action = "要確認"
        else:
            action = "追加しない"
        candidate_rows.append(
            [
                candidate["term"],
                ", ".join(sorted(candidate["categories"])),
                f"{candidate['max_confidence']:.2f}",
                action,
                ", ".join(sorted(candidate["dates"])),
            ]
        )

    noise_rate = f"{(noisy_articles / len(unique_articles) * 100):.1f}%" if unique_articles else "0.0%"
    lines = [
        f"# Keyword Quality Report - {label}",
        "",
        "## Query Coverage",
        "",
        "記事タイトル・抜粋に検索語が文字列として現れる割合です。検索語ごとの実際のヒット元は保存していないため、関連度の厳密な指標ではありません。",
        "",
        *format_table(["Keyword", "Literal Matches", "Unique Articles", "Match Rate"], keyword_rows),
        "",
        "## Noise Quality",
        "",
        f"- ノイズ候補: {noisy_articles}/{len(unique_articles)} 件 ({noise_rate})",
        "",
        *format_table(["Source", "Unique Articles", "Noise Candidates", "Noise Rate"], source_rows),
        "",
        "## Noise Reasons",
        "",
        *format_table(["Reason", "Articles"], noise_rows),
        "",
        "## AI Candidate Review",
        "",
        *format_table(["Candidate", "Category", "Max Confidence", "Recommended Action", "Seen On"], candidate_rows),
        "",
        "## Recommended Operation",
        "",
        "- `追加候補` は、検索語へ反映する前に対象範囲とノイズ量を確認します。",
        "- 単発配信・切り抜き・攻略記事の比率が高い場合は、媒体や除外語のルールを追加します。",
        "- このレポートは既定キーワードを自動変更しません。手動変更は config/keywords.json を編集して行います。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate weekly trend and keyword-quality Markdown reports."
    )
    parser.add_argument("--week", help="ISO week such as 2026-W29. Defaults to the latest saved week.")
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=PROJECT_ROOT / "content" / "structured-records",
    )
    parser.add_argument(
        "--ai-candidate-dir",
        type=Path,
        default=PROJECT_ROOT / "content" / "ai-keyword-candidates",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "content" / "analysis",
    )
    args = parser.parse_args()

    runs, articles = load_records(args.records_dir)
    dated_runs = [run for run in runs if parse_date(run.get("runDate"))]
    if not dated_runs:
        raise SystemExit(f"No valid run records found in {args.records_dir}")

    available_weeks = sorted({week_label(parse_date(run["runDate"])) for run in dated_runs})
    target_week = args.week or available_weeks[-1]
    if target_week not in available_weeks:
        raise SystemExit(f"No records found for {target_week}; available: {', '.join(available_weeks)}")

    selected_runs = [
        run for run in dated_runs if week_label(parse_date(run["runDate"])) == target_week
    ]
    selected_dates = {str(run["runDate"]) for run in selected_runs}
    selected_articles = [
        record for record in articles if str(record.get("runDate")) in selected_dates
    ]
    unique_articles = deduplicate_articles(selected_articles)
    candidates = aggregate_candidates(parse_ai_candidates(args.ai_candidate_dir, target_week))

    weekly_report = build_weekly_report(
        target_week, selected_runs, selected_articles, unique_articles, candidates
    )
    quality_report = build_keyword_quality_report(
        target_week, selected_runs, unique_articles, candidates
    )

    weekly_dir = args.output_dir / "weekly-reports"
    quality_dir = args.output_dir / "keyword-quality"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    quality_dir.mkdir(parents=True, exist_ok=True)
    weekly_path = weekly_dir / f"weekly-trends-{target_week}.md"
    quality_path = quality_dir / f"keyword-quality-{target_week}.md"
    weekly_path.write_text(weekly_report, encoding="utf-8")
    quality_path.write_text(quality_report, encoding="utf-8")

    print(f"wrote {weekly_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {quality_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())