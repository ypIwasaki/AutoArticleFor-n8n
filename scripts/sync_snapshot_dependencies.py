"""Closed dependency manifests for reproducible phase 5 snapshots."""
from pathlib import Path
import json
import shutil
import article_review_facts as review
import database_phase1 as base
import project_database as db

VERSION='snapshot-dependencies-v5-prior-evidence'
import missing_body_acceptance as acceptance
import identity_history_dependencies as identity_history

class DependencyError(RuntimeError):
    pass


def safe_path(root, relative):
    p=Path(relative)
    if p.is_absolute() or '..' in p.parts or str(p)!=relative:
        raise DependencyError('invalid_dependency_path')
    resolved=(Path(root)/p).resolve()
    if Path(root).resolve() not in resolved.parents:
        raise DependencyError('dependency_outside_snapshot')
    return resolved


def implementation_paths(root):
    root=Path(root)
    # Include transitive local Python helpers and configuration, not just the
    # entry points. This also covers changes in parsers and snapshot validation.
    paths=list((root/'scripts').glob('*.py'))+list((root/'database/migrations').glob('[0-9][0-9][0-9]_*.sql'))
    paths+=list((root/'config').rglob('*.json'))
    required={'scripts/article_review_facts.py','scripts/import_legacy_database.py',
              'scripts/legacy_sync_projection.py','scripts/continuous_database_sync.py',
              'scripts/project_database.py','scripts/sync_snapshot_dependencies.py',
              'scripts/prepare_legacy_sync.py'}
    names={str(p.relative_to(root)) for p in paths}
    if not required<=names:raise DependencyError('required_implementation_missing')
    return sorted(names)


def entry(root,path,stamp,processing_version=None):
    p=safe_path(root,path)
    if not p.is_file():raise DependencyError('required_dependency_missing:'+path)
    result={'path':path,'size':p.stat().st_size,'sha256':base.sha(p),'captured_at':stamp}
    if processing_version is not None:result['processing_version']=processing_version
    return result


def capture_ledgers(root):
    import import_legacy_database as importer
    import continuous_database_sync as sync
    root=Path(root);stamp=base.now()
    policy={'format':VERSION,'review_version':review.REVIEW_VERSION,
            'files':[entry(root,p,stamp) for p in review.POLICY_FILES],
            'policy_hash':review.policy_hash(root)}
    if policy['policy_hash'] is None:raise DependencyError('policy_hash_unavailable')
    versions={'scripts/article_review_facts.py':str(review.REVIEW_VERSION),
              'scripts/import_legacy_database.py':importer.VERSION,
              'scripts/continuous_database_sync.py':sync.VERSION,
              'scripts/legacy_sync_projection.py':sync.VERSION,
              'scripts/project_database.py':db.BACKUP_VERSION}
    decision=entry(root,acceptance.LEDGER_PATH,stamp)
    acceptance.load(root/acceptance.LEDGER_PATH)
    decision['format_version']=acceptance.VERSION
    policy['acceptance_decision']=decision
    policy['identity_history']=identity_history.capture_registry(root)
    implementation={'format':VERSION,'files':[entry(root,p,stamp,versions.get(p,VERSION)) for p in implementation_paths(root)]}
    return policy,implementation


def copy_dependencies(root,snapshot,policy,implementation):
    snapshot=Path(snapshot)
    for ledger,destination in ((policy,snapshot/'files'),(implementation,snapshot/'implementation')):
        for item in ledger['files']:
            p=destination/item['path'];p.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(safe_path(root,item['path']),p)
            check_file(destination,item)
    item=policy['acceptance_decision'];out=snapshot/'files'/item['path'];out.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(safe_path(root,item['path']),out);check_file(snapshot/'files',item)
    identity_history.copy(root,snapshot,policy['identity_history'])
    base.save(snapshot/'policy-dependencies.json',policy)
    base.save(snapshot/'implementation-dependencies.json',implementation)


def check_file(root,item):
    p=safe_path(root,item['path'])
    if not p.is_file():raise DependencyError('required_dependency_missing:'+item['path'])
    if p.stat().st_size!=item['size'] or base.sha(p)!=item['sha256']:
        raise DependencyError('dependency_hash_mismatch:'+item['path'])


def verify(snapshot,runtime_root=None):
    snapshot=Path(snapshot);runtime_root=Path(runtime_root or db.ROOT)
    try:
        policy=json.loads((snapshot/'policy-dependencies.json').read_text())
        implementation=json.loads((snapshot/'implementation-dependencies.json').read_text())
    except (FileNotFoundError,json.JSONDecodeError) as exc:
        raise DependencyError('dependency_ledger_missing_or_invalid') from exc
    if policy.get('format')!=VERSION or implementation.get('format')!=VERSION:
        raise DependencyError('dependency_format_changed')
    if policy.get('review_version')!=review.REVIEW_VERSION:
        raise DependencyError('review_version_changed')
    decision=policy.get('acceptance_decision',{})
    if decision.get('path')!=acceptance.LEDGER_PATH or decision.get('format_version')!=acceptance.VERSION:raise DependencyError('acceptance_dependency_missing_or_wrong_version')
    check_file(snapshot/'files',decision)
    acceptance.load(snapshot/'files'/decision['path'])
    identity_history.verify(snapshot,policy.get('identity_history',{}))
    required=list(review.POLICY_FILES);names=[x['path'] for x in policy['files']]
    if len(names)!=len(set(names)) or set(names)!=set(required):
        raise DependencyError('unmanifested_or_missing_policy')
    for item in policy['files']:check_file(snapshot/'files',item)
    if not policy.get('policy_hash') or review.policy_hash(snapshot/'files')!=policy['policy_hash']:
        raise DependencyError('policy_hash_not_reproducible')
    names=[x['path'] for x in implementation['files']]
    if len(names)!=len(set(names)) or set(names)!=set(implementation_paths(runtime_root)):
        raise DependencyError('implementation_inventory_changed')
    for item in implementation['files']:
        check_file(snapshot/'implementation',item)
        check_file(runtime_root,item)
    return {'policy_dependencies':policy,'implementation_dependencies':implementation}


def assert_live_unchanged(root,policy,implementation):
    if review.REVIEW_VERSION!=policy['review_version']:raise DependencyError('review_version_changed_during_capture')
    for ledger in (policy,implementation):
        for item in ledger['files']:check_file(root,item)
    check_file(root,policy['acceptance_decision'])
    identity_history.assert_live(root,policy['identity_history'])
    if review.policy_hash(Path(root))!=policy['policy_hash']:
        raise DependencyError('policy_changed_during_capture')


def verify_project_snapshot(snapshot):
    snapshot=Path(snapshot)
    meta=json.loads((snapshot/'phase1-result.json').read_text())
    expected=meta.get('project_backup_sha256')
    if not expected or not (snapshot/'project.sqlite').is_file() or base.sha(snapshot/'project.sqlite')!=expected:
        raise DependencyError('prior_project_snapshot_missing_or_changed')
    return expected


def input_identity(snapshot):
    snapshot=Path(snapshot)
    # Including ledgers themselves binds captured metadata and all dependency
    # hashes to the operation ID, not just business data.
    deps=verify(snapshot)
    meta=json.loads((snapshot/'phase1-result.json').read_text())
    return dict(project_snapshot_sha256=verify_project_snapshot(snapshot),database=meta['backup_sha256'],projection_epoch=meta['projection_epoch'],files=json.loads((snapshot/'input-files.json').read_text()),**deps)
