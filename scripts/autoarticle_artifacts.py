"""Read-only checks of saved artifacts. Structure/provenance never imply semantic approval."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from urllib.parse import urlsplit

import article_artifact_formats as formats
import article_review_facts as shared
import autoarticle_apply as db
from autoarticle_progress import Blocked, read_json, timestamp
from read_ai_inputs import load_day

STEPS = ("summary", "talent-review", "classification-review", "weekly")


class ArtifactInvalid(Blocked):
    def __init__(self, result):
        super().__init__("saved_artifact_invalid")
        self.result = result


class Check:
    def __init__(self):
        self.count = 0
        self.errors = []

    def require(self, condition, code, row=None):
        if not condition:
            self.count += 1
            if len(self.errors) < 10:
                self.errors.append(dict(code=code, **({"row": row} if row is not None else {})))
        return bool(condition)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def url(value):
    try:
        return text(value) and urlsplit(value).scheme in ("http", "https") and bool(urlsplit(value).hostname)
    except ValueError:
        return False


def array(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, list) and all(text(x) for x in result) else None
    except ValueError:
        return None


def date_time(value):
    try:
        timestamp(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def confidence(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1


def source_context(progress):
    warnings = []
    _, rows, captures = load_day(progress.root, progress.date, warnings)
    articles = {r["article"]["url"]: r["article"] for r in rows}
    index, known = shared.load_reviews(progress.root, progress.date, warnings)
    policy = shared.policy_hash(progress.root)

    def review(article_url, task):
        article = articles.get(article_url)
        if article is None:
            return False
        result = shared.resolve_review(index, known, progress.date, article, captures.get(article_url), policy, task)
        return result["status"] == "current" and result["taskStatus"] == "ready"

    return articles, review, len(warnings)


def summary(progress, check):
    markdown = progress.path(progress.outputs("summary")[0]).read_text(encoding="utf-8")
    articles, ready, warnings = source_context(progress)
    parsed = formats.parsed_summaries(markdown, progress.date)
    # Count declarations across the entire file: unsupported headings must not disappear silently.
    declarations = len(re.findall(r"^\s*-\s*要約\s*[:：]", markdown, re.MULTILINE))
    check.require(formats.SOURCE_NOTES_HEADING in markdown, "summary_section_missing")
    check.require(declarations == len(parsed), "summary_not_readable_by_dashboard")
    seen = set()
    for i, (links, entry) in enumerate(parsed, 1):
        article_url = links[0][1].strip()
        check.require(text(entry["text"]), "summary_empty", i)
        check.require(url(article_url) and article_url in articles, "summary_source_unknown", i)
        check.require(article_url not in seen, "summary_duplicate_url", i)
        check.require(ready(article_url, "article-summary"), "summary_review_not_current_ready", i)
        # Extra links also become dashboard keys; prevent a summary being assigned to another source.
        check.require(all(link.strip() == article_url for _, link in links), "summary_links_disagree", i)
        seen.add(article_url)
    expected = {u for u in articles if ready(u, "article-summary")}
    check.require(seen == expected, "summary_ready_coverage_mismatch")
    return {"declaredSummaries": declarations, "dashboardSummaries": len(parsed), "readyArticles": len(expected), "inputWarningCount": warnings}


def proposals(progress, step, check, existing):
    kind = "talent" if step == "talent-review" else "classification"
    value = db.proposal(progress, kind)
    articles, ready, warnings = source_context(progress)
    counts = {"inputWarningCount": warnings}
    if kind == "talent":
        for group, fields in (("articles", ("article_key", "url", "title", "excerpt", "source", "last_seen_at")),
                              ("talents", ("talent_id", "display_name", "organization", "aliases_json", "status", "last_seen_at")),
                              ("articleTalents", ("relation_key", "article_key", "talent_id", "matched_aliases_json", "matched_fields", "evidence_text", "detection_method", "last_seen_at"))):
            for i, row in enumerate(value[group], 1):
                check.require(all(isinstance(row.get(f), str) for f in fields), group + "_text_type_invalid", i)
        groups = db.expected(kind, value, {})
        by_key = {r["article_key"]: r for r in value["articles"]}
        talents = {r["talent_id"] for r in value["talents"]}
        if any(r["article_key"] not in by_key or r["talent_id"] not in talents for r in value["articleTalents"]):
            stored = existing()
            by_key = {**{r["article_key"]: r for r in stored.get("articles", [])}, **by_key}
            talents |= {r["talent_id"] for r in stored.get("talents", [])}
        urls = [r["url"] for r in value["articles"]]
        check.require(len(set(urls)) == len(urls), "article_url_duplicate")
        for table, _, rows in groups:
            counts[table] = len(rows)
            for i, row in enumerate(rows, 1):
                check.require(date_time(row["last_seen_at"]), table + "_timestamp_invalid", i)
                if table == "articles":
                    check.require(url(row["url"]) and row["url"] in articles, "article_source_unknown", i)
                    check.require(text(row["title"]), "article_title_missing", i)
                    check.require(not row["published_at"] or date_time(row["published_at"]), "article_published_at_invalid", i)
                elif table == "talents":
                    check.require(text(row["display_name"]) and text(row["status"]), "talent_required_text_missing", i)
                    check.require(array(row["aliases_json"]) is not None, "talent_aliases_invalid", i)
                    check.require(type(row["search_enabled"]) is bool and type(row["auto_discovered"]) is bool, "talent_boolean_invalid", i)
                else:
                    source = by_key.get(row["article_key"], {})
                    check.require(bool(source) and row["talent_id"] in talents, "relation_reference_missing", i)
                    check.require(ready(source.get("url"), "talent-index"), "relation_review_not_current_ready", i)
                    check.require(array(row["matched_aliases_json"]) is not None, "relation_aliases_invalid", i)
                    check.require(text(row["evidence_text"]) and text(row["matched_fields"]), "relation_evidence_missing", i)
                    check.require(confidence(row["confidence"]) and row["detection_method"] == "ai_review", "relation_review_fields_invalid", i)
        return counts
    # Prefer local proposals; consult the verified DB only for unresolved references.
    path = progress.path(progress.outputs("talent-review")[0])
    talent = db.proposal(progress, "talent") if path.exists() else {"articles": []}
    proposed = talent["articles"]
    keys = {r["article_key"] for r in proposed}
    urls = {r["url"] for r in proposed}
    needs_existing = any((r.get("article_key") and r["article_key"] not in keys) or (r.get("article_url") and r["article_url"] not in urls) for r in value["classifications"])
    stored = existing().get("articles", []) if needs_existing else []
    merged = {r["article_key"]: r for r in stored + proposed}
    current = {"articles": list(merged.values())}
    by_key = {r["article_key"]: r["url"] for r in current["articles"]}
    taxonomy = read_json(progress.root / "config/article-classification-taxonomy.json")
    allowed = {k: {x["id"] for x in taxonomy[k]} for k in ("articleTypes", "categories", "relevance")}
    # An empty reviewed proposal is a valid saved artifact; apply retains its own no-op policy.
    rows = db.expected(kind, value, current)[0][2] if value["classifications"] else []
    for i, row in enumerate(rows, 1):
        check.require(ready(by_key.get(row["article_key"]), "article-classification"), "classification_review_not_current_ready", i)
        for field, choices in (("article_type", "articleTypes"), ("primary_category", "categories"), ("relevance", "relevance")):
            check.require(row[field] in allowed[choices], "classification_" + field + "_invalid", i)
        secondary = array(row["secondary_categories_json"])
        check.require(secondary is not None and len(secondary) <= taxonomy["rules"]["maximumSecondaryCategories"] and len(set(secondary)) == len(secondary) and all(x in allowed["categories"] and x != row["primary_category"] for x in secondary), "classification_secondary_invalid", i)
        check.require(confidence(row["confidence"]) and row["classification_method"] == "ai_review" and text(row["evidence_text"]), "classification_review_fields_invalid", i)
        check.require(date_time(row["classified_at"]), "classification_timestamp_invalid", i)
    counts["classifications"] = len(rows)
    return counts


def weekly(progress, check):
    path = progress.path(progress.outputs("weekly")[0])
    markdown = path.read_text(encoding="utf-8")
    meta = formats.weekly_report_metadata(markdown, path)
    end = (dt.date.fromisoformat(progress.monday) + dt.timedelta(days=6)).isoformat()
    check.require(bool(formats.WEEKLY_REPORT_FRONT_MATTER_PATTERN.match(markdown)), "weekly_frontmatter_missing")
    check.require(meta["weekStart"] == progress.monday and meta["weekEnd"] == end, "weekly_dates_mismatch")
    check.require(meta["coveredThrough"] == progress.date, "weekly_cutoff_mismatch")
    check.require(text(meta["title"]), "weekly_title_missing")
    return {"weekStart": meta["weekStart"], "coveredThrough": meta["coveredThrough"]}


def validate(progress, step, existing=None):
    def unavailable():
        raise Blocked("existing_references_require_db_check")
    existing = existing or unavailable
    check = Check()
    counts = {}
    try:
        if step == "summary":
            counts = summary(progress, check)
        elif step in ("talent-review", "classification-review"):
            counts = proposals(progress, step, check, existing)
        elif step == "weekly":
            counts = weekly(progress, check)
        else:
            raise ValueError("unsupported artifact step")
    except (Blocked, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        check.require(False, str(error) if isinstance(error, Blocked) else "artifact_" + type(error).__name__)
    result = {"step": step, "status": "valid" if not check.count else "invalid", "readOnly": True,
              "verification": "structure_and_review_provenance", "semanticReview": "operator_required",
              "counts": counts, "errorCount": check.count, "errors": check.errors}
    if check.count:
        raise ArtifactInvalid(result)
    return result
