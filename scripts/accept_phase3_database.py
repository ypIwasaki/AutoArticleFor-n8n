"""Accept missing-body source claims without changing imported input or importer replay identity."""
import json
import project_database as db
import import_legacy_database as imp

def claims(raw):
    v=imp.content_values(raw)
    length=raw.get('contentLength',raw.get('content_length',raw.get('body_length')))
    return v,length

def missing_claim(raw):
    v,length=claims(raw)
    return not v['text'] and isinstance(length,(int,float)) and length>0 and bool(v['stored_hash']) and v['stored_hash']!=db.checksum(b'')

def candidates(c):
    # Derive the population from every preserved source record, never a fixed count.
    for row in c.execute('SELECT * FROM source_records ORDER BY id'):
        raw=json.loads(row['raw_json'])
        if isinstance(raw,dict) and missing_claim(raw):yield dict(row),raw

def reconcile(c,research):
    updates=0
    with db.transaction(c):
        for source,raw in list(candidates(c)):
            v,length=claims(raw)
            f=c.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(source['target_id'],)).fetchone()
            if not f or f['version_id'] is not None or f['body_integrity']!='held_missing_body' or f['status']!='unverified':
                raise RuntimeError('Unexpected missing-body disposition; no automatic correction')
            if source['state']!='held' or not source['reason']:raise RuntimeError('Missing source hold reason')
            if f['stored_body_length'] is None:
                c.execute('UPDATE content_fetch_attempts SET stored_body_length=? WHERE id=?',(length,f['id']));updates+=1
            elif f['stored_body_length']!=length:raise RuntimeError('Stored length differs from source')
            found=False
            for conflict in c.execute("SELECT * FROM consolidation_conflicts WHERE article_id=? AND kind='body_integrity'",(f['article_id'],)).fetchall():
                detail=json.loads(conflict['details_json'])
                if detail.get('source_record_id')!=source['id']:continue
                found=True
                detail['preserved_missing_body_claim']=dict(source_path=source['source_path'],record_position=source['record_position'],input_hash=source['input_hash'],stored_body_hash=v['stored_hash'],stored_body_length=length,source_status=v['status'],original_url=v['url'],resolved_url=v['resolved'],content_path=v['content_path'],body_field_present=any(k in raw for k in ('content_text','contentText')),computed_empty_hash=db.checksum(b''))
                detail['acceptance_research']=research
                encoded=db.canonical(detail)
                if encoded!=conflict['details_json']:
                    c.execute('UPDATE consolidation_conflicts SET details_json=? WHERE id=?',(encoded,conflict['id']));updates+=1
            if not found:raise RuntimeError('Missing conflict record')
    return updates

def verify(c):
    targets=[]
    for source,raw in candidates(c):
        v,length=claims(raw)
        f=c.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(source['target_id'],)).fetchone()
        assert f and f['body_integrity']=='held_missing_body' and f['version_id'] is None and f['status']=='unverified'
        assert f['source_status']==v['status'] and f['stored_body_hash']==v['stored_hash'] and f['stored_body_length']==length
        assert f['original_url']==v['url'] and f['resolved_url']==v['resolved'] and f['source_content_path']==v['content_path']
        assert json.loads(f['raw_json'])==raw and source['input_hash']==imp.hashrow(raw)
        assert source['state']=='held' and source['reason']
        assert c.execute('SELECT identity_state FROM articles WHERE id=?',(f['article_id'],)).fetchone()[0]=='held'
        details=[json.loads(x[0]) for x in c.execute("SELECT details_json FROM consolidation_conflicts WHERE kind='body_integrity' AND article_id=?",(f['article_id'],))]
        assert any(x.get('source_record_id')==source['id'] and x.get('acceptance_research') and x['preserved_missing_body_claim']['stored_body_length']==length for x in details)
        targets.append(dict(source_record_id=source['id'],source_path=source['source_path'],position=source['record_position'],input_hash=source['input_hash'],fetch_id=f['id'],article_id=f['article_id'],stored_body_hash=v['stored_hash'],stored_body_length=length,body_integrity=f['body_integrity'],source_status=f['source_status'],status=f['status'],version_id=f['version_id']))
    actual={x[0] for x in c.execute("SELECT id FROM content_fetch_attempts WHERE body_integrity='held_missing_body'")}
    assert actual=={x['fetch_id'] for x in targets}, 'Unexplained missing-body fetches'
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert not c.execute("SELECT 1 FROM source_records WHERE state NOT IN ('imported','held') OR (state='held' AND (reason IS NULL OR reason='' OR reason='processing'))").fetchone()
    assert all(x['read_source']==x['write_target']=='legacy' for x in c.execute('SELECT * FROM cutover_state'))
    return dict(status='passed',derived_missing_body_count=len(targets),targets=targets,no_payload_references=True,source_values_preserved=True,all_sources_disposed=True,foreign_key_violations=0,all_cutover_legacy=True)
