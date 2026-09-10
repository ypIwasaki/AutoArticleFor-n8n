#!/usr/bin/env python3
"""AutoArticle operations: status/resume, start, collect, checkpoint, apply."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import autoarticle_apply as db_apply
import autoarticle_n8n as n8n
import autoarticle_tokens as token_usage
from autoarticle_progress import ALL_STEPS, REVIEW_STEPS, Blocked, Progress, now, read_json, today, write_json
from sync_workflow_to_n8n import load_env_file

ROOT = Path(__file__).resolve().parents[1]


class Operations:
    def __init__(self, root=ROOT, run_date=None, client=None, startup_env=None):
        self.root = Path(root).resolve()
        self.client = client or n8n.Client(os.environ.get("N8N_API_BASE_URL") or os.environ.get("N8N_BASE_URL") or "http://127.0.0.1:5678", os.environ.get("N8N_API_KEY", ""))
        self.progress = Progress(self.root, run_date or today(), self.client.base)
        host = os.environ.get("TALENT_DASHBOARD_HOST", "127.0.0.1")
        self.dashboard = "http://%s:%d" % (host, int(os.environ.get("TALENT_DASHBOARD_PORT", "8765")))
        self.startup_env = dict(os.environ if startup_env is None else startup_env)

    def workflow(self, kind, require=False):
        explicit = os.environ.get("N8N_WORKFLOW_ID") if kind == "collect" else os.environ.get("AUTOARTICLE_%s_WORKFLOW_ID" % kind.upper())
        workflow, matches = n8n.workflow(self.client, self.root, kind, explicit)
        if require and workflow.get("active") is not True:
            raise Blocked("workflow_not_active")
        if require and not matches:
            raise Blocked("workflow_definition_differs_sync_requires_review")
        return workflow, matches

    def status(self):
        p = self.progress
        result = {"runDate": p.date, "n8n": "unknown", "workflow": {"status": "unknown"}, "executions": {"status": "unknown"}}
        try:
            self.client.request("/healthz")
            result["n8n"] = "reachable"
        except Blocked as error:
            result["n8n"] = str(error)
        try:
            workflow, matches = self.workflow("collect")
            schedules = [node.get("parameters", {}).get("rule") for node in workflow.get("nodes", []) if node.get("type") == "n8n-nodes-base.scheduleTrigger" and not node.get("disabled")]
            result["workflow"] = {"id": str(workflow["id"]), "active": workflow.get("active"), "definitionMatches": matches,
                                  "timezone": workflow.get("settings", {}).get("timezone"), "schedule": schedules}
            rows, running = n8n.executions(self.client, workflow["id"], p.date)
            counts = {}
            for row in rows:
                key = row.get("status", "unknown")
                counts[key] = counts.get(key, 0) + 1
            result["executions"] = {"status": "checked", "counts": counts, "running": len(running), "latest": [
                {"id": str(r["id"]), "status": r.get("status"), "startedAt": r.get("startedAt")}
                for r in sorted(rows, key=lambda r: r.get("startedAt") or "", reverse=True)[:3]]}
        except Blocked as error:
            result["executions"] = {"status": "unknown", "reason": str(error)}
        result["generated"] = {name: p.path(path).is_file() for name, path in p.generated().items()}
        result["outputs"] = {step: all(p.path(path).is_file() for path in p.outputs(step)) for step in REVIEW_STEPS if step != "page"}
        result["artifactVerification"] = "existence_only"
        return result

    def check_weekly(self):
        p = self.progress
        args = [sys.executable, str(self.root / "scripts/generate_analysis_reports.py"), "--through", p.date, "--as-of", p.date,
                "--check", "--check-report", "content/weekly-reports/%s.md" % p.monday]
        result = subprocess.run(args, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if result.returncode:
            raise Blocked("weekly_validation_failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("checked") is not True or value.get("coveredThrough") != p.date or value.get("reviewAsOf") != p.date or value.get("status") not in ("ready", "provisional"):
            raise Blocked("weekly_validation_response_invalid")
        return value

    def verify_collection(self, execution_id):
        workflow, _ = self.workflow("collect")
        return n8n.verify_collection(self.client, self.progress, workflow["id"], execution_id)

    def resume(self):
        result = self.status()
        progress = {}
        saved = self.progress.load()["steps"]
        for step, entry in saved.items():
            state = entry["status"] if self.progress.current(entry) else "stale"
            if state in ("completed", "starting", "submission_unknown"):
                prior_state = state
                try:
                    if step in ("n8n", "dashboard", "page"):
                        client = self.client if step == "n8n" else n8n.Client(self.dashboard, "")
                        if step in ("dashboard", "page") and entry.get("url") != self.dashboard:
                            raise Blocked("dashboard_target_changed")
                        client.request("/healthz" if step == "n8n" else "/api/health")
                        state = "needs_display_recheck" if step == "page" else "completed"
                    elif step.startswith("apply-"):
                        kind = step[6:]
                        workflow, _ = self.workflow(kind)
                        db_apply.verify(kind, db_apply.proposal(self.progress, kind), db_apply.tables(self.client.base, workflow))
                        state = "completed"
                    elif step == "collect":
                        execution_id = entry.get("executionId")
                        if not execution_id:
                            workflow, _ = self.workflow("collect")
                            rows, running = n8n.executions(self.client, workflow["id"], self.progress.date)
                            if running or len(rows) != 1:
                                raise Blocked("collection_execution_unresolved")
                            execution_id = rows[0]["id"]
                        self.verify_collection(execution_id)
                        state = "completed"
                    elif step == "weekly":
                        self.check_weekly()
                except (Blocked, KeyError, ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired):
                    state = prior_state if prior_state in ("starting", "submission_unknown") else "unverified"
            progress[step] = state
        result["progress"] = {step: progress.get(step, "not_recorded") for step in ALL_STEPS}
        result["evidenceFile"] = str(self.progress.file)
        result["reviewVerification"] = "operator_attested; see evidence notes for holds"
        if "weekly" in saved:
            result["weeklyMetricsStatus"] = saved["weekly"].get("metricsStatus", "unknown")
        result["resumePolicy"] = "recheck_before_skip; unknown submissions are never retried"
        return result

    def collect(self):
        p = self.progress
        if p.date != today():
            raise Blocked("collection_only_supports_today_jst")
        workflow, _ = self.workflow("collect", require=True)
        rows, running = n8n.executions(self.client, workflow["id"], p.date)
        if running:
            raise Blocked("collection_already_running")
        if rows:
            if len(rows) != 1 or rows[0].get("status") != "success":
                raise Blocked("existing_execution_requires_review")
            old = p.load()["steps"].get("collect")
            if old and old.get("status") == "completed" and not p.current(old):
                raise Blocked("collection_outputs_changed_requires_review")
            files, count = self.verify_collection(rows[0]["id"])
            p.record("collect", "completed", files=files, executionId=str(rows[0]["id"]), articles=count)
            return {"step": "collect", "status": "reused", "articles": count}
        if "collect" in p.load()["steps"] or any(p.path(path).exists() for path in p.generated().values() if p.date in path):
            raise Blocked("existing_collection_evidence_requires_review")
        p.record("collect", "submission_unknown", files={}, workflowId=str(workflow["id"]))
        response = self.client.request("/webhook/" + n8n.WORKFLOWS["collect"][1], {}, timeout=850)
        if isinstance(response, list) and len(response) == 1:
            response = response[0]
        # Parallel terminal branches can return the file-writer output instead of
        # the summary. The unique successful execution and file evidence below
        # are authoritative; never resend a POST to obtain a different response.
        response_verified = isinstance(response, dict) and response.get("saved") is True and response.get("date") == p.date
        rows, running = n8n.executions(self.client, workflow["id"], p.date)
        if running or len(rows) != 1:
            raise Blocked("collection_execution_unresolved_do_not_retry")
        files, count = self.verify_collection(rows[0]["id"])
        p.record("collect", "completed", files=files, executionId=str(rows[0]["id"]), articles=count, webhookResponseVerified=response_verified)
        return {"step": "collect", "status": "completed", "articles": count, "verification": "execution_and_files", "webhookResponseVerified": response_verified}

    def checkpoint(self, step, evidence, note):
        weekly = self.check_weekly() if step == "weekly" else None
        if step == "page":
            n8n.Client(self.dashboard, "").request("/api/health")
        result = self.progress.checkpoint(step, evidence, note)
        if step in ("weekly", "page"):
            entry = self.progress.load()["steps"][step]
            extra = {k: v for k, v in entry.items() if k not in ("status", "checkedAt", "target")}
            if step == "page":
                extra["url"] = self.dashboard
            else:
                extra["metricsStatus"] = weekly.get("status", "unknown")
            self.progress.record(step, "completed", **extra)
        return result

    def apply(self, kind):
        if kind == "all":
            return {"steps": [self.apply("talent"), self.apply("classification")]}
        p = self.progress
        review = "talent-review" if kind == "talent" else "classification-review"
        entry = p.load()["steps"].get(review, {})
        if entry.get("status") != "completed" or not p.current(entry):
            raise Blocked("proposal_review_missing_or_stale")
        value = db_apply.proposal(p, kind)
        workflow, _ = self.workflow(kind, require=True)
        _, running = n8n.executions(self.client, workflow["id"], p.date)
        if running:
            raise Blocked("apply_workflow_already_running")
        current = db_apply.tables(self.client.base, workflow)
        db_apply.preflight(kind, value, current)
        old = p.load()["steps"].get("apply-" + kind)
        if old:
            if not p.current(old):
                raise Blocked("previous_apply_inputs_changed_requires_review")
            db_apply.verify(kind, value, current)
            p.record("apply-" + kind, "completed", files=entry["files"], verification="db_content_match", workflowId=str(workflow["id"]))
            return {"step": "apply-" + kind, "status": "reconciled", "verification": "db_content_match"}
        p.record("apply-" + kind, "submission_unknown", files=entry["files"], workflowId=str(workflow["id"]))
        response = self.client.request("/webhook/" + n8n.WORKFLOWS[kind][1], value, timeout=850)
        fields = ("articles", "talents", "articleTalents") if kind == "talent" else ("classifications",)
        counts = {f: len(value[f]) for f in fields}
        if not isinstance(response, dict) or response.get("accepted") is not True or response.get("proposalDate") != p.date or response.get("counts") != counts:
            raise Blocked("apply_response_unverified_do_not_retry")
        db_apply.verify(kind, value, db_apply.tables(self.client.base, workflow))
        p.record("apply-" + kind, "completed", files=entry["files"], verification="db_content_match", workflowId=str(workflow["id"]))
        return {"step": "apply-" + kind, "status": "completed", "counts": counts, "verification": "db_content_match"}

    def start(self, service):
        client = self.client if service == "n8n" else n8n.Client(self.dashboard, "")
        url = urllib.parse.urlsplit(client.base)
        if url.hostname not in ("localhost", "127.0.0.1", "::1") or url.path or url.scheme != "http":
            raise Blocked("startup_requires_local_http_target")
        path = "/healthz" if service == "n8n" else "/api/health"
        try:
            client.request(path)
        except Blocked:
            pass
        else:
            self.progress.record(service, "completed", files={}, verification="http_reachable", url=client.base)
            return {"step": service, "status": "already_reachable", "url": client.base}
        try:
            with socket.create_connection((url.hostname, url.port or 80), timeout=2):
                raise Blocked("port_occupied_but_health_unverified")
        except OSError:
            pass
        marker = self.progress.directory / (service + "-process.json")
        if marker.exists():
            previous = read_json(marker)
            try:
                os.kill(previous["pid"], 0)
                raise Blocked("previous_process_alive_check_startup_log")
            except ProcessLookupError:
                pass
        log = self.root / ".operation-logs" / (service + "-" + uuid.uuid4().hex + ".log")
        log.parent.mkdir(mode=0o700, exist_ok=True)
        env = self.startup_env.copy()
        if service == "n8n":
            env.update(N8N_PORT=str(url.port or 80), N8N_HOST=url.hostname, N8N_PROTOCOL="http")
        else:
            env.update(TALENT_DASHBOARD_HOST=url.hostname, TALENT_DASHBOARD_PORT=str(url.port or 80))
            for key in ("N8N_DATABASE_PATH", "N8N_USER_FOLDER"):
                if key in os.environ:
                    env[key] = os.environ[key]
        script = "start_n8n_with_file_access.sh" if service == "n8n" else "start_talent_dashboard.sh"
        with log.open("xb") as stream:
            os.chmod(str(log), 0o600)
            process = subprocess.Popen(["bash", str(self.root / "scripts" / script)], cwd=str(self.root), env=env,
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        write_json(marker, {"pid": process.pid, "startedAt": now(), "log": str(log), "url": client.base})
        self.progress.record(service, "starting", files={}, pid=process.pid, log=str(log), url=client.base)
        for _ in range(20):
            if process.poll() is not None:
                raise Blocked("server_exited_check_startup_log")
            try:
                client.request(path, timeout=1)
            except Blocked:
                time.sleep(1)
                continue
            self.progress.record(service, "completed", files={}, verification="http_reachable", pid=process.pid, log=str(log), url=client.base)
            return {"step": service, "status": "reachable", "url": client.base, "log": str(log)}
        raise Blocked("startup_not_ready_process_may_still_be_running")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Target collection date (JST); default today")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "resume", "collect"):
        subs.add_parser(command)
    token_usage.add_arguments(subs.add_parser("tokens", help="Record per-step Codex tokens and daily Markdown"))
    start = subs.add_parser("start")
    start.add_argument("service", choices=("n8n", "dashboard"))
    apply = subs.add_parser("apply")
    apply.add_argument("--kind", choices=("all", "talent", "classification"), default="all")
    checkpoint = subs.add_parser("checkpoint")
    checkpoint.add_argument("step", choices=REVIEW_STEPS)
    checkpoint.add_argument("--evidence", action="append", required=True, help="Existing project review/validation evidence file; repeatable")
    checkpoint.add_argument("--note", required=True, help="What was verified, including holds/provisional state; no secrets")
    args = parser.parse_args(argv)
    try:
        startup_env = os.environ.copy()
        load_env_file(ROOT / ".env")
        if args.command == "tokens":
            result = token_usage.execute(args, root=ROOT, run_date=args.date)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0
        ops = Operations(run_date=args.date, startup_env=startup_env)
        if args.command in ("status", "resume"):
            result = getattr(ops, args.command)()
        else:
            with ops.progress.lock():
                if args.command == "start":
                    result = ops.start(args.service)
                elif args.command == "collect":
                    result = ops.collect()
                elif args.command == "apply":
                    result = ops.apply(args.kind)
                else:
                    result = ops.checkpoint(args.step, args.evidence, args.note)
        if args.command == "checkpoint":
            result["tokenUsage"] = token_usage.checkpoint_finished(ROOT, args.step, ops.progress.date)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (Blocked, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        reason = str(error) if isinstance(error, Blocked) else type(error).__name__
        print(json.dumps({"status": "blocked", "reason": reason, "next": "inspect relevant configuration/evidence; do not retry POST blindly"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
