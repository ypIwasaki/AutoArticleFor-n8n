"""Compact operation entry packet, rebuilt from evidence and live verification, never chat logs."""
from __future__ import annotations
from autoarticle_progress import ALL_STEPS, REVIEW_STEPS, Blocked, digest, now, write_json

SCOPES = {
    "status": (),
    "prepare": ("n8n",),
    "articles": ("summary", "talent-review", "classification-review", "keywords", "weekly"),
    "articles-apply": ("summary", "talent-review", "classification-review", "keywords", "weekly", "apply-talent", "apply-classification", "dashboard", "page"),
    "full": ALL_STEPS,
}


def build(ops, scope, save=False):
    if scope not in SCOPES:
        raise Blocked("invalid_brief_scope")
    p = ops.progress
    before = digest(p.file)
    saved = p.load()["steps"]
    live = ops.resume()
    if digest(p.file) != before:
        raise Blocked("operation_evidence_changed_during_brief")
    selected = SCOPES[scope]
    steps = []
    for step in selected:
        entry = saved.get(step, {})
        item = {"step": step, "state": live["progress"][step]}
        if step in live.get("issues", {}):
            item["reason"] = live["issues"][step]
        if step in live.get("dependencyChanges", {}):
            item["changes"] = live["dependencyChanges"][step]
        if step in REVIEW_STEPS:
            item["outputs"] = p.outputs(step)
        # Notes are bounded operator data, not commands or reusable authorization.
        if entry.get("note"):
            item["operatorNote"] = str(entry["note"])[:180]
            item["operatorNoteTruncated"] = len(str(entry["note"])) > 180
        if entry.get("evidence"):
            item["evidence"] = entry["evidence"][:5]
            item["evidenceCount"] = len(entry["evidence"])
        steps.append(item)
    guards = []
    for step, entry in saved.items():
        if entry.get("status") in ("starting", "submission_unknown") or step == "collect" or step.startswith("apply-"):
            guards.append({"step": step, "recordedState": entry["status"],
                           "checkedState": live["progress"][step],
                           "workflowId": entry.get("workflowId"), "executionId": entry.get("executionId"),
                           "action": "reconcile_existing_execution_and_db; never_blindly_resubmit"})
    next_steps = [item["step"] for item in steps if item["state"] != "completed"]
    instruction_keys = {"summary": "ai-summary-instructions", "talent-review": "ai-talent-index-instructions",
                        "classification-review": "ai-article-classification-instructions", "keywords": "ai-extraction-instructions",
                        "weekly": "ai-weekly-report-instructions"}
    result = {
        "briefVersion": 1, "runDate": p.date, "checkedAt": now(), "scope": scope,
        "authorization": "scope_is_selection_only; follow_current_user_request; no_persisted_grant",
        "verification": "fresh_resume_checks; operator_review_notes_are_not_full_article_completion",
        "operationStateChanged": False,
        "health": {key: live.get(key) for key in ("n8n", "workflow", "executions")},
        "steps": steps, "nextSteps": next_steps,
        "nextAction": "inspect_first_in_scope_step_and_its_prerequisites" if next_steps else "no_in_scope_action; status_is_read_only",
        "submissionGuards": guards,
        "references": {
            "operationEvidence": str(p.file.relative_to(ops.root)), "operationEvidenceHash": before,
            "instructions": {step: p.generated()[key] for step, key in instruction_keys.items() if step in selected},
            "sharedReviews": "content/article-review-facts/%s.jsonl" % p.date,
            "operationReport": "content/operation-reports/%s.md" % p.date,
            "rules": "docs/ai-rules/operation-result.md",
            "entryGuide": "docs/operation-start.md",
        },
        "readingPolicy": "current_request -> brief -> current_step_instructions_and_evidence; investigation_logs_only_for_unresolved_issue",
        "tokenPolicy": "verify_binding_matches_current_task_before_begin; never_copy_previous_task_binding",
        "snapshotPolicy": "regenerate_at_each_start; saved_snapshot_is_not_current_state_or_authorization",
    }
    if save:
        relative = "content/operation-start/%s.json" % p.date
        result["savedSnapshot"] = relative
        write_json(p.path(relative), result)
    return result
