"""Bounded read-only n8n inspection and explicit, single-shot HTTP requests."""
from __future__ import annotations

import base64
import hashlib
import json
from contextlib import closing
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
        # bounded allowance restricted to single-execution inspection and the official
        # retry response, which also includes full execution data.
        limit = 256 * 1024 * 1024 if api and (body is None and re.fullmatch(r"/executions/[^/?]+\?includeData=true", path) or body is not None and re.fullmatch(r"/executions/[^/?]+/retry", path)) else 32 * 1024 * 1024
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
    if 'Save Collection to Project DB' in runs:
        from contextlib import closing
        import project_database as project_db
        import project_write_outbox
        saved=runs['Save Collection to Project DB'][-1]['data']['main'][0][0]['json']
        state=project_write_outbox.status(saved.get('operationId'))
        if saved.get('writeTarget')!='project-db' or saved.get('compatibility')!='complete' or not state or state['status']!='complete' or state['pendingDeliveries']:
            raise Blocked('project_collection_save_incomplete')
        with closing(project_db.connect(readonly=True)) as database:
            matches=database.execute('SELECT id FROM collection_runs WHERE run_date=? AND workflow_execution_id=?',(progress.date,str(execution_id))).fetchall()
            if len(matches)!=1 or database.execute('SELECT count(*) FROM article_occurrences WHERE collection_run_id=?',(matches[0][0],)).fetchone()[0]!=len(articles):
                raise Blocked('project_collection_destination_mismatch')
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


def collection_success(rows, entry):
    """Only one successful execution, or its explicitly recorded linear retry family."""
    if len(rows) == 1 and rows[0].get('status') == 'success':
        return rows[0]['id']
    if not entry.get('retryOf') or not entry.get('executionId'):
        raise Blocked('existing_execution_requires_review')
    by_id = {str(row['id']): row for row in rows}
    current = str(entry['executionId'])
    if current not in by_id or by_id[current].get('status') != 'success':
        raise Blocked('collection_retry_not_successful')
    seen = set()
    while current:
        if current in seen or current not in by_id:
            raise Blocked('collection_retry_lineage_invalid')
        row = by_id[current]
        if seen and row.get('status') != 'error':
            raise Blocked('collection_retry_lineage_invalid')
        seen.add(current)
        current = str(row['retryOf']) if row.get('retryOf') else None
    if seen != set(by_id) or str(by_id[str(entry['executionId'])].get('retryOf')) != str(entry['retryOf']):
        raise Blocked('collection_retry_lineage_invalid')
    return entry['executionId']


def collection_retry_preflight(ops, execution_id):
    """Read-only guard for an explicitly requested, pre-save RSS failure retry."""
    from autoarticle_progress import today
    import project_database
    p = ops.progress
    execution_id = str(execution_id)
    if p.date != today() or not execution_id.isdigit():
        raise Blocked('collection_retry_requires_today_and_execution_id')
    workflow, _ = ops.workflow('collect', require=True)
    rows, running = executions(ops.client, workflow['id'], p.date)
    if running:
        raise Blocked('collection_already_running')
    # A repeated invocation may inspect a linked successful child, but never POST again.
    children = [x for x in rows if str(x.get('retryOf')) == execution_id]
    if children:
        if len(children) != 1 or children[0].get('status') != 'success':
            raise Blocked('collection_retry_already_exists_requires_review')
        child = children[0]
        collection_success(rows, {'retryOf':execution_id, 'executionId':str(child['id'])})
        return {'status':'already_succeeded','executionId':str(child['id']),'retryOf':execution_id}
    by_id = {str(row['id']):row for row in rows}
    current, seen = execution_id, set()
    while current:
        if current in seen or current not in by_id or by_id[current].get('status') != 'error':
            raise Blocked('collection_retry_lineage_invalid')
        seen.add(current)
        current = str(by_id[current]['retryOf']) if by_id[current].get('retryOf') else None
    if seen != set(by_id):
        raise Blocked('unrelated_collection_execution_exists')
    saved = p.load()['steps'].get('collect', {})
    if str(saved.get('retryOf')) == execution_id:
        raise Blocked('collection_retry_submission_unknown_do_not_resubmit')
    execution = ops.client.request('/executions/'+execution_id+'?includeData=true', api=True)
    result = execution.get('data', {}).get('resultData', {})
    if str(execution.get('workflowId')) != str(workflow['id']) or execution.get('status') != 'error':
        raise Blocked('collection_retry_target_not_failed')
    allowed = {'Keyword Summary Webhook','Daily Schedule','Manual Trigger','Read Keyword Configuration',
               'Parse Keyword Configuration','Load Talent Registry','Build Keyword Summary Request',
               'Build Search RSS URLs','Read RSS Search Results','Fetch One RSS Feed','Attach RSS Search Provenance'}
    runs = result.get('runData', {})
    if not runs or not set(runs).issubset(allowed) or result.get('lastNodeExecuted') not in {'Read RSS Search Results','Fetch One RSS Feed'}:
        raise Blocked('collection_retry_may_have_saved_data')
    error = result.get('error', {})
    error_text = json.dumps({k:error.get(k) for k in ('message','messages')}).lower()
    if not any(code in error_text for code in ('timed out','timeout','etimedout','econnreset')):
        raise Blocked('collection_retry_failure_requires_review')
    if any(p.path(path).exists() for path in p.generated().values() if p.date in path):
        raise Blocked('collection_retry_outputs_already_exist')
    with closing(project_database.connect(ops.root/'data/autoarticle.sqlite',readonly=True)) as c:
        if c.execute('SELECT 1 FROM collection_runs WHERE run_date=?',(p.date,)).fetchone():
            raise Blocked('collection_retry_db_already_saved')
        if c.execute("SELECT 1 FROM compatibility_deliveries WHERE status='pending' LIMIT 1").fetchone():
            raise Blocked('collection_retry_pending_compatibility')
        for old_id in seen:
            if c.execute('SELECT 1 FROM sync_runs WHERE id=?',('db-collect-'+str(workflow['id'])+'-'+old_id,)).fetchone():
                raise Blocked('collection_retry_write_request_exists')
    return {'status':'ready','retryOf':execution_id,'workflowId':str(workflow['id']),
            'failedNode':result['lastNodeExecuted'],'databaseSaved':False,'readOnly':True}
