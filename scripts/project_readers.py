"""Read-only compatibility adapters over normalized project records.

cutover_state is the only operational route selector. Archived JSON supplies
formatting and ancillary fields, never overrides normalized business values.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
import uuid
import project_database as db

FEATURES = ('comparison', 'dashboard', 'weekly', 'ai-reader', 'n8n-daily')
VERSION = 'project-readers-v1'


def path_for(root=None):
    root = Path(root or db.ROOT).resolve()
    return db.database_path() if root == db.ROOT else root / 'data/autoarticle.sqlite'


def source(root=None, feature='ai-reader', database=None):
    if feature not in FEATURES:
        raise ValueError('Unknown read feature: ' + feature)
    path = Path(database or path_for(root))
    # Standalone legacy fixtures/archives have no cutover configuration.
    if not path.exists() and root is not None and Path(root).resolve() != db.ROOT:
        return 'legacy'
    with closing(db.connect(path, readonly=True)) as c:
        row = c.execute('SELECT read_source,write_target FROM cutover_state WHERE feature=?', (feature,)).fetchone()
        if row is None or row['read_source'] not in ('legacy', 'project-db'):
            raise ValueError('Missing/invalid read route: ' + feature)
        return row['read_source']


def set_source(feature, value, database=None):
    if feature not in FEATURES or value not in ('legacy', 'project-db'):
        raise ValueError('Invalid read route')
    with closing(db.connect(database)) as c, db.transaction(c):
        row = c.execute('SELECT write_target FROM cutover_state WHERE feature=?', (feature,)).fetchone()
        if not row or row[0] != 'legacy':
            raise ValueError('Phase 6 requires legacy writes')
        c.execute("UPDATE cutover_state SET read_source=?,changed_at=?,rollback_state=CASE WHEN ?='legacy' THEN 'rolled-back' ELSE rollback_state END WHERE feature=?", (value, db.now(), value, feature))


@contextmanager
def reader(root=None, database=None):
    with closing(db.connect(database or path_for(root), readonly=True)) as c:
        c.execute('BEGIN')
        yield Reader(c)


def formatted(old, value):
    """Retain an original timestamp spelling only when its instant is equal."""
    if old in (None, '') and value in (None, ''):
        return old
    try:
        before = datetime.fromisoformat(str(old).replace('Z', '+00:00'))
        after = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        before = before.replace(tzinfo=timezone.utc) if before.tzinfo is None else before
        after = after.replace(tzinfo=timezone.utc) if after.tzinfo is None else after
        if before == after:
            return old
    except ValueError:
        pass
    return value


def assign(raw, name, value, timestamp=False):
    if name not in raw and value in (None, ''):
        return
    raw[name] = formatted(raw.get(name), value) if timestamp else (raw.get(name) if raw.get(name) in (None, '') and value in (None, '') else value)


class ReviewRecord(dict):
    """Compatibility fields plus DB provenance outside the validated JSON schema."""
    pass


class Reader:
    def __init__(self, connection):
        self.c = connection

    def has_day(self, day):
        return bool(self.c.execute('SELECT 1 FROM collection_runs WHERE run_date=?', (day,)).fetchone())

    def sources(self, kind, path):
        # Source rows are append-only; a changed position adds a new source version.
        # Select the most recently committed version, retaining all older rows in DB.
        return self.c.execute("""SELECT s.* FROM source_records s
          WHERE s.target_kind=? AND s.source_path LIKE ?
          AND s.rowid=(SELECT max(v.rowid) FROM source_records v
            WHERE v.source_path=s.source_path AND v.record_position=s.record_position)
          ORDER BY s.source_path, CAST(replace(s.record_position,'line:','') AS INTEGER), s.record_position""", (kind, path)).fetchall()

    def capture(self, row):
        raw = json.loads(row['raw_json'])
        for key, column in [('contentStatus','status'),('originalUrl','original_url'),('resolvedUrl','resolved_url'),
                            ('contentCompleteness','completeness'),('failureReason','failure_reason'),('extractionMethod','extraction_method')]:
            assign(raw, key, row[column])
        assign(raw, 'fetchedAt', row['fetched_at'], True)
        if 'retryAfter' in raw or row['retry_after']:
            assign(raw, 'retryAfter', row['retry_after'], True)
        payload = self.c.execute('SELECT p.* FROM article_content_versions v JOIN content_payloads p ON p.id=v.payload_id WHERE v.id=? AND v.article_id=?', (row['version_id'],row['article_id'])).fetchone()
        if row['version_id'] and payload is None:
            raise ValueError('Unresolved body version')
        for key, value in [('contentText', payload['text'] if payload else ''),
                           ('contentMarkdown', payload['markdown'] if payload else ''),
                           ('pageMetadata', json.loads(payload['metadata_json']) if payload else {}),
                           ('nonContentText', payload['non_content_text'] if payload else ''),
                           ('extractionScope', payload['extraction_scope'] if payload else raw.get('extractionScope', ''))]:
            if key in raw or value:
                raw[key] = value
        raw['_project'] = dict(articleId=row['article_id'], contentVersionId=row['version_id'],
                               fetchAttemptId=row['id'], bodyIntegrity=row['body_integrity'],
                               sourceStatus=row['source_status'], storedBodyHash=row['stored_body_hash'],
                               storedBodyLength=row['stored_body_length'])
        return raw

    def load_day(self, day, warnings):
        runs = self.c.execute('SELECT * FROM collection_runs WHERE run_date=?', (day,)).fetchall()
        if len(runs) != 1:
            raise ValueError(day + ': expected exactly one collection run')
        runrow = runs[0]; run = json.loads(runrow['search_conditions_json'])
        run['runDate'] = runrow['run_date']
        if 'workflowExecutionId' in run:
            if str(run['workflowExecutionId']) != runrow['workflow_execution_id']:
                run['workflowExecutionId'] = runrow['workflow_execution_id']
        rows = self.c.execute("""SELECT o.*,a.identity_state FROM article_occurrences o
          JOIN articles a ON a.id=o.article_id WHERE o.collection_run_id=?
          ORDER BY CAST(replace(o.source_record,'line:','') AS INTEGER),o.id""", (runrow['id'],)).fetchall()
        articles = []
        for row in rows:
            raw = json.loads(row['observations_json']); article = raw['article']
            for key in ('title','url','excerpt'):
                assign(article,key,row[key])
            assign(article,'publishedAt',row['published_at'],True)
            raw['runDate'] = runrow['run_date']
            raw['_project'] = dict(articleId=row['article_id'], occurrenceId=row['id'], identityState=row['identity_state'])
            articles.append(raw)
        captures = {}
        sources = self.sources('content_fetch_attempts', 'content/article-body-captures/'+day+'.jsonl')
        for s in sources:
            row = self.c.execute('SELECT * FROM content_fetch_attempts WHERE id=?', (s['target_id'],)).fetchone()
            if row is None:
                raise ValueError('Missing capture target')
            if row['original_url'] in captures:
                warnings.append(day+': duplicate body capture for '+row['original_url']+'; last saved row selected')
            captures[row['original_url']] = self.capture(row)
        if not sources:
            warnings.append(day+': body-capture file is missing; articles are not_captured, not unavailable')
        return run, articles, captures

    def reviews(self, through, warnings):
        index, known = {}, set()
        ns = uuid.UUID('520e98ee-e2d9-4c73-a2d5-535e16f6ce61')
        def ident(*parts): return str(uuid.uuid5(ns, db.canonical(parts)))
        for s in self.sources('review_records', 'content/article-review-facts/%.jsonl'):
            day = Path(s['source_path']).stem
            if day > through:
                continue
            r = self.c.execute('SELECT * FROM review_records WHERE id=?',(s['target_id'],)).fetchone()
            raw = ReviewRecord(json.loads(r['raw_json'])); known.add(raw['url'])
            raw.database_status = r['status']
            raw.database_references = dict(reviewId=r['id'], articleId=r['article_id'], contentVersionId=r['content_version_id'], status=r['status'])
            raw.update(inputHash=r['input_hash'],policyHash=r['rule_hash'],basis=r['basis'],reviewedBy=r['reviewer'])
            assign(raw,'reviewedAt',r['reviewed_at'],True)
            raw['taskStatus'] = dict(self.c.execute('SELECT task,status FROM review_task_statuses WHERE review_id=?',(r['id'],)))
            emap = {ident('evidence',r['id'],e['id']):e['id'] for e in raw['evidence']}
            evid = []
            for e in self.c.execute('SELECT * FROM review_evidence WHERE review_id=?',(r['id'],)):
                if e['id'] not in emap: raise ValueError('Unknown evidence identifier')
                evid.append(dict(id=emap[e['id']],field=e['input_field'],start=e['start_offset'],end=e['end_offset'],quote=e['quote']))
            eorder = {e['id']:i for i,e in enumerate(raw['evidence'])}
            raw['evidence'] = sorted(evid,key=lambda e:eorder[e['id']])
            facts, fmap = [], {}
            for f in self.c.execute('SELECT * FROM review_facts WHERE review_id=?',(r['id'],)):
                item=json.loads(f['raw_json']); fmap[f['id']]=item['id']; item['text']=f['fact']
                refs={emap[x[0]] for x in self.c.execute('SELECT evidence_id FROM review_fact_evidence WHERE fact_id=?',(f['id'],))}
                item['evidenceIds']=sorted(refs,key=lambda x:eorder[x]); facts.append(item)
            forder={f['id']:i for i,f in enumerate(raw['facts'])}; raw['facts']=sorted(facts,key=lambda f:forder[f['id']])
            entities=[]
            for e in self.c.execute('SELECT * FROM review_entities WHERE review_id=?',(r['id'],)):
                item=json.loads(e['raw_json']);item.update(name=e['name'],kind=e['kind'])
                item['factIds']=sorted({fmap[x[0]] for x in self.c.execute('SELECT fact_id FROM review_entity_facts WHERE entity_id=?',(e['id'],))},key=lambda x:forder[x])
                entities.append((e['id'],item))
            order={ident('entity',r['id'],i):i for i in range(len(raw['entities']))}
            raw['entities']=[e for _,e in sorted(entities,key=lambda v:order[v[0]])]
            index[(raw['url'],r['input_hash'],r['rule_hash'])]=(raw,day,s['source_path'])
        return index,known

    def legacy_key(self, article_id):
        if not hasattr(self, '_legacy_keys'):
            self._legacy_keys = {}
            for row in self.c.execute("SELECT article_id,value FROM article_identifiers WHERE kind='legacy_key' AND source IN (SELECT DISTINCT source_path FROM source_records WHERE target_kind='articles' AND source_path LIKE 'n8n:%')"):
                self._legacy_keys.setdefault(row['article_id'], set()).add(row['value'])
        values = self._legacy_keys.get(article_id, set())
        if len(values) != 1:
            raise ValueError('Legacy article identity is not unique: '+article_id)
        return next(iter(values))

    def table(self, name):
        if name not in ('articles','talents','article_talents','article_classifications','article_feedback'):
            raise ValueError('Unsupported reader table')
        result=[]
        article_sources = {}
        article_reviews = {}
        if name == 'articles':
            reviews = {}
            for rev in self.c.execute('SELECT r.id,r.article_id,r.status,r.content_version_id,t.task,t.status AS task_status FROM review_records r LEFT JOIN review_task_statuses t ON t.review_id=r.id ORDER BY r.reviewed_at,r.id,t.task'):
                entry = reviews.setdefault(rev['id'], dict(reviewId=rev['id'],articleId=rev['article_id'],status=rev['status'],contentVersionId=rev['content_version_id'],taskStatus={}))
                if rev['task']:entry['taskStatus'][rev['task']]=rev['task_status']
            for entry in reviews.values():article_reviews.setdefault(entry['articleId'],[]).append(entry)
            article_sources = {s['target_id']:s['raw_json'] for s in self.c.execute(
                "SELECT target_id,raw_json FROM source_records WHERE target_kind='articles' AND source_path LIKE 'n8n:%' ORDER BY rowid")}
        query='SELECT * FROM '+name
        if name in ('article_classifications','article_feedback'):query+=' WHERE is_current=1'
        for row in self.c.execute(query).fetchall():
            raw=json.loads(row['raw_json']) if 'raw_json' in row.keys() else None
            if name=='articles':
                original = article_sources.get(row['id'])
                if original is None:continue  # Collection-only identities have no legacy dashboard key.
                raw=json.loads(original)
            if name=='articles':
                for field in ('title','url','excerpt','source'):assign(raw,field,row[field])
                assign(raw,'published_at',row['published_at'],True)
                raw['article_key']=self.legacy_key(row['id'])
                raw['identity_state']=row['identity_state'];raw['project_article_id']=row['id']
                raw['review_states']=article_reviews.get(row['id'],[])
            elif name=='talents':
                for field in ('display_name','organization','status'):assign(raw,field,row[field])
                for field in ('search_enabled','auto_discovered'):raw[field]=bool(row[field])
                aliases=[x[0] for x in self.c.execute('SELECT alias FROM talent_aliases WHERE talent_id=? ORDER BY alias',(row['id'],))]
                previous=raw.get('aliases_json','[]')
                try: same=set(json.loads(previous) if isinstance(previous,str) else previous)==set(aliases)
                except (TypeError,ValueError):same=False
                if not same:raw['aliases_json']=json.dumps(aliases,ensure_ascii=False) if isinstance(previous,str) else aliases
                assign(raw,'last_seen_at',row['last_seen_at'],True)
            else:
                raw['article_key']=self.legacy_key(row['article_id'])
                if name=='article_talents':
                    talent=self.c.execute('SELECT raw_json FROM talents WHERE id=?',(row['talent_id'],)).fetchone()
                    raw['talent_id']=json.loads(talent[0])['talent_id'];raw['evidence_text']=row['evidence'];raw['confidence']=row['confidence'];raw['state']=row['state']
                elif name=='article_classifications':
                    for field in ('article_type','primary_category','relevance','confidence'):raw[field]=row[field]
                    assign(raw,'evidence_text',row['evidence']);assign(raw,'classified_at',row['classified_at'],True)
                    categories=[x[0] for x in self.c.execute('SELECT category FROM classification_secondary_categories WHERE classification_id=? ORDER BY category',(row['id'],))]
                    old=raw.get('secondary_categories_json','[]');prior=json.loads(old) if isinstance(old,str) else old
                    if set(prior)!=set(categories):raw['secondary_categories_json']=json.dumps(categories) if isinstance(old,str) else categories
                else:
                    raw.update(is_rejected=row['is_rejected'],reason_code=row['reason_code'],review_source=row['source'])
                    assign(raw,'reviewed_at',row['reviewed_at'],True)
            result.append(raw)
        return sorted(result,key=lambda r:r.get('id',0))

    def dashboard(self):
        result={name:self.table(name) for name in ('talents','articles','article_talents','article_classifications','article_feedback')}
        result['_article_feedback_available']=True
        return result

    def documents(self, directory, through='9999-12-31'):
        # Proposal/registry snapshots were deliberately migrated as immutable history,
        # not promoted to adopted business records. Preserve that distinction.
        grouped={}
        for s in self.sources('legacy_history_records','content/'+directory+'/%'):
            if not s['source_path'].endswith('.json'):
                continue
            day=Path(s['source_path']).stem
            if day>through:continue
            h=self.c.execute('SELECT * FROM legacy_history_records WHERE id=?',(s['target_id'],)).fetchone()
            if h is None:raise ValueError('Missing proposal history')
            raw=json.loads(h['raw_json']); doc=grouped.setdefault(s['source_path'],{})
            if s['record_position']=='header':doc.update(raw)
            elif '/' in s['record_position']:
                key,ordinal=s['record_position'].split('/',1)
                doc.setdefault(key,[]).append((int(ordinal),raw))
        for path,doc in sorted(grouped.items()):
            for k,v in list(doc.items()):
                if isinstance(v,list) and v and isinstance(v[0],tuple):doc[k]=[x[1] for x in sorted(v)]
            yield path,doc

    def classifications(self, through='9999-12-31'):
        result={}
        for path,doc in self.documents('article-classification-proposals',through):
            for row in doc.get('classifications',[]):result[row['article_url']]=(row,Path(path).stem)
        return result

    def summaries(self):
        result={}
        for row in self.c.execute('SELECT s.*,a.url FROM article_summaries s JOIN articles a ON a.id=s.article_id WHERE s.is_current=1 ORDER BY s.saved_at,s.id'):
            raw=json.loads(row['raw_json']);raw['text']=row['summary']
            result[row['url']]=raw
        return result

    def capture_metadata(self):
        result={}
        for s in self.sources('content_fetch_attempts','content/article-body-captures/backfill-state.json'):
            row=self.c.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(s['target_id'],)).fetchone()
            raw=json.loads(row['raw_json'])
            result[row['original_url']]={'resolved_url':row['resolved_url'] or '', 'source_host':raw.get('source_host') or '',
                                         'content_status':row['status'],'body_integrity':row['body_integrity'],'content_version_id':row['version_id']}
        return result


def has_day(root,day,feature='ai-reader'):
    if source(root,feature)=='legacy':
        return (Path(root)/'content/structured-records'/(day+'.jsonl')).is_file()
    with reader(root) as r:return r.has_day(day)


def service_rows(name,database=None):
    adopted=source(feature='n8n-daily',database=database)
    if adopted=='project-db':
        with reader(database=database) as r:rows=r.table(name)
    else:
        if name not in ('talents','article_feedback'):raise ValueError('Unsupported legacy read')
        path=Path(os.environ.get('N8N_DATABASE_PATH',str(Path.home()/'.n8n/database.sqlite')))
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
            row=c.execute('SELECT id FROM data_table WHERE name=?',(name,)).fetchone()
            if not row:raise ValueError('Missing legacy table')
            table='data_table_user_'+row[0]
            if not all(ch.isalnum() or ch=='_' for ch in table):raise ValueError('Invalid legacy table identifier')
            rows=[dict(r) for r in c.execute('SELECT * FROM "'+table+'"')]
    return dict(source=adopted,rows=rows)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Change one read route; writes remain legacy.')
    parser.add_argument('--feature', required=True, choices=FEATURES)
    parser.add_argument('--source', required=True, choices=('legacy','project-db'))
    args = parser.parse_args()
    set_source(args.feature,args.source)
    print(db.canonical(dict(feature=args.feature,read_source=source(feature=args.feature),write_target='legacy')))
