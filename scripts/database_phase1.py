#!/usr/bin/env python3
"""Phase 1 only: immutable private backups and read-only legacy baselines."""
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('structured-records','article-body-captures','article-review-facts','article-summaries','talent-index-proposals','article-classification-proposals','article-feedback-instructions','official-talent-registry')
KEYS = dict(articles='article_key', article_contents='article_key', talents='talent_id', article_talents='relation_key', article_classifications='article_key', article_feedback='article_key')

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()

def save(p,v):
    p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def ro(p):
    c=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c

def q(s):
    return '"'+s.replace('"','""')+'"'

def inventory():
    paths=[]
    for d in [*('content/'+d for d in TARGETS), 'config','scripts','docs/ai-rules','apps/talent-dashboard','content/ai-keyword-candidates','content/analysis','content/weekly-reports']:
        paths.extend(p for p in (ROOT/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    return [dict(path=str(p.relative_to(ROOT)),size=p.stat().st_size,sha256=sha(p)) for p in sorted(set(paths))]

def idle(c):
    found=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit() or int(p.name)==os.getpid(): continue
        try:
            args=(p/'cmdline').read_bytes().decode(errors='replace').split('\0')
            names=[Path(a).name for a in args[:2]]
            if any(n=='n8n' or any(k in n for k in ('capture_article','backfill','save_article','save_talent','generate_daily','apply_article','weekly_metrics')) for n in names):
                found.append(dict(pid=int(p.name),program=names[0]))
        except (FileNotFoundError,ProcessLookupError): pass
    executions=[dict(r) for r in c.execute("SELECT id,status FROM execution_entity WHERE status IN ('new','running','waiting')")]
    result=dict(observed_at=now(),processes=found,unfinished_executions=executions)
    if found or executions: raise RuntimeError('writers_active')
    return result

def baseline(c):
    mapping={r['name']:'data_table_user_'+r['id'] for r in c.execute('SELECT id,name FROM data_table') if r['name'] in KEYS}
    if set(mapping)!=set(KEYS): raise RuntimeError('six_tables_not_resolved')
    tables={}
    for name,physical in mapping.items():
        table,key=q(physical),q(KEYS[name])
        h=hashlib.sha256()
        for row in c.execute(f'SELECT * FROM {table} ORDER BY id'):
            h.update((json.dumps(dict(row),sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode())
        tables[name]=dict(physical_table=physical,
            columns=[dict(r) for r in c.execute(f'PRAGMA table_info({table})')],
            indexes=[dict(r) for r in c.execute(f'PRAGMA index_list({table})')],
            foreign_keys=[dict(r) for r in c.execute(f'PRAGMA foreign_key_list({table})')],
            count=c.execute(f'SELECT count(*) FROM {table}').fetchone()[0],rows_sha256=h.hexdigest(),
            null_or_empty_keys=c.execute(f"SELECT count(*) FROM {table} WHERE {key} IS NULL OR {key}=''").fetchone()[0],
            duplicate_business_keys=[dict(r) for r in c.execute(f'SELECT {key} AS business_key,count(*) AS count FROM {table} GROUP BY {key} HAVING count(*)>1')])
    conflicts={}
    for name,fields in (('articles','url'),('article_contents','original_url'),('article_talents','article_key,talent_id')):
        conflicts[name+':duplicates:'+fields]=[dict(r) for r in c.execute(f'SELECT {fields},count(*) AS count,group_concat(id) AS row_ids FROM {q(mapping[name])} GROUP BY {fields} HAVING count(*)>1')]
    for name,col,parent in (('article_contents','article_key','articles'),('article_talents','article_key','articles'),('article_classifications','article_key','articles'),('article_feedback','article_key','articles'),('article_talents','talent_id','talents')):
        conflicts[name+':missing:'+col]=[dict(r) for r in c.execute(f'SELECT s.id,s.{col} FROM {q(mapping[name])} s WHERE NOT EXISTS (SELECT 1 FROM {q(mapping[parent])} p WHERE p.{col}=s.{col})')]
    states={}
    for name,col in (('article_contents','content_status'),('talents','status'),('talents','search_enabled')):
        states[name+':'+col]=[dict(r) for r in c.execute(f'SELECT {col},count(*) AS count FROM {q(mapping[name])} GROUP BY {col}')]
    return dict(tables=tables,conflicts=conflicts,states=states)

def credentials(files):
    patterns=[rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',rb'\b(?:sk-proj-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}',rb'(?i)["\x27]?(?:api[_-]?key|access[_-]?token|authorization|password|client_secret)["\x27]?\s*[:=]\s*["\x27](?!Bearer\s*\{|\$|\{|<)[A-Za-z0-9_./+=-]{20,}["\x27]']
    findings=[]
    for entry in files:
        if not entry['path'].startswith('content/'): continue
        b=(ROOT/entry['path']).read_bytes()
        for i,pat in enumerate(patterns):
            if re.search(pat,b): findings.append(dict(path=entry['path'],rule=i))
    return findings

def outputs(folder,database,date):
    sys.path.insert(0,str(ROOT/'scripts'))
    import read_ai_inputs, weekly_metrics
    spec=importlib.util.spec_from_file_location('legacy_dashboard',ROOT/'apps/talent-dashboard/server.py')
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    os.environ['N8N_DATABASE_PATH']=str(database)
    dashboard=m.build_dashboard()
    if dashboard.get('sourceError'): raise RuntimeError('dashboard_fallback')
    save(folder/'dashboard.json',dashboard)
    save(folder/'weekly.json',weekly_metrics.build_metrics(ROOT,date,as_of=date))
    for task in read_ai_inputs.TASKS:
        save(folder/('ai-'+task+'.json'),read_ai_inputs.build_payload(ROOT,date,task,offset=0,limit=20,include_body=True))
    save(folder/'parameters.json',dict(run_date=date,as_of=date,offset=0,limit=20,max_content_chars=6000,include_body=True))

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path.home()/'.n8n/database.sqlite')
    parser.add_argument('--run-date',required=True)
    args=parser.parse_args()
    dt.date.fromisoformat(args.run_date)
    os.umask(0o077)
    folder=ROOT/'.operation-state/database-consolidation'/dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder.mkdir(parents=True,exist_ok=False)
    report=dict(phase=1,status='running',started_at=now(),source_database=str(args.database),legacy_modified=False)
    try:
        with ro(args.database) as source:
            report['idle_before']=idle(source)
            before=inventory()
            save(folder/'input-files.json',before)
            findings=credentials(before)
            save(folder/'credential-scan.json',dict(findings=findings,checked_files=len(before),scope='content files; full n8n backup is confidential'))
            if findings: raise RuntimeError('credential_information_in_inputs')
            snapshot=folder/'n8n.sqlite'
            with sqlite3.connect(snapshot) as dest: source.backup(dest,pages=1024)
            for entry in before:
                dest=folder/'files'/entry['path']
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(ROOT/entry['path'],dest)
                if sha(dest)!=entry['sha256']: raise RuntimeError('input_changed_during_copy')
            with ro(snapshot) as backup:
                if [r[0] for r in backup.execute('PRAGMA integrity_check')]!=['ok']: raise RuntimeError('backup_integrity_failed')
                saved=baseline(backup)
            save(folder/'database-baseline.json',saved)
            save(folder/'known-conflicts.json',saved['conflicts'])
            restored_path=folder/'restore-test.sqlite'
            with ro(snapshot) as backup, sqlite3.connect(restored_path) as dest: backup.backup(dest)
            with ro(restored_path) as restored:
                integrity=[r[0] for r in restored.execute('PRAGMA integrity_check')]
                if integrity!=['ok'] or baseline(restored)!=saved: raise RuntimeError('restoration_mismatch')
            report['restored_all_six_tables']=True
            report['restore_integrity']=integrity
            output_dir=folder/'representative-outputs'
            output_dir.mkdir()
            outputs(output_dir,snapshot,args.run_date)
            report['idle_after']=idle(source)
            if inventory()!=before: raise RuntimeError('inputs_changed')
            if baseline(source)!=saved: raise RuntimeError('source_database_changed')
            report.update(status='complete',input_files=len(before),hashes_recomputed=True,backup_sha256=sha(snapshot),table_counts={k:v['count'] for k,v in saved['tables'].items()})
    except Exception as exc:
        report.update(status='stopped',reason=str(exc),exception_type=type(exc).__name__)
    report['finished_at']=now()
    save(folder/'phase1-result.json',report)
    print(json.dumps(dict(report_path=str(folder/'phase1-result.json'),**report),ensure_ascii=False))
    return 0 if report['status']=='complete' else 2

if __name__=='__main__':
    sys.dont_write_bytecode=True
    raise SystemExit(main())

