"""Capture a completed legacy operation without writing legacy business data."""
from contextlib import closing
from pathlib import Path
import json
import os
import shutil
import sqlite3
import database_phase1 as base
import continuous_database_sync as sync
import project_database as db


def inventory(root):
    result=[]
    for directory in base.TARGETS:
        for p in sorted((root/'content'/directory).rglob('*')):
            if p.is_file():result.append({'path':str(p.relative_to(root)),'size':p.stat().st_size,'sha256':base.sha(p)})
    return sorted(result,key=lambda r:r['path'])


def live_guard(snapshot,root,database):
    import sync_snapshot_dependencies as dependencies
    snapshot=Path(snapshot)
    ledgers=dependencies.verify(snapshot)
    dependencies.assert_live_unchanged(root,ledgers['policy_dependencies'],ledgers['implementation_dependencies'])
    expected=json.loads((snapshot/'database-baseline.json').read_text())
    with closing(base.ro(database)) as c:
        if c.execute("SELECT count(*) FROM execution_entity WHERE status IN ('new','running','waiting')").fetchone()[0]:
            raise sync.SyncStopped('legacy_execution_in_progress')
        c.execute('BEGIN');actual=base.baseline(c);c.rollback()
    if actual!=expected:raise sync.SyncStopped('live_database_changed')
    if inventory(root)!=json.loads((snapshot/'input-files.json').read_text()):
        raise sync.SyncStopped('live_files_changed')


def prepare(operation_id,run_date,step,root,database,folder,supersedes_snapshot=None,supersedes_hash=None):
    from autoarticle_ops import Operations
    from sync_workflow_to_n8n import load_env_file
    if not db.ID_PATTERN.fullmatch(operation_id):raise ValueError('Stable operation ID required')
    root=Path(root).resolve();folder=Path(folder).resolve()
    if folder.exists():raise sync.SyncStopped('existing_snapshot_requires_same_id_status_check')
    load_env_file(root/'.env')
    ops=Operations(root,run_date)
    if step not in ('collect','summary','talent-review','classification-review','apply-talent','apply-classification'):
        raise ValueError('Unsupported completed operation')
    observed=ops.resume()
    entry=ops.progress.load()['steps'].get(step,{})
    if observed['progress'].get(step)!='completed' or entry.get('status')!='completed':
        raise sync.SyncStopped('legacy_completion_unverified')
    evidence={'kind':'validated-checkpoint','status':'complete','step':step,'entry_hash':sync.digest(entry),'completed_at':entry['checkedAt'],'operation_id':operation_id}
    if step=='collect':
        evidence.update(kind='n8n-execution',execution_id=entry['executionId'])
    if bool(supersedes_snapshot)!=bool(supersedes_hash):raise ValueError('Both superseded snapshot and request hash are required')
    if supersedes_snapshot:
        old_request=json.loads((Path(supersedes_snapshot)/'sync-request.json').read_text())
        old=old_request['receipt']['legacy_completion']
        if sync.digest(old_request)!=supersedes_hash:raise sync.SyncStopped('superseded_request_hash_mismatch')
        if old.get('entry_hash')!=evidence.get('entry_hash') or old.get('step')!=step or old.get('operation_id')==operation_id:raise sync.SyncStopped('superseded_request_not_same_business')
        if sync.operation_status(old['operation_id'],db.database_path()) is not None:raise sync.SyncStopped('superseded_operation_has_database_state')
        evidence['supersedes']={'operation_id':old['operation_id'],'request_hash':supersedes_hash,'snapshot_hash':old_request['receipt']['snapshot_hash'],'same_business_entry_hash':evidence['entry_hash'],'verified_no_sync_run':True}

    import complete_sync_snapshot as complete
    import sync_snapshot_dependencies as dependencies
    result=complete.capture(root,database,db.database_path(),folder,evidence,ops.progress.load())
    live_guard(folder,root,database)
    if sync.digest(ops.progress.load()['steps'].get(step,{}))!=evidence['entry_hash']:
        raise sync.SyncStopped('legacy_completion_changed')
    dependencies.verify(folder)
    return dict(result,operationId=operation_id,status='prepared')
