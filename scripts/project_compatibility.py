"""Post-commit n8n Data Table compatibility; never used before the DB commit."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import project_database as db
import project_write_outbox as writes
import continuous_database_sync as sync
from autoarticle_apply import equal

KEYS={'articles':'article_key','talents':'talent_id','article_talents':'relation_key','article_classifications':'article_key','article_feedback':'article_key','article_contents':'article_key'}


def legacy_path():
    return Path(os.environ.get('N8N_DATABASE_PATH',str(Path.home()/'.n8n/database.sqlite')))


def current(name,keyvalue):
    if name not in KEYS:raise ValueError('Unsupported compatibility table')
    with closing(sqlite3.connect(legacy_path().resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        tables=c.execute('SELECT id FROM data_table WHERE name=?',(name,)).fetchall()
        if len(tables)!=1 or not re.fullmatch('[A-Za-z0-9_]+',tables[0][0]):raise writes.WriteStopped('legacy_table_not_unique')
        table_id=tables[0][0]
        rows=[dict(x) for x in c.execute('SELECT * FROM "data_table_user_'+table_id+'" WHERE '+KEYS[name]+'=?',(keyvalue,))]
        if len(rows)>1:raise writes.WriteStopped('legacy_key_not_unique')
        return table_id, rows[0] if rows else None


def projected(row,columns):
    return {k:row.get(k) for k in columns} if row is not None else None


def enqueue(unit,name,data):
    table_id,before=current(name,data[KEYS[name]])
    unit.delivery('data-table',name,dict(tableId=table_id,data=data,before=projected(before,data)),sync.digest(projected(before,data)))


def same(actual,wanted):
    return actual is not None and all(equal('classified_at' if k in ('fetched_at','reviewed_at') else k,actual.get(k),v) for k,v in wanted.items())


def deliver(row):
    payload=json.loads(row['payload_json'])
    if sync.digest(payload)!=row['payload_hash']:raise writes.WriteStopped('outbox_payload_hash_changed')
    name=row['target'];data=payload['data'];table_id,actual=current(name,data[KEYS[name]])
    if table_id!=payload['tableId']:raise writes.WriteStopped('compatibility_table_changed')
    if same(actual,data):return
    if projected(actual,data)!=payload['before']:raise writes.WriteStopped('legacy_target_changed')
    from sync_workflow_to_n8n import load_env_file,normalize_api_base_url,api_request
    load_env_file(db.ROOT/'.env')
    base=normalize_api_base_url(os.environ.get('N8N_API_BASE_URL') or os.environ.get('N8N_BASE_URL') or '')
    if urlsplit(base).hostname not in ('127.0.0.1','localhost'):raise ValueError('Local n8n API required')
    # Idempotent key upsert. A lost response remains pending; the next retry first
    # verifies whether the exact intended result is already present.
    api_request('POST',base,'/data-tables/'+table_id+'/rows/upsert',os.environ['N8N_API_KEY'],{'filter':{'type':'and','filters':[{'columnName':KEYS[name],'condition':'eq','value':data[KEYS[name]]}]},'data':data,'returnData':False})
    _,actual=current(name,data[KEYS[name]])
    if not same(actual,data):raise writes.WriteStopped('legacy_compatibility_comparison_failed')
