"""Accept missing-body source claims without changing imported input or importer replay identity."""
import json
import project_database as db
import import_legacy_database as imp

from missing_body_acceptance import claims, missing_claim, candidates
import missing_body_acceptance as acceptance

def reconcile(c,ledger_path):
    ledger=acceptance.load(ledger_path)
    return acceptance.apply(c,ledger,db.checksum(__import__('pathlib').Path(ledger_path).read_bytes()))['updated_rows']

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
