"""Validated, source-bound semantic review records; no AI or network calls."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

REVIEW_VERSION = 1
TASKS = ("article-summary", "talent-index", "article-classification")
DIRECTORY = "content/article-review-facts"
RULES = "docs/ai-rules/shared-article-review.md"
POLICY_FILES = (RULES,) + tuple(f"docs/ai-rules/{task}.md" for task in TASKS)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def policy_hash(root: Path) -> str | None:
    # Missing policy must never allow reuse, but legacy input reading still works.
    if not all((root / name).is_file() for name in POLICY_FILES):
        return None
    return digest([REVIEW_VERSION, {name: (root / name).read_text(encoding="utf-8-sig") for name in POLICY_FILES}])


def selected_content(capture: dict[str, Any] | None) -> tuple[str, str]:
    capture = capture or {}
    for field in ("contentText", "contentMarkdown"):
        text = capture.get(field)
        if isinstance(text, str) and text:
            return field, text
    metadata = capture.get("pageMetadata")
    if isinstance(metadata, dict) and metadata:
        return "pageMetadata", canonical(metadata)
    return "contentText", ""


def source_fields(article: dict[str, Any], capture: dict[str, Any] | None) -> dict[str, str]:
    fields = {key: str(article.get(key) or "") for key in ("title", "excerpt")}
    field, text = selected_content(capture)
    fields[field] = text
    if capture and isinstance(capture.get("pageMetadata"), dict):
        fields["pageMetadata"] = canonical(capture["pageMetadata"])
    return fields


def input_hash(article: dict[str, Any], capture: dict[str, Any] | None) -> str:
    article_input = {key: article.get(key) for key in ("url", "title", "excerpt", "source", "publishedAt")}
    capture_input = None
    if capture is not None:
        keys = ("resolvedUrl", "contentStatus", "contentType", "contentCompleteness", "contentText",
                "contentMarkdown", "pageMetadata", "failureReason", "extractionMethod", "extractionScope")
        capture_input = {key: capture.get(key) for key in keys}
        if capture.get("contentStatus") != "verified":
            capture_input["fetchedAt"] = capture.get("fetchedAt")
    return digest({"article": article_input, "capture": capture_input})


def require_text(value: Any, label: str, maximum: int = 2000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label}: expected nonempty text, at most {maximum} characters")


def validate_record(record: dict[str, Any], article: dict[str, Any], capture: dict[str, Any] | None,
                    expected_policy: str | None) -> None:
    """Check structure, provenance and verbatim evidence, not semantic correctness."""
    if not isinstance(record, dict):
        raise ValueError("Review must be an object")
    required = {"reviewVersion", "url", "inputHash", "policyHash", "basis", "taskStatus",
                "facts", "entities", "evidence", "unresolved", "reviewedBy"}
    if not required <= record.keys() or record.keys() - required - {"sourceDate", "reviewedAt"}:
        raise ValueError("Review fields are missing or unsupported")
    if type(record["reviewVersion"]) is not int or record["reviewVersion"] != REVIEW_VERSION:
        raise ValueError("Unsupported reviewVersion")
    if record["url"] != article["url"] or record["inputHash"] != input_hash(article, capture):
        raise ValueError("Review inputHash/URL does not match the saved input; re-read before reviewing")
    if not expected_policy or record["policyHash"] != expected_policy:
        raise ValueError("Review policyHash does not match the current shared rules")
    require_text(record["reviewedBy"], "reviewedBy", 100)
    statuses = record["taskStatus"]
    if not isinstance(statuses, dict) or set(statuses) != set(TASKS):
        raise ValueError("taskStatus must specify all three tasks")
    if any(value not in ("ready", "held", "needs_review") for value in statuses.values()):
        raise ValueError("taskStatus must be ready, held or needs_review")
    basis = record["basis"]
    if basis not in ("body", "partial", "metadata", "none"):
        raise ValueError("Invalid review basis")
    status = (capture or {}).get("contentStatus")
    field, text = selected_content(capture)
    if basis == "body" and (status != "verified" or field == "pageMetadata" or not text.strip()):
        raise ValueError("body basis requires a nonempty verified saved body (not metadata)")
    if basis == "partial" and (status != "partial" or field == "pageMetadata" or not text.strip()):
        raise ValueError("partial basis requires a saved partial body")
    if basis == "metadata" and status != "metadata_only":
        raise ValueError("metadata basis requires metadata_only capture")
    if any(statuses[task] == "ready" for task in ("article-summary", "article-classification")) and basis != "body":
        raise ValueError("Summary/classification ready requires body basis")
    for key in ("facts", "entities", "evidence", "unresolved"):
        if not isinstance(record[key], list) or len(record[key]) > 100:
            raise ValueError(f"{key} must be an array of at most 100 items")
    for item in record["unresolved"]:
        require_text(item, "unresolved")
    if any(value != "ready" for value in statuses.values()) and not record["unresolved"]:
        raise ValueError("Held/incomplete tasks require unresolved reasons")
    fields = source_fields(article, capture)
    evidence_ids = set()
    body_evidence = set()
    for evidence in record["evidence"]:
        if not isinstance(evidence, dict) or set(evidence) != {"id", "field", "start", "end", "quote"}:
            raise ValueError("Evidence requires id, field, start, end, quote")
        require_text(evidence["id"], "evidence.id", 100)
        if evidence["id"] in evidence_ids:
            raise ValueError("Duplicate evidence id")
        evidence_ids.add(evidence["id"])
        name, start, end = evidence["field"], evidence["start"], evidence["end"]
        require_text(name, "evidence.field", 100)
        require_text(evidence["quote"], "evidence.quote", 1000)
        if name not in fields or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(fields[name]):
            raise ValueError("Invalid evidence field or character range")
        if fields[name][start:end] != evidence["quote"]:
            raise ValueError("Evidence quote does not match the saved source range")
        if name in ("contentText", "contentMarkdown"):
            body_evidence.add(evidence["id"])
    fact_ids = set()
    grounded_in_body = False
    for fact in record["facts"]:
        if not isinstance(fact, dict) or set(fact) != {"id", "text", "evidenceIds"}:
            raise ValueError("Fact requires id, text, evidenceIds")
        require_text(fact["id"], "fact.id", 100)
        require_text(fact["text"], "fact.text")
        if fact["id"] in fact_ids:
            raise ValueError("Duplicate fact id")
        fact_ids.add(fact["id"])
        refs = fact["evidenceIds"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in evidence_ids for ref in refs):
            raise ValueError("Fact evidenceIds must reference saved evidence")
        grounded_in_body |= bool(body_evidence.intersection(refs))
    if any(value == "ready" for value in statuses.values()) and not fact_ids:
        raise ValueError("Ready tasks require grounded facts")
    if any(statuses[t] == "ready" for t in ("article-summary", "article-classification")) and not grounded_in_body:
        raise ValueError("Summary/classification ready requires facts grounded in body evidence")
    for entity in record["entities"]:
        if not isinstance(entity, dict) or set(entity) != {"name", "kind", "factIds"}:
            raise ValueError("Entity requires name, kind, factIds")
        require_text(entity["name"], "entity.name", 200)
        if entity["kind"] not in ("person", "organization", "other"):
            raise ValueError("Invalid entity kind")
        refs = entity["factIds"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in fact_ids for ref in refs):
            raise ValueError("Entity factIds must reference grounded facts")
    # Bound the complete shared packet; do not silently truncate reviewed evidence.
    if len(canonical(record)) > 30000:
        raise ValueError("Review exceeds 30000 characters; keep facts/evidence concise")


def load_reviews(root: Path, through_date: str, warnings: list[str], feature: str = "ai-reader") -> tuple[dict, set]:
    import project_readers as project
    if project.source(root, feature) == "project-db":
        with project.reader(root) as reader:
            return reader.reviews(through_date, warnings)
    index, known_urls = {}, set()
    for path in sorted((root / DIRECTORY).glob("*.jsonl")):
        try:
            day = date.fromisoformat(path.stem).isoformat()
        except ValueError:
            continue
        if day > through_date:
            continue
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        except (OSError, ValueError):
            warnings.append(f"{DIRECTORY}/{path.name}: unreadable review cache; use original input")
            continue
        for record in rows:
            if not isinstance(record, dict) or not isinstance(record.get("url"), str):
                warnings.append(f"{DIRECTORY}/{path.name}: invalid review entry ignored")
                continue
            url = record["url"]
            known_urls.add(url)
            if not all(isinstance(record.get(key), str) for key in ("inputHash", "policyHash")):
                continue
            # The containing date is authoritative; never accept future/mislabelled provenance.
            index[(url, record["inputHash"], record["policyHash"])] = (record, day, f"{DIRECTORY}/{path.name}")
    return index, known_urls


def resolve_review(index: dict, known_urls: set, day: str, article: dict, capture: dict | None,
                   policy: str | None, task: str) -> dict:
    key = (article["url"], input_hash(article, capture), policy)
    match = index.get(key)
    if not match:
        return {"status": "stale" if article["url"] in known_urls else "missing", "taskStatus": "needs_review"}
    record, source_day, path = match
    database_status = getattr(record, "database_status", "current")
    if database_status != "current":
        return {"status": database_status, "taskStatus": "held" if database_status == "held" else "needs_review",
                "path": path, "reason": "Dedicated DB review is not current",
                "databaseReferences": record.database_references}
    try:
        if record.get("sourceDate") != source_day:
            raise ValueError("Review sourceDate does not match its file")
        reviewed_at = datetime.fromisoformat(str(record.get("reviewedAt", "")).replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("Missing review timestamp timezone")
        validate_record(record, article, capture, policy)
    except (TypeError, ValueError, KeyError) as exc:
        return {"status": "invalid", "taskStatus": "needs_review", "reason": str(exc)}
    if record["basis"] != "body" and source_day != day:
        return {"status": "stale", "taskStatus": "needs_review", "reason": "Non-body reviews must be reconsidered on a new date"}
    result = {"status": "current", "taskStatus": record["taskStatus"][task], "path": path, "record": record}
    if hasattr(record, "database_references"):
        result["databaseReferences"] = record.database_references
    return result


def atomic_merge(path: Path, records: list[dict]) -> None:
    """Serialize writers; validate all inputs before calling; replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"Review write lock exists: {lock}; check concurrent writers before removing a stale lock") from exc
    os.close(descriptor)
    temporary = None
    try:
        previous = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
        merged = {}
        for record in previous + records:
            key = (record["url"], record["inputHash"], record["policyHash"])
            merged[key] = record
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".review-", delete=False) as stream:
            temporary = Path(stream.name)
            for record in merged.values():
                stream.write(canonical(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        lock.unlink()
