"""Deterministic, source-bound acceptance annotations; never copy target JSON."""
import json
from pathlib import Path
import project_database as db
import import_legacy_database as imp
VERSION = 'missing-body-acceptance-v1'
LEDGER_PATH = 'docs/database-missing-body-decisions.json'

def claims(raw):
    return imp.content_values(raw), imp.stored_length(raw)

def missing_claim(raw):
    v,length=claims(raw)
    return not v['text'] and isinstance(length,(int,float)) and length>0 and bool(v['stored_hash']) and v['stored_hash']!=db.checksum(b'')

def candidates(c):
    for row in c.execute('SELECT * FROM source_records ORDER BY id'):
        raw=json.loads(row['raw_json'])
        if isinstance(raw,dict) and missing_claim(raw):yield dict(row),raw

def load(path):
    ledger=json.loads(Path(path).read_text())
    if ledger.get('format_version')!=VERSION:raise RuntimeError('acceptance_format_mismatch')
    if not isinstance(ledger.get('research'),dict) or not ledger['research']:raise RuntimeError('acceptance_research_missing')
    ids=[x['decision_id'] for x in ledger['decisions']]
    if len(ids)!=len(set(ids)):raise RuntimeError('duplicate_acceptance_decision')
    return ledger

def plan(c,ledger):
    if ledger.get('format_version')!=VERSION:raise RuntimeError('acceptance_format_mismatch')
    decisions=ledger['decisions'];sources=list(candidates(c))
    expected={'missing-body:'+s['id'] for s,raw in sources}
    if {d['decision_id'] for d in decisions}!=expected or len(decisions)!=len(expected):
        raise RuntimeError('unregistered_or_missing_acceptance_decision')
    by_id={d['decision_id']:d for d in decisions};changes=[]
    for source,raw in sources:
        d=by_id['missing-body:'+source['id']];v,length=claims(raw)
        required=dict(subject='missing_body',body_integrity='held_missing_body',status='unverified',preserve_source_status=True,create_payload=False,create_version=False,source_path=source['source_path'],record_position=source['record_position'],input_hash=source['input_hash'],stored_body_hash=v['stored_hash'],stored_body_length=length,source_status=v['status'],conflict_kind='body_integrity')
        if any(d.get(k)!=value for k,value in required.items()) or not d.get('approval_reference'):
            raise RuntimeError('acceptance_source_claim_mismatch')
        f=c.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(source['target_id'],)).fetchone()
        if not f or f['version_id'] is not None or f['body_integrity']!='held_missing_body' or f['status']!='unverified':raise RuntimeError('unexpected_missing_body_disposition')
        if source['input_hash']!=imp.hashrow(raw) or source['state']!='held' or not source['reason']:raise RuntimeError('acceptance_source_identity_mismatch')
        values={'source_status':v['status'],'stored_body_hash':v['stored_hash'],'stored_body_length':length,'original_url':v['url'],'resolved_url':v['resolved'],'source_content_path':v['content_path']}
        if any(f[k]!=value for k,value in values.items()) or json.loads(f['raw_json'])!=raw:raise RuntimeError('acceptance_fetch_claim_mismatch')
        if c.execute('SELECT identity_state FROM articles WHERE id=?',(f['article_id'],)).fetchone()[0]!='held':raise RuntimeError('acceptance_article_not_held')
        found=[]
        for conflict in c.execute("SELECT * FROM consolidation_conflicts WHERE article_id=? AND kind='body_integrity'",(f['article_id'],)):
            detail=json.loads(conflict['details_json'])
            if detail.get('source_record_id')!=source['id']:continue
            if detail.get('source_path')!=source['source_path'] or str(detail.get('source_position'))!=str(source['record_position']) or detail.get('stored_body_hash')!=v['stored_hash'] or detail.get('source_status')!=v['status']:raise RuntimeError('acceptance_base_conflict_mismatch')
            annotations=dict(acceptance_research=ledger['research'],preserved_missing_body_claim=dict(source_path=source['source_path'],record_position=source['record_position'],input_hash=source['input_hash'],stored_body_hash=v['stored_hash'],stored_body_length=length,source_status=v['status'],original_url=v['url'],resolved_url=v['resolved'],content_path=v['content_path'],body_field_present=any(k in raw for k in ('content_text','contentText')),computed_empty_hash=db.checksum(b'')))
            for key,value in annotations.items():
                if key in detail and detail[key]!=value:raise RuntimeError('existing_acceptance_annotation_mismatch')
                detail[key]=value
            found.append((conflict['id'],db.canonical(detail),conflict['details_json']))
        if len(found)!=1:raise RuntimeError('acceptance_conflict_not_unique')
        changes.extend(found)
    return changes

def apply(c,ledger,ledger_hash):
    changes=plan(c,ledger)  # Validate every decision before any mutation.
    c.execute('SAVEPOINT missing_body_acceptance')
    try:
        updates=0
        for identity,after,before in changes:
            if after!=before:
                c.execute('UPDATE consolidation_conflicts SET details_json=? WHERE id=?',(after,identity));updates+=1
        c.execute('RELEASE missing_body_acceptance')
    except Exception:
        c.execute('ROLLBACK TO missing_body_acceptance');c.execute('RELEASE missing_body_acceptance');raise
    return dict(version=VERSION,ledger_sha256=ledger_hash,matched_decisions=len(changes),updated_rows=updates)
