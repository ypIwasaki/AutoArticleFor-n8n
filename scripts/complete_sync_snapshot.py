"""Create and restore a closed, unpublished-until-verified recovery bundle."""
from contextlib import closing
from pathlib import Path
import json
import os
import shutil
import sqlite3
import tempfile
import database_phase1 as base
import project_database as db
import sync_snapshot_dependencies as deps


def inventory(root):
    root=Path(root);result=[]
    for directory in base.TARGETS:
        for p in sorted((root/'content'/directory).rglob('*')):
            if p.is_file():result.append({'path':str(p.relative_to(root)),'size':p.stat().st_size,'sha256':base.sha(p)})
    return sorted(result,key=lambda x:x['path'])


def legacy_copy(source,destination):
    with closing(base.ro(source)) as old,closing(sqlite3.connect(destination)) as new:
        old.execute('PRAGMA foreign_keys=ON');new.execute('PRAGMA foreign_keys=ON');old.backup(new)
    with closing(base.ro(destination)) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('legacy_backup_integrity_failed')


def capture(root,legacy,project,destination,completion=None,operation_state=None,hook=None):
    root=Path(root).resolve();destination=Path(destination).resolve()
    if destination.exists():raise RuntimeError('snapshot_destination_exists')
    os.umask(0o077);destination.parent.mkdir(parents=True,exist_ok=True)
    policy,implementation=deps.capture_ledgers(root)
    files=inventory(root)
    temporary=Path(tempfile.mkdtemp(prefix=destination.name+'.incomplete-',dir=destination.parent))
    try:
        deps.copy_dependencies(root,temporary,policy,implementation)
        for item in files:
            out=temporary/'files'/item['path'];out.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(root/item['path'],out);deps.check_file(temporary/'files',item)
        if hook:hook(temporary)
        deps.assert_live_unchanged(root,policy,implementation)
        legacy_copy(legacy,temporary/'n8n.sqlite')
        project_backup=db.backup(temporary/'project.sqlite',project)
        with closing(base.ro(temporary/'n8n.sqlite')) as c:
            saved=base.baseline(c)
        base.save(temporary/'database-baseline.json',saved)
        with closing(db.connect(project,readonly=True)) as c:
            row=c.execute('SELECT input_snapshot FROM migration_runs ORDER BY started_at LIMIT 1').fetchone()
            epoch=base.now()
            if row:
                meta=Path(row[0])/'phase1-result.json'
                if not meta.is_file():raise RuntimeError('projection_epoch_unavailable')
                epoch=json.loads(meta.read_text())['finished_at']
            versions=[dict(x) for x in c.execute('SELECT * FROM schema_migrations ORDER BY version')]
            cutover=[dict(x) for x in c.execute('SELECT * FROM cutover_state ORDER BY feature')]
        deps.assert_live_unchanged(root,policy,implementation)
        if inventory(root)!=files:raise RuntimeError('business_files_changed_during_capture')
        with closing(base.ro(legacy)) as c:
            c.execute('BEGIN');current=base.baseline(c);c.rollback()
        if saved!=current:raise RuntimeError('legacy_changed_during_capture')
        from compare_phase4_database import fingerprint
        if fingerprint(project)!=fingerprint(temporary/'project.sqlite'):
            raise RuntimeError('project_changed_during_capture')
        base.save(temporary/'input-files.json',files)
        if completion is not None:base.save(temporary/'legacy-completion.json',completion)
        if operation_state is not None:base.save(temporary/'operation-state.json',operation_state)
        base.save(temporary/'phase1-result.json',{'status':'complete','backup_sha256':base.sha(temporary/'n8n.sqlite'),'project_backup_sha256':project_backup['sha256'],'finished_at':base.now(),'projection_epoch':epoch,'purpose':'complete_phase5_recovery_bundle'})
        deps.verify(temporary,root)
        deps.assert_live_unchanged(root,policy,implementation)
        manifest=[{'path':str(p.relative_to(temporary)),'size':p.stat().st_size,'sha256':base.sha(p)} for p in sorted(temporary.rglob('*')) if p.is_file()]
        base.save(temporary/'bundle.json',{'status':'complete','files':manifest,'schema_versions':versions,'cutover':cutover,'captured_at':base.now()})
        # Publication is a single directory rename. Failed captures remain
        # quarantined for investigation; they never become a usable snapshot.
        temporary.rename(destination)
        return {'status':'complete','snapshot':str(destination),'business_files':len(files),'policy_files':len(policy['files']),'implementation_files':len(implementation['files'])}
    except Exception as exc:
        base.save(temporary/'capture-failure.json',{'status':'failed','reason':type(exc).__name__,'published':False})
        raise


def verify_bundle(snapshot):
    snapshot=Path(snapshot);bundle=json.loads((snapshot/'bundle.json').read_text())
    if bundle['status']!='complete':raise RuntimeError('incomplete_bundle')
    for item in bundle['files']:deps.check_file(snapshot,item)
    deps.verify(snapshot)
    return bundle


def restore(snapshot,destination):
    snapshot=Path(snapshot);destination=Path(destination)
    if destination.exists():raise RuntimeError('restore_destination_exists')
    bundle=verify_bundle(snapshot);os.umask(0o077);destination.mkdir(parents=True)
    for item in bundle['files']:
        out=destination/item['path'];out.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(snapshot/item['path'],out);deps.check_file(destination,item)
    shutil.copyfile(snapshot/'bundle.json',destination/'bundle.json')
    verify_bundle(destination)
    with closing(base.ro(destination/'n8n.sqlite')) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('restored_legacy_integrity_failed')
        if base.baseline(c)!=json.loads((snapshot/'database-baseline.json').read_text()):raise RuntimeError('restored_legacy_difference')
    with closing(db.connect(destination/'project.sqlite',readonly=True)) as c:
        c.execute('PRAGMA cache_size=-262144')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('restored_project_integrity_failed')
    from compare_phase4_database import fingerprint
    if fingerprint(snapshot/'project.sqlite')!=fingerprint(destination/'project.sqlite'):raise RuntimeError('restored_project_difference')
    return {'status':'passed','restored_files':len(bundle['files']),'legacy_equal':True,'project_equal':True,'policies_equal':True,'implementation_equal':True}
