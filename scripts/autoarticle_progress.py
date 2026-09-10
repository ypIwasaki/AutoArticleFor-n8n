"""Durable, local operation evidence. File hashes are not semantic approval."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
REVIEW_STEPS = ("summary", "talent-review", "classification-review", "keywords", "weekly", "page")
ALL_STEPS = ("n8n", "collect", "summary", "talent-review", "classification-review", "keywords", "weekly", "apply-talent", "apply-classification", "dashboard", "page")


class Blocked(Exception):
    """Public reason code: never put server bodies or secrets in this exception."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def today():
    return dt.datetime.now(JST).date().isoformat()


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    if not path.is_file():
        return None
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(str(temp), 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


class Progress:
    def __init__(self, root, run_date, target):
        self.root = Path(root).resolve()
        self.date, self.target = run_date, target
        date = dt.date.fromisoformat(run_date)
        if date.isoformat() != run_date:
            raise Blocked("invalid_date")
        self.monday = (date - dt.timedelta(days=date.weekday())).isoformat()
        self.week = "%04d-W%02d" % date.isocalendar()[:2]
        self.directory = self.root / ".operation-state"
        self.file = self.directory / (self.date + ".json")

    def path(self, relative):
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise Blocked("evidence_outside_project")
        return path

    def generated(self):
        folders = ("structured-records", "daily-digests", "keyword-candidates", "ai-summary-instructions", "ai-extraction-instructions", "ai-talent-index-instructions", "ai-article-classification-instructions")
        result = {folder: "content/%s/%s.%s" % (folder, self.date, "jsonl" if folder == "structured-records" else "md") for folder in folders}
        result["ai-weekly-report-instructions"] = "content/ai-weekly-report-instructions/%s.md" % self.monday
        return result

    def outputs(self, step):
        mapping = {
            "summary": ["article-summaries/%s.md" % self.date],
            "talent-review": ["talent-index-proposals/%s.%s" % (self.date, ext) for ext in ("json", "md")],
            "classification-review": ["article-classification-proposals/%s.%s" % (self.date, ext) for ext in ("json", "md")],
            "keywords": ["ai-keyword-candidates/%s.md" % self.date],
            "weekly": ["weekly-reports/%s.md" % self.monday, "analysis/weekly-metrics/%s.json" % self.week, "analysis/weekly-reports/weekly-trends-%s.md" % self.week, "analysis/keyword-quality/keyword-quality-%s.md" % self.week],
            "page": [],
        }
        return ["content/" + item for item in mapping[step]]

    def fingerprints(self, paths):
        return {p: digest(self.path(p)) for p in sorted(set(paths))}

    def inputs(self, step):
        inputs = list(self.generated().values())
        inputs += ["content/article-body-captures/%s.jsonl" % self.date, "content/article-review-facts/%s.jsonl" % self.date]
        inputs += [str(p.relative_to(self.root)) for p in (self.root / "docs/ai-rules").glob("*.md")]
        inputs += ["config/article-classification-taxonomy.json", "config/keywords.json"]
        if step in ("weekly", "page"):
            for name in REVIEW_STEPS:
                if name != step:
                    inputs += self.outputs(name)
        return inputs

    def load(self):
        if not self.file.exists():
            return {"schemaVersion": 1, "runDate": self.date, "steps": {}, "history": []}
        value = read_json(self.file)
        if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("runDate") != self.date or not isinstance(value.get("steps"), dict) or not isinstance(value.get("history"), list):
            raise Blocked("invalid_progress_file")
        if any(step not in ALL_STEPS or not isinstance(entry, dict) or entry.get("status") not in ("completed", "starting", "submission_unknown") for step, entry in value["steps"].items()):
            raise Blocked("invalid_progress_steps")
        return value

    @contextlib.contextmanager
    def lock(self):
        if os.name != "posix":
            raise Blocked("run_in_wsl_or_linux")
        import fcntl
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with (self.directory / "operations.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked("another_operation_running")
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def record(self, step, status, **evidence):
        state = self.load()
        entry = dict(status=status, checkedAt=now(), target=self.target, **evidence)
        state["steps"][step] = entry
        state["history"].append(dict(step=step, **entry))
        write_json(self.file, state)
        return entry

    def current(self, entry):
        return entry.get("target") == self.target and isinstance(entry.get("files"), dict) and all(digest(self.path(p)) == value for p, value in entry["files"].items())

    def checkpoint(self, step, evidence, note):
        if not evidence or not note.strip():
            raise Blocked("review_evidence_and_note_required")
        paths = self.outputs(step) + [str(self.path(p).relative_to(self.root)) for p in evidence]
        if not all(self.path(p).is_file() for p in paths):
            raise Blocked("checkpoint_output_or_evidence_missing")
        if not self.path(self.generated()["structured-records"]).is_file():
            raise Blocked("source_archive_missing")
        files = self.fingerprints(paths + self.inputs(step))
        self.record(step, "completed", files=files, verification="operator_attested", note=note[:1000], evidence=evidence)
        return {"step": step, "status": "completed", "verification": "operator_attested", "fileCount": len(files)}
