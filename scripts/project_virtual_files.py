"""DB-backed machine artifacts. Human Markdown remains a physical output."""
from contextlib import closing
import json
from pathlib import Path
import project_database as db
import project_readers


def retired(root):
    path=project_readers.path_for(root)
    if not path.exists():return False
    with closing(db.connect(path,readonly=True)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='compatibility_policy'").fetchone():return False
        return c.execute('SELECT mode FROM compatibility_policy WHERE id=1').fetchone()[0]!='automatic'


def fingerprint(root,relative):
    """None means not DB-backed; an empty string means no saved DB artifact."""
    folders=('structured-records','article-body-captures','article-review-facts','talent-index-proposals','article-classification-proposals')
    if Path(relative).suffix not in ('.json','.jsonl') or not any(relative.startswith('content/'+f+'/') for f in folders) or not retired(root):return None
    with closing(db.connect(project_readers.path_for(root),readonly=True)) as c:
        rows=c.execute("""SELECT record_position,input_hash,target_kind,target_id FROM source_records s WHERE source_path=?
            AND s.rowid=(SELECT max(v.rowid) FROM source_records v WHERE v.source_path=s.source_path AND v.record_position=s.record_position)
            ORDER BY record_position""",(relative,)).fetchall()
        return 'project-db:'+db.checksum(db.canonical([list(x) for x in rows]).encode()) if rows else ''


def document(root,relative):
    if project_readers.source(root)!='project-db':return json.loads((Path(root)/relative).read_text())
    with project_readers.reader(root) as reader:
        matches=[value for path,value in reader.documents(Path(relative).parent.name) if path==relative]
    if len(matches)!=1:raise ValueError('DB proposal is missing or ambiguous')
    return matches[0]


def runtime_state(root,name):
    relative='content/article-body-captures/'+name
    with project_readers.reader(root) as reader:
        row=reader.c.execute('SELECT raw_json FROM source_records WHERE source_path=? AND record_position=? ORDER BY rowid DESC LIMIT 1',(relative,'$')).fetchone()
        if row:return json.loads(row[0])
        # Phase 3 stores host entries separately. Reconstruct exactly those inputs.
        rows=reader.c.execute('SELECT record_position,raw_json FROM source_records WHERE source_path=? ORDER BY rowid',(relative,)).fetchall()
        result={'hosts':{},'globalNext':0}
        for row in rows:
            raw=json.loads(row['raw_json']);pos=row['record_position']
            if pos=='header':result.update(raw)
            elif pos.startswith('hosts/'):
                result['hosts'][pos[len('hosts/'):]]=raw
        return result


def collection_records(day,database):
    with closing(db.connect(database,readonly=True)) as c:
        run=c.execute('SELECT * FROM collection_runs WHERE run_date=?',(day,)).fetchall()
        if len(run)!=1:raise ValueError('Collection is missing or ambiguous')
        rows=c.execute("SELECT observations_json FROM article_occurrences WHERE collection_run_id=? ORDER BY CAST(replace(source_record,'line:','') AS INTEGER)",(run[0]['id'],)).fetchall()
        return [json.loads(run[0]['search_conditions_json'])]+[json.loads(row[0]) for row in rows]


def collection_output(day,database):
    records=collection_records(day,database);result=dict(records[0]);result['articles']=[x['article'] for x in records[1:]]
    result['articleCount']=len(result['articles'])
    return result
