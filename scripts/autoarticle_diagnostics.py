"""Bounded diagnostics. Never submit workflows or modify operation evidence."""
from __future__ import annotations
import sqlite3
import subprocess
import uuid
from pathlib import Path
import autoarticle_apply as db
import autoarticle_n8n as n8n
from autoarticle_progress import Blocked, digest, now, today, write_json, read_json

ERRORS = (Blocked, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired)

def reason(error):
    return str(error) if isinstance(error, Blocked) else type(error).__name__

def file_changes(progress, entry):
    changes = progress.changes(entry)
    return {"count": changes["changedFileCount"], "sample": changes["changedFiles"],
            "sampleLimit": 10, "reasons": changes["reasons"], "dependencyMode": changes["dependencyMode"]}

def mismatch_summary(kind, value, current):
    issues = []
    total = 0
    for table, key, rows in db.expected(kind, value, current):
        indexed = {}
        for stored in current.get(table, []):
            indexed.setdefault(stored.get(key), []).append(stored)
        for row in rows:
            matches = indexed.get(row[key], [])
            fields = ["row_count"] if len(matches) != 1 else [f for f, wanted in row.items() if not db.equal(f, matches[0].get(f), wanted)]
            if fields:
                total += 1
                if len(issues) < 10:
                    # No source text, URLs, credentials, or values in the bundle.
                    issues.append({"table": table, "fields": fields})
    return {"mismatchedRows": total, "sample": issues, "sampleLimit": 10}

def preflight(ops, kind):
    p = ops.progress
    checks = []
    details = {}
    def check(name, action):
        try:
            value = action()
            checks.append({"check": name, "status": "passed"})
            return value
        except ERRORS as error:
            checks.append({"check": name, "status": "blocked", "reason": reason(error)})
            return None
    health = check("connection", lambda: (ops.client if kind != "dashboard" else n8n.Client(ops.dashboard, "")).request("/healthz" if kind != "dashboard" else "/api/health", timeout=5))
    if health is None or kind == "dashboard":
        return {"kind": kind, "ready": all(c["status"] == "passed" for c in checks), "checks": checks}
    executions = None
    resolved = check("workflow", lambda: ops.workflow(kind, require=True))
    if resolved is not None:
        workflow, _ = resolved
        details["workflowId"] = str(workflow["id"])
        executions = check("execution_history", lambda: n8n.executions(ops.client, workflow["id"], p.date))
        if executions is not None:
            rows, running = executions
            details["executions"] = [{key: row.get(key) for key in ("id", "status", "startedAt")} for row in sorted(rows, key=lambda r: r.get("startedAt") or "", reverse=True)[:3]]
            if running:
                checks.append({"check": "running_execution", "status": "blocked", "reason": "workflow_already_running"})
            elif kind == "collect" and rows:
                try:
                    n8n.collection_success(rows, p.load()['steps'].get('collect', {}))
                    details["nextAction"] = "collect_reconciles_existing_success_without_resubmitting"
                except Blocked as error:
                    checks.append({"check": "existing_execution", "status": "blocked", "reason": str(error)})
    saved = check("operation_evidence", p.load)
    if kind == "collect":
        def keyword_config():
            config = read_json(ops.root / "config/keywords.json")
            manual = config.get("manualKeywords")
            excluded = config.get("excludedKeywords", [])
            cap = config.get("maxAutoKeywords", 30)
            if not isinstance(manual, list) or not manual or any(not isinstance(v, str) or not v.strip() for v in manual):
                raise Blocked("invalid_manual_keywords")
            if not isinstance(excluded, list) or any(not isinstance(v, str) for v in excluded) or isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
                raise Blocked("invalid_keyword_configuration")
            return True
        check("keyword_configuration", keyword_config)
        if p.date != today():
            checks.append({"check": "collection_date", "status": "blocked", "reason": "collection_only_supports_today_jst"})
        if saved is not None:
            entry = saved["steps"].get("collect")
            if entry and not p.current(entry):
                checks.append({"check": "collection_evidence", "status": "blocked", "reason": "collection_evidence_changed"})
            if executions is not None and not executions[0] and (entry or any(p.path(path).exists() for path in p.generated().values() if p.date in path)):
                checks.append({"check": "collection_history", "status": "blocked", "reason": "existing_collection_evidence_requires_review"})
            if entry:
                details["savedState"] = entry["status"]
                details["changedFiles"] = file_changes(p, entry)
                if entry["status"] == "submission_unknown":
                    details["nextAction"] = "inspect_execution_and_files_before_any_reconciliation; never_resubmit"
        details["verification"] = "readiness_only; collect still verifies execution and generated files"
    elif kind in ("talent", "classification"):
        review = "talent-review" if kind == "talent" else "classification-review"
        if saved is not None:
            entry = saved["steps"].get(review, {})
            if entry.get("status") != "completed" or not p.current(entry):
                checks.append({"check": "review_checkpoint", "status": "blocked", "reason": "proposal_review_missing_or_stale"})
            details["changedFiles"] = file_changes(p, entry)
        value = check("proposal_contract", lambda: db.proposal(p, kind))
        if value is not None and resolved is not None:
            current = check("database_target", lambda: db.tables(ops.client.base, resolved[0], ops.root))
            if current is not None:
                check("proposal_fields_and_permissions", lambda: db.preflight(kind, value, current))
                if saved is not None and "apply-" + kind in saved["steps"]:
                    if not p.current(saved["steps"]["apply-" + kind]):
                        checks.append({"check": "previous_submission_inputs", "status": "blocked", "reason": "previous_apply_inputs_changed_requires_review"})
                    check("previous_submission", lambda: db.verify(kind, value, current))
                    details["databaseDifference"] = check("database_difference", lambda: mismatch_summary(kind, value, current))
                    details["nextAction"] = "apply_reconciles_existing_submission_only; never_resubmit"
        details["verification"] = "readiness_only; workflow validates full proposal schema at apply"
    return {"kind": kind, "ready": all(c["status"] == "passed" for c in checks), "checks": checks, **details}

def snapshot(ops, step, code, live=None):
    p = ops.progress
    result = {"diagnosticVersion": 1, "runDate": p.date, "createdAt": now(), "step": step, "reason": code,
              "operationEvidence": str(p.file.relative_to(ops.root)), "automaticRetry": False,
              "next": ["tokens begin investigation", "inspect only the failed step evidence", "resume after repair; reuse verified completed steps"],
              "verification": "diagnostic_only; not editorial approval or operation completion"}
    try:
        saved = p.load()["steps"]
        result["recordedSteps"] = {name: entry["status"] for name, entry in saved.items()}
        entry = saved.get(step, {})
        result["failedStepEvidence"] = {key: entry[key] for key in ("status", "workflowId", "executionId", "checkedAt") if key in entry}
        result["changedFiles"] = file_changes(p, entry)
        # Existing startup log location only, never log contents.
        if entry.get("log"):
            log = Path(entry["log"]).resolve()
            if ops.root in log.parents:
                result["startupLog"] = str(log.relative_to(ops.root))
    except ERRORS as error:
        result["evidenceReadError"] = reason(error)
    if live is not None:
        result["liveChecks"] = live
    path = ops.root / ".operation-logs" / "diagnostics" / (p.date + "-" + uuid.uuid4().hex + ".json")
    write_json(path, result)
    return str(path.relative_to(ops.root))

def diagnose(ops, step):
    kind = {"apply-talent": "talent", "talent-review": "talent", "apply-classification": "classification", "classification-review": "classification", "page": "dashboard"}.get(step, step)
    live = preflight(ops, kind) if kind in ("collect", "talent", "classification", "dashboard") else None
    path = snapshot(ops, step, "operator_requested_diagnosis", live)
    return {"status": "diagnostic_saved", "step": step, "diagnosticFile": path, "ready": live.get("ready") if live else None, "automaticRetry": False}
