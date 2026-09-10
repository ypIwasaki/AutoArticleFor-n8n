"""Bounded read-only n8n inspection and explicit, single-shot HTTP requests."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from autoarticle_progress import Blocked, JST, read_json, timestamp

WORKFLOWS = {
    "collect": ("daily-keyword-news-summary", "daily-keyword-summary/request"),
    "talent": ("apply-talent-index-proposal", "talent-index/apply"),
    "classification": ("apply-article-classification-proposal", "article-classification/apply"),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


class Client:
    def __init__(self, base, key, timeout=5):
        base = base.rstrip("/")
        self.base = base[:-7] if base.endswith("/api/v1") else base
        url = urllib.parse.urlsplit(self.base)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise Blocked("invalid_base_url")
        self.key, self.timeout = key, timeout

    def request(self, path, body=None, api=False, timeout=None):
        # Execution detail includes every RSS item and node output. Keep the larger
        # bounded allowance restricted to read-only, single-execution inspection.
        limit = 256 * 1024 * 1024 if api and body is None and re.fullmatch(r"/executions/[^/?]+\?includeData=true", path) else 32 * 1024 * 1024
        if api and not self.key:
            raise Blocked("api_key_missing")
        headers = {"Accept": "application/json"}
        if api:
            headers["X-N8N-API-KEY"] = self.key
            path = "/api/v1" + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise Blocked("response_too_large")
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise Blocked("http_" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise Blocked("connection_unavailable_or_timeout") from None
        except (ValueError, UnicodeError):
            raise Blocked("invalid_response") from None

    def pages(self, path):
        rows, seen = [], set()
        for _ in range(50):
            payload = self.request(path, api=True)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or any(not isinstance(r, dict) for r in payload["data"]):
                raise Blocked("invalid_list_response")
            rows.extend(payload["data"])
            cursor = payload.get("nextCursor")
            if not cursor:
                return rows
            if cursor in seen:
                raise Blocked("repeated_cursor")
            seen.add(cursor)
            path = re.sub(r"&cursor=[^&]*", "", path) + "&cursor=" + urllib.parse.quote(str(cursor), safe="")
        raise Blocked("history_scan_limit")


def workflow(client, root, kind, explicit=None):
    local = read_json(root / "n8n/workflows" / (WORKFLOWS[kind][0] + ".workflow.json"))
    if not explicit:
        matches = [w for w in client.pages("/workflows?limit=100") if w.get("name") == local["name"]]
        if len(matches) != 1:
            raise Blocked("workflow_missing_or_ambiguous")
        explicit = str(matches[0]["id"])
    remote = client.request("/workflows/" + urllib.parse.quote(explicit, safe=""), api=True)
    if not isinstance(remote, dict) or remote.get("name") != local["name"] or not remote.get("id"):
        raise Blocked("workflow_identity_mismatch")
    def definition(w):
        # Layout and server metadata do not affect execution; credentials and error settings do.
        fields = ("id", "name", "type", "typeVersion", "parameters", "credentials", "disabled", "onError", "continueOnFail", "retryOnFail", "maxTries", "waitBetweenTries", "alwaysOutputData", "executeOnce")
        return {"nodes": sorted([{k: n[k] for k in fields if k in n} for n in w.get("nodes", [])], key=lambda n: n["name"]),
                "connections": w.get("connections"), "settings": {k: w.get("settings", {}).get(k) for k in local.get("settings", {})}}
    return remote, definition(remote) == definition(local)


def executions(client, workflow_id, run_date):
    rows = client.pages("/executions?limit=100&includeData=false&workflowId=" + urllib.parse.quote(str(workflow_id), safe=""))
    selected, running = [], []
    for row in rows:
        if str(row.get("workflowId")) != str(workflow_id):
            raise Blocked("execution_workflow_mismatch")
        if row.get("status") in ("new", "running", "waiting"):
            running.append(row)
        try:
            dates = [timestamp(row[key]).astimezone(JST).date().isoformat() for key in ("startedAt", "stoppedAt") if row.get(key)]
        except (ValueError, TypeError):
            raise Blocked("invalid_execution_time")
        if not dates:
            raise Blocked("execution_time_unknown")
        if run_date in dates:
            selected.append(row)
    return selected, running


def verify_collection(client, progress, workflow_id, execution_id):
    execution = client.request("/executions/%s?includeData=true" % urllib.parse.quote(str(execution_id), safe=""), api=True)
    if str(execution.get("workflowId")) != str(workflow_id) or execution.get("status") != "success":
        raise Blocked("collection_execution_not_successful")
    runs = execution.get("data", {}).get("resultData", {}).get("runData", {})
    try:
        node = runs["Build Structured Records"][-1]["data"]["main"][0][0]["json"]
        if node["date"] != progress.date:
            raise Blocked("execution_artifact_date_mismatch")
        with progress.path(progress.generated()["structured-records"]).open(encoding="utf-8") as stream:
            header = json.loads(next(stream))
            articles = [json.loads(line) for line in stream if line.strip()]
        generated = timestamp(header["generatedAt"])
        if not timestamp(execution["startedAt"]) <= generated <= timestamp(execution["stoppedAt"]):
            raise Blocked("archive_does_not_match_execution_time")
        if header.get("recordType") != "run" or header["runDate"] != progress.date or header.get("capturedArticleCount") != len(articles) or node["capturedArticleCount"] != len(articles):
            raise Blocked("archive_count_mismatch")
        if any(row.get("recordType") != "article" or row.get("runDate") != progress.date for row in articles):
            raise Blocked("invalid_archive_rows")
    except (KeyError, IndexError, TypeError, ValueError, OSError, StopIteration):
        raise Blocked("collection_evidence_missing_or_invalid")
    files = progress.fingerprints(progress.generated().values())
    if not all(files.values()):
        raise Blocked("generated_files_missing")
    try:
        markdown_items = runs["Build Markdown Files"][-1]["data"]["main"][0]
        emitted = {item["json"]["relativePath"]: item for item in markdown_items}
        for relative in progress.generated().values():
            if relative.endswith(".jsonl"):
                continue
            item = emitted[relative]
            if item["json"]["date"] != progress.date:
                raise Blocked("markdown_execution_date_mismatch")
            encoded = item.get("binary", {}).get("data", {}).get("data", "")
            if encoded and encoded not in ("filesystem", "s3"):
                try:
                    expected_hash = hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
                except ValueError:
                    raise Blocked("markdown_binary_unverifiable")
                if expected_hash != files[relative]:
                    raise Blocked("markdown_content_mismatch")
            else:
                # n8n may store binaries externally. Require the emitted path and write time.
                written = progress.path(relative).stat().st_mtime
                if not timestamp(execution["startedAt"]).timestamp() - 2 <= written <= timestamp(execution["stoppedAt"]).timestamp() + 2:
                    raise Blocked("markdown_write_time_unverified")
    except (KeyError, IndexError, TypeError):
        raise Blocked("markdown_execution_evidence_missing")
    return files, len(articles)
