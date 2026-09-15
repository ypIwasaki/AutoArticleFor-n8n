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
    snapshot=Path(snapshot)
    expected=json.loads((snapshot/'database-baseline.json').read_text())
    with closing(base.ro(database)) as c:
        if c.execute("SELECT count(*) FROM execution_entity WHERE status IN ('new','running','waiting')").fetchone()[0]:
            raise sync.SyncStopped('legacy_execution_in_progress')
        c.execute('BEGIN');actual=base.baseline(c);c.rollback()
    if actual!=expected:raise sync.SyncStopped('live_database_changed')
    if inventory(root)!=json.loads((snapshot/'input-files.json').read_text()):
        raise sync.SyncStopped('live_files_changed')


def prepare(operation_id,run_date,step,root,database,folder):
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
    os.umask(0o077);folder.mkdir(parents=True)
    base.save(folder/'legacy-completion.json',evidence);base.save(folder/'operation-state.json',ops.progress.load())
    files=inventory(root);base.save(folder/'input-files.json',files)
    for item in files:
        dest=folder/'files'/item['path'];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/item['path'],dest)
        if base.sha(dest)!=item['sha256']:raise sync.SyncStopped('source_changed_during_snapshot')
    with closing(base.ro(database)) as source,closing(sqlite3.connect(folder/'n8n.sqlite')) as dest:
        dest.execute('PRAGMA foreign_keys=ON');source.backup(dest)
    with closing(base.ro(folder/'n8n.sqlite')) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise sync.SyncStopped('source_snapshot_integrity_failed')
        base.save(folder/'database-baseline.json',base.baseline(c))
    live_guard(folder,root,database)
    if sync.digest(ops.progress.load()['steps'].get(step,{}))!=evidence['entry_hash']:
        raise sync.SyncStopped('legacy_completion_changed')
    base.save(folder/'phase1-result.json',{'status':'complete','backup_sha256':base.sha(folder/'n8n.sqlite'),'finished_at':base.now(),'purpose':'phase5_completed_operation_snapshot'})
    return {'operationId':operation_id,'snapshot':str(folder),'status':'prepared'}
