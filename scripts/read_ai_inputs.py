#!/usr/bin/env python3
"""Read bounded task-specific inputs; persist source-bound references in the project DB."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import article_review_facts as shared
import project_readers as project

ROOT = Path(__file__).resolve().parents[1]
TASKS = ("article-summary", "keyword-extraction", "talent-index", "article-classification", "weekly-report")
BODY_TASKS = {"article-summary", "talent-index", "article-classification"}


def parse_day(value: str) -> date:
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("Dates must use YYYY-MM-DD")
    return day


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{number}: invalid JSON ({exc.msg})") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path.name}:{number}: expected a JSON object")
            records.append(record)
    return records


def path_reference(root: Path, relative: str, role: str) -> dict[str, Any]:
    return {"role": role, "path": relative, "exists": (root / relative).is_file()}


def day_references(root: Path, day: str, task: str) -> list[dict[str, Any]]:
    paths = [
        ("structuredRecords", f"content/structured-records/{day}.jsonl"),
        ("bodyCaptures", f"content/article-body-captures/{day}.jsonl"),
    ]
    if task in BODY_TASKS:
        paths.append(("sharedReviewFacts", f"{shared.DIRECTORY}/{day}.jsonl"))
    if task in {"weekly-report", "talent-index", "article-classification"}:
        paths.append(("articleSummaries", f"content/article-summaries/{day}.md"))
    if task in {"weekly-report", "article-classification"}:
        paths.append(("classificationProposals", f"content/article-classification-proposals/{day}.json"))
    if task == "article-classification":
        paths.append(("talentProposals", f"content/talent-index-proposals/{day}.json"))
    if task in {"keyword-extraction", "weekly-report"}:
        paths.extend([
            ("ruleBasedCandidates", f"content/keyword-candidates/{day}.md"),
            ("aiCandidates", f"content/ai-keyword-candidates/{day}.md"),
        ])
    return [path_reference(root, path, role) for role, path in paths]


def load_day(root: Path, day: str, warnings: list[str], records_dir: Path | None = None, feature: str = "ai-reader") -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if project.source(root, feature) == "project-db" and (records_dir is None or Path(records_dir) == root / "content/structured-records"):
        with project.reader(root) as reader:
            return reader.load_day(day, warnings)
    relative = f"content/structured-records/{day}.jsonl"
    records = read_jsonl(Path(records_dir) / f"{day}.jsonl" if records_dir is not None else root / relative)
    runs = [row for row in records if row.get("recordType") == "run"]
    if len(runs) != 1:
        raise ValueError(f"{relative}: expected exactly one run record, found {len(runs)}")
    run = runs[0]
    if run.get("runDate", day) != day:
        raise ValueError(f"{relative}: runDate does not match requested date")
    articles = []
    for row in records:
        if row.get("recordType") == "run":
            continue
        if row.get("recordType") != "article":
            raise ValueError(f"{relative}: unsupported recordType {row.get('recordType')!r}")
        article = row.get("article")
        if not isinstance(article, dict) or not isinstance(article.get("url"), str) or not article["url"].strip():
            raise ValueError(f"{relative}: article record has no usable URL")
        if row.get("runDate", day) != day:
            raise ValueError(f"{relative}: article runDate does not match requested date")
        articles.append(row)
    declared = run.get("capturedArticleCount", run.get("articleCount"))
    if declared is not None and declared != len(articles):
        warnings.append(f"{day}: declared captured count {declared} differs from {len(articles)} saved article records")
    captures: dict[str, dict[str, Any]] = {}
    capture_path = root / "content/article-body-captures" / f"{day}.jsonl"
    if capture_path.is_file():
        for row in read_jsonl(capture_path):
            url = row.get("originalUrl")
            if not isinstance(url, str) or not url:
                raise ValueError(f"{capture_path.name}: body capture has no originalUrl")
            if url in captures:
                warnings.append(f"{day}: duplicate body capture for {url}; last saved row selected")
            captures[url] = row
    else:
        warnings.append(f"{day}: body-capture file is missing; articles are not_captured, not unavailable")
    return run, articles, captures


def capture_status(capture: dict[str, Any] | None) -> str:
    return "not_captured" if capture is None else str(capture.get("contentStatus") or "unknown")


def content_chunk(capture: dict[str, Any], offset: int, maximum: int) -> dict[str, Any]:
    field, text = shared.selected_content(capture)
    if offset > len(text):
        raise ValueError(f"content-offset {offset} exceeds content length {len(text)}")
    end = min(offset + maximum, len(text))
    return {
        "field": field, "text": text[offset:end], "offset": offset,
        "returnedCharacters": end - offset, "totalCharacters": len(text),
        "nextOffset": end if end < len(text) else None, "complete": end == len(text),
    }


def article_view(day: str, row: dict[str, Any], index: int, capture: dict[str, Any] | None, task: str, content_offset: int, max_content_chars: int) -> dict[str, Any]:
    article = row["article"]
    result = {
        "runDate": day, "articleIndex": row.get("articleIndex", index),
        "url": article["url"], "title": article.get("title", ""),
        "publishedAt": article.get("publishedAt", ""), "source": article.get("source", ""),
        "contentStatus": capture_status(capture),
    }
    for key in ("matchedSearchKeywords", "matchedRssKeywords", "keywordMatchMethod"):
        if key in row or key in article:
            result[key] = row.get(key, article.get(key))
    if task in {"keyword-extraction", "talent-index", "weekly-report"}:
        result["rssExcerpt"] = article.get("excerpt", "")
    if "_project" in row:
        result["databaseReferences"] = dict(row["_project"])
    if capture is not None:
        if "_project" in capture:
            result.setdefault("databaseReferences", {})["capture"] = capture["_project"]
        for key in ("articleKey", "resolvedUrl", "sourceDomain", "contentType", "contentCompleteness", "failureReason", "fetchedAt", "extractionMethod", "extractionScope"):
            if key in capture:
                result[key] = capture[key]
        if task in BODY_TASKS:
            result["content"] = content_chunk(capture, content_offset, max_content_chars)
    elif content_offset:
        raise ValueError("No saved capture exists for the requested content continuation")
    return result


def build_payload(root: Path, run_date: str, task: str, offset: int = 0, limit: int = 20, article_url: str | None = None, content_offset: int = 0, max_content_chars: int = 6000, include_body: bool = False) -> dict[str, Any]:
    if task in BODY_TASKS and project.source(root, "ai-reader") == "project-db":
        from ai_input_minimization import build_payload as compact
        return compact(root, run_date, task, offset, limit, article_url, content_offset, max_content_chars, include_body)
    requested = parse_day(run_date)
    if task not in TASKS:
        raise ValueError(f"Unknown task: {task}")
    if offset < 0 or content_offset < 0 or limit < 1 or max_content_chars < 1:
        raise ValueError("offsets must be non-negative; limit and max-content-chars must be positive")
    if content_offset and (not article_url or task not in BODY_TASKS):
        raise ValueError("content-offset requires article-url and a daily body-review task")
    first = requested - timedelta(days=requested.weekday()) if task == "weekly-report" else requested
    days = [(first + timedelta(days=i)).isoformat() for i in range((requested - first).days + 1)]
    feature = "weekly" if task == "weekly-report" else "ai-reader"
    adopted = project.source(root, feature)
    missing = [day for day in days if not project.has_day(root, day, feature)]
    if missing and task != "weekly-report":
        raise ValueError(f"Missing structured records: content/structured-records/{run_date}.jsonl")
    warnings: list[str] = []
    review_index, known_review_urls = shared.load_reviews(root, run_date, warnings, feature=feature) if task in BODY_TASKS else ({}, set())
    review_policy = shared.policy_hash(root) if task in BODY_TASKS else None
    entries = []
    runs = []
    references = []
    for day in days:
        references.extend(day_references(root, day, task))
        if day in missing:
            continue
        run, articles, captures = load_day(root, day, warnings, feature=feature)
        runs.append(run)
        for index, row in enumerate(articles, 1):
            entries.append((day, row, index, captures.get(row["article"]["url"])))
    total = len(entries)
    unique_urls = len({entry[1]["article"]["url"] for entry in entries})
    if task != "weekly-report" and unique_urls != total:
        warnings.append("Duplicate article URLs are preserved; use runDate and articleIndex to account for every saved record")
    selected = [entry for entry in entries if not article_url or entry[1]["article"]["url"] == article_url]
    if article_url and not selected:
        raise ValueError("article-url was not found in the requested structured records")
    if offset > len(selected):
        raise ValueError(f"offset {offset} exceeds matching article count {len(selected)}")
    page = selected[offset:offset + limit]
    if content_offset and len(page) != 1:
        raise ValueError(
            "Content continuation requires one selected article record; "
            "use --limit 1 and --offset to select a position among matching article-url records"
        )
    next_offset = offset + len(page)
    views = []
    for day, row, index, capture in page:
        view = article_view(day, row, index, capture, task, content_offset, max_content_chars)
        if task in BODY_TASKS:
            review = shared.resolve_review(review_index, known_review_urls, day, row["article"], capture, review_policy, task)
            view["reviewInput"] = {"reviewVersion": shared.REVIEW_VERSION,
                                   "inputHash": shared.input_hash(row["article"], capture), "policyHash": review_policy}
            view["sharedReview"] = review
            if review["status"] == "current" and review["taskStatus"] in ("ready", "held") and not (include_body or content_offset):
                view.pop("content", None)
                view["bodyOmitted"] = True
            else:
                view["bodyOmitted"] = False
        views.append(view)
    result: dict[str, Any] = {
        "inputVersion": 1, "task": task, "runDate": run_date, "dataSource": adopted,
        "totalArticles": total, "uniqueUrls": unique_urls, "matchingArticles": len(selected),
        "offset": offset, "returnedArticles": len(page),
        "nextOffset": next_offset if next_offset < len(selected) else None,
        "articles": views,
    }
    if offset == 0 and content_offset == 0:
        if task in BODY_TASKS:
            references.append(path_reference(root, shared.RULES, "sharedReviewRules"))
        if task in {"article-classification", "weekly-report"}:
            references.append(path_reference(root, "config/article-classification-taxonomy.json", "classificationTaxonomy"))
        if task == "keyword-extraction":
            references.append(path_reference(root, "config/keywords.json", "currentKeywordConfig"))
        if task == "weekly-report":
            references.append(path_reference(root, f"content/weekly-reports/{first.isoformat()}.md", "existingWeeklyReport"))
            from weekly_metrics import dated_paths
            feedback = dated_paths(root / "content/article-feedback-instructions", ".md", run_date)
            if feedback:
                references.append(path_reference(root, feedback[-1].relative_to(root).as_posix(), "latestFeedbackInstructions"))
        result["context"] = {
            "runs": runs, "references": references,
            "captureStatusCounts": dict(Counter(capture_status(entry[3]) for entry in entries)),
            "contentPolicy": "Saved capture status is not editorial approval. RSS excerpts and metadata are not verified article bodies. Read content continuations when needed for evidence.",
        }
        if task in BODY_TASKS:
            result["context"]["sharedReviewPolicy"] = "Use current ready facts as source evidence, not final approval. Held means record an unresolved result. Missing, stale, invalid or needs_review requires review. Use --include-body to inspect evidence or reconsider any cached decision."
    if task == "weekly-report":
        result["coverage"] = {
            "weekStart": first.isoformat(), "weekEnd": (first + timedelta(days=6)).isoformat(),
            "coveredThrough": run_date, "availableDates": [day for day in days if day not in missing],
            "missingDates": missing,
            "countBasis": "totalArticles counts saved records across days; uniqueUrls counts distinct original URLs. Feedback exclusions are not applied by this reader.",
        }
    if warnings:
        result["warnings"] = warnings
    return result


def inventory_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Navigation only. Omitted bodies/facts must still be read for review."""
    result = {key: value for key, value in payload.items() if key not in ("articles", "context")}
    if payload.get("inputVersion") == 2:
        result["view"] = "inventory"
        result["articles"] = [{k:v for k,v in a.items() if k in ("ref","title","state","contentStatus")} for a in payload["articles"]]
        return result
    result["view"] = "inventory"
    result["reviewEvidenceOmitted"] = True
    result["nextAction"] = "Read --view detail --offset recordOffset --limit 1 for evidence. This inventory is not a completed review."
    articles = []
    for position, article in enumerate(payload["articles"], payload["offset"]):
        row = {key: article[key] for key in ("runDate", "articleIndex", "publishedAt", "sourceDomain", "contentStatus") if key in article}
        row["recordOffset"] = position
        for field, limit in (("title", 240), ("failureReason", 200)):
            if field in article:
                value = str(article[field])
                row[field] = value[:limit]
                if len(value) > limit:
                    row[field + "Truncated"] = True
        review = article.get("sharedReview", {})
        row["sharedReview"] = {key: review[key] for key in ("status", "taskStatus") if key in review}
        articles.append(row)
    result["articles"] = articles
    if "context" in payload:
        context = payload["context"]
        result["context"] = {key: context[key] for key in ("captureStatusCounts", "references") if key in context}
        result["context"]["runs"] = [
            {**{key: run[key] for key in ("runDate", "generatedAt", "period", "articleCount") if key in run},
             "keywordCount": len(run.get("keywords", []))}
            for run in context.get("runs", [])
        ]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date", required=True, help="Archive date in YYYY-MM-DD (JST)")
    parser.add_argument("--task", choices=TASKS, required=True)
    parser.add_argument("--offset", type=int, default=0, help="Zero-based saved article position, not articleIndex")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--article-url", help="Select an exact original URL for source inspection or continuation")
    parser.add_argument("--content-offset", type=int, default=0, help="Character offset in one selected saved body")
    parser.add_argument("--max-content-chars", type=int, default=6000)
    parser.add_argument("--include-body", action="store_true", help="Read saved body for an unfinished task; saved/held tasks remain excluded")
    parser.add_argument("--weekly-articles", action="store_true", help="Explicitly page raw, pre-exclusion weekly articles instead of reading fixed metrics")
    parser.add_argument("--as-of", help="Weekly evaluation cutoff; default is run-date")
    parser.add_argument("--view", choices=("detail", "inventory"), default="detail", help="inventory: bounded navigation without URLs, bodies or evidence; detail: full review input")
    parser.add_argument("--pretty", action="store_true", help="Indent JSON; default is compact UTF-8 JSON")
    parser.add_argument("--article-ref")
    parser.add_argument("--reference-kind", choices=("detail","body","facts","evidence","source"), default="body")
    parser.add_argument("--ids", nargs="+")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.view == "inventory" and (args.include_body or args.content_offset or (args.task == "weekly-report" and not args.weekly_articles)):
            raise ValueError("inventory cannot read body continuations or weekly metrics; use --view detail")
        if args.task == "weekly-report" and not args.weekly_articles:
            if args.offset or args.article_url or args.content_offset:
                raise ValueError("Use --weekly-articles for article selection or pagination")
            from weekly_metrics import read_weekly_input
            payload = read_weekly_input(ROOT, args.run_date, args.as_of)
        else:
            if args.as_of:
                raise ValueError("--as-of is for the default weekly metrics view only")
            if args.weekly_articles and args.task != "weekly-report":
                raise ValueError("--weekly-articles requires --task weekly-report")
            if args.article_ref:
                from ai_input_minimization import additional
                payload = additional(ROOT,args.run_date,args.task,args.article_ref,args.reference_kind,args.content_offset,args.max_content_chars,args.ids)
            else:
                payload = build_payload(ROOT, args.run_date, args.task, args.offset, args.limit, args.article_url, args.content_offset, args.max_content_chars, args.include_body)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.view == "inventory" and not args.article_ref:
        payload = inventory_payload(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
