"""Replay immutable evidence in a hash-bound previous project snapshot."""
from contextlib import closing
from pathlib import Path
import json
import project_database as db
import identity_assessment_history as identity
import missing_body_capture_history as captures

def scopes(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='missing_body_capture_scopes'").fetchone():return {}
    result={}
    for row in c.execute('SELECT * FROM missing_body_capture_scopes'):
        members=[dict(x) for x in c.execute('SELECT path,position,source_record_id,input_hash FROM missing_body_capture_scope_members WHERE scope_id=?',(row['id'],))]
        members.sort(key=lambda x:(x['path'],int(x['position'][5:])))
        result[row['id']]=(dict(row),members)
    return result

def verify(snapshot,work):
    from sync_snapshot_dependencies import verify_project_snapshot
    verify_project_snapshot(snapshot)
    with closing(db.connect(Path(snapshot)/'project.sqlite',readonly=True)) as c:
        c.execute('PRAGMA cache_size=-262144')
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='identity_assessment_versions'").fetchone() or not c.execute('SELECT 1 FROM identity_assessment_versions LIMIT 1').fetchone():return None
        processors={x['id']:dict(x) for x in c.execute('SELECT * FROM identity_processors')}
        proof=identity.verify_database(c,processors,work,include_reconstruction=True)
        captures.verify_database(c,scopes(c))
        return proof

def restore_identity(c,snapshot,proof):
    if not proof:return 0
    added=0
    with closing(db.connect(Path(snapshot)/'project.sqlite',readonly=True)) as old:
        for entity in ('identityBlob','identityProcessor','identityProcessorFile','identityEvidence','identityEvidenceMember'):
            table=identity.ENTITIES[entity]
            for row in old.execute('SELECT * FROM '+table):added+=identity.insert(c,table,dict(row))
        for sid,row in proof['initial_rows'].items():added+=identity.insert(c,'article_identity_assessments',row)
        for old_version in old.execute('SELECT * FROM identity_assessment_versions'):
            sid=old_version['source_record_id'];source=c.execute('SELECT id,input_hash FROM source_records WHERE id=?',(sid,)).fetchone()
            if not source:raise RuntimeError('prior_identity_source_not_restored')
            decision=proof['reproduced_scopes'][old_version['evidence_set_id']][sid]
            regenerated=identity.version_row(source,old_version['evidence_set_id'],decision,proof['initial_rows'][sid],old_version['kind'])
            if regenerated!=dict(old_version):raise RuntimeError('prior_identity_version_not_reproduced')
            added+=identity.insert(c,'identity_assessment_versions',regenerated)
        for row in old.execute('SELECT * FROM identity_assessment_events'):added+=identity.insert(c,'identity_assessment_events',dict(row))
    return added
