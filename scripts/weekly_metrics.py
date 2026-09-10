"""Deterministic week-to-date numbers, provenance and generated report blocks."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import article_review_facts as shared
from article_feedback_snapshot import REASONS, atomic_text
import generate_analysis_reports as legacy

VERSION = 1
JST = timezone(timedelta(hours=9))
START = "<!-- weekly-metrics:start -->"
END = "<!-- weekly-metrics:end -->"


def day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use YYYY-MM-DD dates")
    return parsed


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def dated_paths(directory, suffix, through, since=None):
    result = []
    for path in directory.glob("*" + suffix):
        try:
            label = day(path.stem).isoformat()
        except ValueError:
            continue
        if label <= through and (since is None or label >= since):
            result.append(path)
    return sorted(result)


def ratio(count, denominator):
    return {"count": count, "denominator": denominator,
            "percent": round(100 * count / denominator, 2) if denominator else None}


def normal(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def host(value):
    value = str(value or "").strip()
    return (urlparse(value if "://" in value else "//" + value).hostname or "").casefold()


class Inputs:
    def __init__(self, root):
        self.root = root
        self.files = {}

    def add(self, path, role):
        path = Path(path)
        try:
            name = path.relative_to(self.root).as_posix()
        except ValueError:
            name = str(path.resolve())
        data = path.read_bytes() if path.is_file() else None
        self.files[name] = {"path": name, "role": role, "exists": data is not None,
                            "sha256": hashlib.sha256(data).hexdigest() if data is not None else None}
        return path

    def json(self, path, role):
        self.add(path, role)
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def manifest(self):
        return [self.files[key] for key in sorted(self.files)]


def feedback_state(root, as_of, inputs):
    directory = root / "content/article-feedback-instructions"
    snapshots = dated_paths(directory, ".json", as_of)
    guides = dated_paths(directory, ".md", as_of)
    for path in guides[-1:]:
        inputs.add(path, "feedbackGuidance")
    if not snapshots:
        return {"status": "missing", "snapshotDate": None, "applied": False}, {}
    path = snapshots[-1]
    snapshot = inputs.json(path, "feedbackSnapshot")
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1 or snapshot.get("snapshotDate") != path.stem:
        raise ValueError(f"Invalid feedback snapshot: {path}")
    if snapshot.get("complete") is not True:
        return {"status": "incomplete", "snapshotDate": path.stem, "applied": False}, {}
    if guides and guides[-1].stem > path.stem:
        return {"status": "stale", "snapshotDate": path.stem, "applied": False}, {}
    if guides and guides[-1].stem == path.stem:
        actual = hashlib.sha256(guides[-1].read_bytes()).hexdigest()
        if actual != snapshot.get("instructionHash"):
            return {"status": "stale", "snapshotDate": path.stem, "applied": False}, {}
    if stamp(snapshot.get("generatedAt")).astimezone(JST).date().isoformat() != path.stem:
        raise ValueError("Feedback snapshot timestamp/date mismatch")
    if not isinstance(snapshot.get("feedback"), list):
        raise ValueError("Feedback snapshot needs the complete feedback array")
    selected = {}
    for row in snapshot["feedback"]:
        if not isinstance(row, dict) or not isinstance(row.get("articleUrl"), str) or not row["articleUrl"]:
            raise ValueError("Feedback row has no articleUrl")
        if row.get("decision") not in ("approved", "rejected"):
            raise ValueError("Invalid feedback decision")
        if row["decision"] == "rejected" and row.get("reasonCode") not in REASONS:
            raise ValueError("Invalid rejection reason")
        if row["decision"] == "rejected" and row.get("reasonCode") == "suspicious_source" and not (host(row.get("sourceDomain")) or normal(row.get("publisherLabel"))):
            raise ValueError("Suspicious source feedback needs a domain or publisher")
        reviewed = stamp(row.get("reviewedAt"))
        if reviewed > stamp(snapshot.get("generatedAt")):
            raise ValueError("Feedback row is newer than its snapshot")
        url = row["articleUrl"]
        previous = selected.get(url)
        if previous and reviewed == previous[0] and row != previous[1]:
            raise ValueError("Conflicting feedback at the same timestamp")
        if previous is None or reviewed >= previous[0]:
            selected[url] = (reviewed, row)
    return {"status": "complete", "snapshotDate": path.stem, "applied": True,
            "snapshotAgeDays": (day(as_of) - day(path.stem)).days}, {url: entry[1] for url, entry in selected.items()}


def classification_rows(directory, as_of, inputs):
    selected = {}
    for path in dated_paths(directory, ".json", as_of):
        payload = inputs.json(path, "classificationProposals")
        if not isinstance(payload, dict) or not isinstance(payload.get("classifications"), list):
            raise ValueError(f"Invalid classification proposal: {path}")
        for row in payload["classifications"]:
            if not isinstance(row, dict) or not isinstance(row.get("article_url"), str):
                raise ValueError(f"Invalid classification row: {path}")
            # Last dated proposal wins; duplicate URLs within a proposal are errors.
        urls = [row["article_url"] for row in payload["classifications"]]
        if len(urls) != len(set(urls)):
            raise ValueError(f"Duplicate classification URL: {path}")
        for row in payload["classifications"]:
            selected[row["article_url"]] = (row, path.stem)
    return selected


def valid_classification(row, taxonomy, source_hash, as_of):
    try:
        if row.get("classification_method") not in ("ai_review", "manual_review"):
            return False
        if row.get("inputHash") is not None and row["inputHash"] != source_hash:
            return False
        if stamp(row.get("classified_at")).astimezone(JST).date().isoformat() > as_of:
            return False
        for field, group in (("article_type", "articleTypes"), ("primary_category", "categories"), ("relevance", "relevance")):
            if row.get(field) not in {item["id"] for item in taxonomy[group]}:
                return False
        confidence = row.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            return False
        if not isinstance(row.get("evidence_text"), str) or not row["evidence_text"].strip():
            return False
        secondary = row.get("secondary_categories_json", [])
        if not isinstance(secondary, list) or any(not isinstance(item, str) for item in secondary):
            return False
        return (len(secondary) <= taxonomy.get("rules", {}).get("maximumSecondaryCategories", 3)
                and len(secondary) == len(set(secondary)) and row["primary_category"] not in secondary
                and set(secondary) <= {item["id"] for item in taxonomy["categories"]})
    except (KeyError, ValueError, TypeError):
        return False


def build_metrics(root, through, as_of=None, records_dir=None, candidate_dir=None, classification_dir=None):
    from read_ai_inputs import capture_status, load_day
    root = Path(root)
    cutoff = day(through)
    as_of = as_of or through
    if day(as_of) < cutoff:
        raise ValueError("as-of cannot precede through")
    monday = cutoff - timedelta(days=cutoff.weekday())
    dates = [(monday + timedelta(days=i)).isoformat() for i in range((cutoff - monday).days + 1)]
    records_dir = Path(records_dir or root / "content/structured-records")
    candidate_dir = Path(candidate_dir or root / "content/ai-keyword-candidates")
    classification_dir = Path(classification_dir or root / "content/article-classification-proposals")
    inputs, warnings = Inputs(root), []
    # Code and rule versions participate in the snapshot fingerprint.
    for relative in ("scripts/weekly_metrics.py", "scripts/generate_analysis_reports.py", "scripts/read_ai_inputs.py",
                     "scripts/article_review_facts.py", "scripts/article_feedback_snapshot.py",
                     "docs/ai-rules/weekly-report.md", *shared.POLICY_FILES):
        inputs.add(root / relative, "implementationOrPolicy")
    tax_path = root / "config/article-classification-taxonomy.json"
    taxonomy = inputs.json(tax_path, "taxonomy") if tax_path.is_file() else {"articleTypes": [], "categories": [], "relevance": []}
    inputs.add(tax_path, "taxonomy")
    alias_path = root / "config/keyword-aliases.json"
    alias_config = inputs.json(alias_path, "aliases") if alias_path.is_file() else {"keywords": []}
    inputs.add(alias_path, "aliases")
    aliases = {}
    for entry in alias_config.get("keywords", []):
        for value in [entry["label"], *entry.get("aliases", [])]:
            key = normal(value)
            if key in aliases and aliases[key] != entry["label"]:
                raise ValueError(f"Ambiguous entity alias: {value}")
            aliases[key] = entry["label"]
    feedback, feedback_by_url = feedback_state(root, as_of, inputs)
    if not feedback["applied"]:
        warnings.append("採否の完全なJSONが未取得・不完全・古い状態です。対象件数は採否未反映の暫定値です。")
    classifications = classification_rows(classification_dir, as_of, inputs)
    for path in dated_paths(root / shared.DIRECTORY, ".jsonl", as_of):
        inputs.add(path, "sharedReviews")
    review_index, known_urls = shared.load_reviews(root, as_of, warnings)
    for key, (record, _, _) in list(review_index.items()):
        try:
            if stamp(record.get("reviewedAt")).astimezone(JST).date().isoformat() > as_of:
                del review_index[key]
        except (ValueError, TypeError):
            pass  # resolve_review will report an invalid record.
    policy = shared.policy_hash(root)
    selected, variants, daily, runs = {}, defaultdict(set), [], []
    raw_count = reported = 0
    for label in dates:
        path = inputs.add(records_dir / (label + ".jsonl"), "structuredRecords")
        capture_path = inputs.add(root / "content/article-body-captures" / (label + ".jsonl"), "bodyCaptures")
        inputs.add(root / "content/article-summaries" / (label + ".md"), "dailySummaryReference")
        if not path.is_file():
            daily.append({"date": label, "available": False, "reported": None, "archived": None, "uniqueUrls": None})
            continue
        # Keep the strict reader's run/row validation and capture joining.
        run, articles, captures = load_day(root, label, warnings, records_dir=records_dir)
        count = run.get("articleCount", len(articles))
        if type(count) is not int or count < 0:
            raise ValueError(f"{label}: invalid reported article count")
        runs.append(run)
        reported += count
        raw_count += len(articles)
        daily.append({"date": label, "available": True, "reported": count,
                      "archived": len(articles), "uniqueUrls": len({r["article"]["url"] for r in articles})})
        for ordinal, row in enumerate(articles):
            article = row["article"]
            url, capture = article["url"], captures.get(article["url"])
            source_hash = shared.input_hash(article, capture)
            variants[url].add(source_hash)
            selected[url] = (label, row, capture, source_hash)
    blocked_hosts, blocked_labels = set(), set()
    for row in feedback_by_url.values():
        if row["decision"] == "rejected" and row["reasonCode"] == "suspicious_source":
            domain, publisher = host(row.get("sourceDomain")), normal(row.get("publisherLabel"))
            if domain: blocked_hosts.add(domain)
            if publisher: blocked_labels.add(publisher)
    excluded = Counter()
    capture_counts, review_states = Counter(), Counter()
    task_counts = {task: Counter() for task in shared.TASKS}
    type_counts, category_counts, relevance_counts, source_counts = Counter(), Counter(), Counter(), Counter()
    topic_counts, noise_counts, keyword_counts = Counter(), Counter(), Counter()
    entity_urls, entity_days = defaultdict(set), defaultdict(set)
    ledger, eligible_rows = [], []
    classified = unbound = invalid_classifications = body_ready = 0
    keywords = sorted({str(keyword) for run in runs for keyword in run.get("keywords", [])})
    for url, (label, row, capture, source_hash) in sorted(selected.items()):
        article = row["article"]
        source = legacy.infer_source(article)
        domain = host((capture or {}).get("sourceDomain") or (capture or {}).get("resolvedUrl") or url)
        evaluation = feedback_by_url.get(url, {})
        reasons = []
        if evaluation.get("decision") == "rejected":
            reasons.append("url:" + evaluation["reasonCode"])
        if domain in blocked_hosts or normal(source) in blocked_labels:
            reasons.append("source:suspicious_source")
        trace = {"url": url, "runDate": label, "articleIndex": row.get("articleIndex"),
                 "inputHash": source_hash, "excludedReasons": reasons, "source": source}
        ledger.append(trace)
        if reasons:
            excluded.update(reasons)
            continue
        eligible_rows.append(row)
        source_counts[source] += 1
        capture_counts[capture_status(capture)] += 1
        review = shared.resolve_review(review_index, known_urls, label, article, capture, policy, "article-summary")
        review_states[review["status"]] += 1
        record = review.get("record", {})
        for task in shared.TASKS:
            task_counts[task][record.get("taskStatus", {}).get(task, "needs_review")] += 1
        ready = review["status"] == "current" and review["taskStatus"] == "ready" and record.get("basis") == "body"
        body_ready += int(ready)
        trace["reviewStatus"] = review["status"]
        trace["reviewPath"] = review.get("path")
        if ready:
            for entity in record["entities"]:
                name = aliases.get(normal(entity["name"]), unicodedata.normalize("NFKC", entity["name"]).strip())
                key = (entity["kind"], name)
                entity_urls[key].add(url)
                entity_days[key].add(label)
        proposed = classifications.get(url)
        if proposed:
            classification, proposal_date = proposed
            valid = valid_classification(classification, taxonomy, source_hash, as_of)
            trace["classification"] = {"proposalDate": proposal_date, "valid": valid,
                                       "inputBound": classification.get("inputHash") is not None}
            if valid:
                classified += 1
                unbound += int(classification.get("inputHash") is None)
                type_counts[classification["article_type"]] += 1
                category_counts[classification["primary_category"]] += 1
                relevance_counts[classification["relevance"]] += 1
            else:
                invalid_classifications += 1
        topic_counts.update(legacy.article_topics(article))
        noise_counts.update(legacy.noise_reasons(article))
        text = "\n".join(str(article.get(key) or "") for key in ("title", "excerpt")).casefold()
        keyword_counts.update(keyword for keyword in keywords if keyword.casefold() in text)
    eligible = len(eligible_rows)
    candidates = []
    for path in dated_paths(candidate_dir, ".md", through, monday.isoformat()):
        inputs.add(path, "keywordCandidates")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.startswith("|"):
                continue
            cells = [value.strip() for value in line.strip().strip("|").split("|")]
            if cells[0] == "Candidate" or cells[0].startswith("---"):
                continue
            if len(cells) < 6:
                raise ValueError(f"Incomplete keyword candidate row: {path}")
            confidence = float(cells[2])
            if not math.isfinite(confidence) or not 0 <= confidence <= 1 or cells[3].casefold() not in ("yes", "no"):
                raise ValueError(f"Invalid keyword candidate decision: {path}")
            candidates.append({"term": cells[0], "category": cells[1], "confidence": confidence,
                               "add": cells[3].casefold() == "yes", "date": path.stem})
    candidate_rows = legacy.aggregate_candidates(candidates)
    candidate_rows = [{**row, "categories": sorted(row["categories"]), "dates": sorted(row["dates"])} for row in candidate_rows]
    if not policy:
        warnings.append("共通確認ルールが揃わないため、本文確認記録を再利用していません。")
    if unbound:
        warnings.append("入力ハッシュのない既存分類を提案件数として集計しています。分類手法ラベルだけでは本文確認済みと判定しません。")
    payload = {
        "metricsVersion": VERSION, "week": legacy.week_label(cutoff), "timezone": "Asia/Tokyo",
        "weekStart": monday.isoformat(), "weekEnd": (monday + timedelta(days=6)).isoformat(),
        "coveredThrough": through, "reviewAsOf": as_of,
        "status": "ready" if feedback["applied"] else "provisional",
        "coverage": {"availableDates": [r["date"] for r in daily if r["available"]],
                     "missingDates": [r["date"] for r in daily if not r["available"]]},
        "feedback": feedback,
        "counts": {"reportedArticles": reported, "archivedRecords": raw_count, "uniqueArticles": len(selected),
                   "duplicateRecords": raw_count - len(selected), "changedInputUrls": sum(len(v) > 1 for v in variants.values()),
                   "excludedArticles": len(selected) - eligible, "eligibleArticles": eligible,
                   "classifiedArticles": classified, "unclassifiedArticles": eligible - classified,
                   "invalidClassificationArticles": invalid_classifications, "unboundClassifications": unbound,
                   "bodyReviewReadyArticles": body_ready, "videoMetadataSummaries": None},
        "daily": daily, "exclusionReasons": dict(sorted(excluded.items())),
        "captureStatuses": dict(sorted(capture_counts.items())), "sharedReviewStatuses": dict(sorted(review_states.items())),
        "taskStatuses": {task: {status: counts[status] for status in ("ready", "held", "needs_review")} for task, counts in task_counts.items()},
        "classificationCoverage": ratio(classified, eligible),
        "byArticleType": {k: ratio(v, classified) for k, v in sorted(type_counts.items())},
        "byCategory": {k: ratio(v, classified) for k, v in sorted(category_counts.items())},
        "byRelevance": {k: ratio(v, classified) for k, v in sorted(relevance_counts.items())},
        "bySource": {k: ratio(v, eligible) for k, v in sorted(source_counts.items())},
        "literalKeywordCoverage": {k: ratio(keyword_counts[k], eligible) for k in keywords},
        "supplementaryTopicSignals": dict(sorted(topic_counts.items())), "noiseReasonSignals": dict(sorted(noise_counts.items())),
        "entities": [{"kind": k[0], "name": k[1], "articles": len(urls), "representativeDates": sorted(entity_days[k])}
                     for k, urls in sorted(entity_urls.items(), key=lambda pair: (-len(pair[1]), pair[0]))],
        "keywordCandidates": candidate_rows, "articleDecisions": ledger,
        "definitions": {
            "identity": "Exact original URL; latest collected date and last saved row win, without mixing captures from other dates.",
            "eligible": "Unique URLs minus explicit URL rejections and exact source-domain/publisher rejections; provisional if feedback is not applied.",
            "classification": "Valid proposal rows; shares use classifiedArticles, coverage uses eligibleArticles. Method labels are not proof of semantic review.",
            "bodyReviewReady": "Current source/policy-bound shared body review with article-summary ready; not a count of written summaries.",
            "videoMetadataSummaries": "Not measured: capture metadata status alone does not prove a video summary was written.",
            "entities": "Distinct eligible URLs from body-review-ready facts; exact NFKC/alias normalization only. Dates are selected representative collection dates.",
            "exclusionReasons": "One URL may have multiple reasons; reason counts are not additive.",
            "time": "Collection dates stop at coveredThrough; evaluation files/timestamps stop at reviewAsOf end of JST day. Naive legacy review timestamps are interpreted as UTC.",
        },
        "inputs": inputs.manifest(), "warnings": sorted(set(warnings)),
    }
    validate_metrics(payload)
    payload["snapshotId"] = shared.digest(payload)
    return payload


def validate_metrics(metrics):
    c = metrics["counts"]
    checks = [c["archivedRecords"] == c["uniqueArticles"] + c["duplicateRecords"],
              c["uniqueArticles"] == c["excludedArticles"] + c["eligibleArticles"],
              c["eligibleArticles"] == c["classifiedArticles"] + c["unclassifiedArticles"],
              sum(metrics["captureStatuses"].values()) == c["eligibleArticles"],
              sum(metrics["sharedReviewStatuses"].values()) == c["eligibleArticles"]]
    checks += [sum(row["count"] for row in metrics[key].values()) == c["classifiedArticles"]
               for key in ("byCategory", "byArticleType", "byRelevance")]
    checks += [sum(counts.values()) == c["eligibleArticles"] for counts in metrics["taskStatuses"].values()]
    if not all(checks):
        raise ValueError("Weekly count invariant failed")


def table(headers, rows):
    def cell(value):
        return str(value if value is not None else "未計測").replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
                      *["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]])


def render_block(metrics):
    c = metrics["counts"]
    lines = [START, "## 確定集計（自動生成・直接編集しない）", "",
             f"- Snapshot: `{metrics['snapshotId']}`",
             f"- 対象期間: {metrics['weekStart']} ～ {metrics['coveredThrough']} (JST)",
             f"- 評価参照日: {metrics['reviewAsOf']}",
             f"- 状態: {'採否反映済み' if metrics['feedback']['applied'] else '暫定・採否未反映'}",
             "- 欠損日: " + (", ".join(metrics["coverage"]["missingDates"]) or "なし"), "",
             table(["指標", "件数"], [[key, value] for key, value in c.items()]), "",
             table(["日付", "収集データあり", "報告件数", "保存行数", "URL数"],
                   [[r["date"], r["available"], r["reported"], r["archived"], r["uniqueUrls"]] for r in metrics["daily"]]), ""]
    for label, key in (("主カテゴリ（分類提案ありを分母）", "byCategory"), ("記事種別（同分母）", "byArticleType"),
                       ("関連度（同分母）", "byRelevance"), ("媒体・上位20（対象記事を分母）", "bySource")):
        rows = sorted(metrics[key].items(), key=lambda row: (-row[1]["count"], row[0]))
        if key == "bySource": rows = rows[:20]
        lines += ["### " + label, "", table(["項目", "件数", "分母", "%"],
                  [[k, v["count"], v["denominator"], v["percent"]] for k, v in rows]), ""]
    lines += ["### 確認状態（対象記事）", "",
              table(["取得状態", "件数"], sorted(metrics["captureStatuses"].items())), "",
              table(["工程", "ready", "held", "needs_review"],
                    [[task, value["ready"], value["held"], value["needs_review"]] for task, value in metrics["taskStatuses"].items()]), "",
              "### 確認済み人物・団体等（上位20、URL単位）", "",
              table(["種別", "名前", "記事数"], [[r["kind"], r["name"], r["articles"]] for r in metrics["entities"][:20]]), "",
              "### 集計上の注意", ""]
    lines += ["- " + value for value in metrics["definitions"].values()]
    lines += ["- " + value for value in metrics["warnings"]]
    return "\n".join([*lines, END])


def render_quality(metrics):
    rows = metrics["literalKeywordCoverage"]
    return "\n".join([f"# Keyword Quality Report - {metrics['week']}", "",
                      f"Snapshot: `{metrics['snapshotId']}`", "", "タイトル・RSS抜粋の文字列一致。意味的な関連性とは別。", "",
                      table(["Keyword", "Matches", "Eligible", "%"], [[k, v["count"], v["denominator"], v["percent"]] for k, v in rows.items()]), "",
                      "## 候補判断（記事数ではなく保存された候補行の採否）", "",
                      table(["Candidate", "Add Decisions", "Total Decisions", "Days"],
                            [[r["term"], r["add_count"], r["total_count"], ", ".join(r["dates"])] for r in metrics["keywordCandidates"]]), "",
                      "## 補助話題シグナル（重複計上あり、分類ではない）", "",
                      table(["Signal", "Articles"], sorted(metrics["supplementaryTopicSignals"].items())), ""])


def output_paths(root, metrics, output_dir=None):
    directory = Path(output_dir or root / "content/analysis")
    week = metrics["week"]
    return (directory / "weekly-metrics" / f"{week}.json",
            directory / "weekly-reports" / f"weekly-trends-{week}.md",
            directory / "keyword-quality" / f"keyword-quality-{week}.md")


def write_outputs(root, metrics, output_dir=None):
    paths = output_paths(root, metrics, output_dir)
    lock = paths[0].with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"Metrics writer lock exists: {lock}") from exc
    os.close(descriptor)
    try:
        for path, text in zip(paths, (json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                                     render_block(metrics) + "\n", render_quality(metrics))):
            atomic_text(path, text)
    finally:
        lock.unlink()
    return paths


def check_outputs(root, metrics, output_dir=None):
    paths = output_paths(root, metrics, output_dir)
    if json.loads(paths[0].read_text(encoding="utf-8")) != metrics:
        raise ValueError("Saved metrics are stale or altered; regenerate")
    if paths[1].read_text(encoding="utf-8") != render_block(metrics) + "\n" or paths[2].read_text(encoding="utf-8") != render_quality(metrics):
        raise ValueError("Generated Markdown differs from the metrics JSON; regenerate")


def report_block(text):
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) >= text.index(END):
        raise ValueError("Report needs exactly one valid weekly-metrics block")
    return text[text.index(START):text.index(END) + len(END)]


def sync_report(path, metrics):
    text = path.read_text(encoding="utf-8") if path.exists() else f"# Weekly News Research Report - {metrics['weekStart']} to {metrics['weekEnd']}\n"
    if START in text or END in text:
        text = text.replace(report_block(text), render_block(metrics))
    else:
        text = text.rstrip() + "\n\n" + render_block(metrics) + "\n"
    atomic_text(path, text)


def check_report(path, metrics):
    if report_block(path.read_text(encoding="utf-8")) != render_block(metrics):
        raise ValueError("Report numeric block is stale or altered")


def read_weekly_input(root, through, as_of=None):
    metrics = build_metrics(root, through, as_of)
    try:
        check_outputs(root, metrics)
    except (OSError, ValueError) as exc:
        raise ValueError("Generate current metrics first: python3 scripts/generate_analysis_reports.py --through " + through +
                         (" --as-of " + as_of if as_of else "") + " (" + str(exc) + ")") from exc
    paths = output_paths(root, metrics)
    compact = {key: metrics[key] for key in ("snapshotId", "week", "coveredThrough", "reviewAsOf", "status", "counts",
               "coverage", "feedback", "daily", "captureStatuses", "taskStatuses", "classificationCoverage", "warnings")}
    for key in ("byCategory", "byArticleType", "byRelevance"):
        compact[key] = metrics[key]
    compact["topSources"] = sorted(metrics["bySource"].items(), key=lambda row: (-row[1]["count"], row[0]))[:20]
    compact["topEntities"] = metrics["entities"][:20]
    return {"inputVersion": 2, "task": "weekly-report", "runDate": through, "metrics": compact,
            "references": [str(path.relative_to(root)) for path in paths],
            "articles": [], "nextOffset": None,
            "nextAction": "Use fixed numbers and reviewed sources for interpretation. --weekly-articles explicitly reads raw pre-exclusion article pages. Do not recount or treat metadata captures as written summaries."}


def main(root, argv=None):
    parser = argparse.ArgumentParser(description="Generate canonical weekly metrics JSON and matching Markdown.")
    parser.add_argument("--week", help="ISO week; without --through covers Monday through Sunday")
    parser.add_argument("--through", help="Collection cutoff YYYY-MM-DD; starts on that week's Monday")
    parser.add_argument("--as-of", help="Evaluation cutoff YYYY-MM-DD; defaults to --through")
    parser.add_argument("--records-dir", type=Path)
    parser.add_argument("--ai-candidate-dir", type=Path)
    parser.add_argument("--classification-proposal-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--require-feedback", action="store_true", help="Fail instead of emitting provisional numbers when full feedback is unavailable")
    parser.add_argument("--check", action="store_true", help="Verify saved JSON and generated Markdown without writing")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sync-report", type=Path, help="Update only the generated numeric block in a research report")
    group.add_argument("--check-report", type=Path, help="Verify the research report's generated numeric block without writing")
    args = parser.parse_args(argv)
    through = args.through
    if args.week:
        if not re.fullmatch(r"\d{4}-W\d{2}", args.week):
            raise ValueError("Use an ISO week such as 2026-W36")
        year, week = map(int, args.week.split("-W"))
        sunday = date.fromisocalendar(year, week, 7)
        through = through or sunday.isoformat()
        if legacy.week_label(day(through)) != args.week:
            raise ValueError("--week and --through disagree")
    if through is None:
        available = dated_paths(args.records_dir or root / "content/structured-records", ".jsonl", "9999-12-31")
        if not available:
            raise ValueError("No saved article dates; specify --through for a missing-data report")
        through = available[-1].stem
    metrics = build_metrics(root, through, args.as_of, args.records_dir, args.ai_candidate_dir, args.classification_proposal_dir)
    if args.require_feedback and not metrics["feedback"]["applied"]:
        raise ValueError("Full feedback snapshot is missing, incomplete or stale; refresh feedback JSON first")
    if args.check and args.sync_report:
        raise ValueError("--check cannot write --sync-report")
    if args.check or args.check_report:
        check_outputs(root, metrics, args.output_dir)
        if args.check_report:
            check_report(args.check_report, metrics)
    else:
        write_outputs(root, metrics, args.output_dir)
        if args.sync_report:
            sync_report(args.sync_report, metrics)
    print(json.dumps({"week": metrics["week"], "coveredThrough": through, "reviewAsOf": metrics["reviewAsOf"],
                      "snapshotId": metrics["snapshotId"], "status": metrics["status"], "counts": metrics["counts"],
                      "paths": [str(p) for p in output_paths(root, metrics, args.output_dir)],
                      "checked": bool(args.check or args.check_report), "warnings": metrics["warnings"]}, ensure_ascii=False))
    return 0
