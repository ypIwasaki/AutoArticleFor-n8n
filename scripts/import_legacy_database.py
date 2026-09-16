"""Conservative, provenance-preserving one-shot legacy import. Never writes legacy data."""
import argparse
import collections
from contextlib import closing
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
import project_database as db
import database_phase1 as baseline
import article_review_facts as review_rules
from article_artifact_formats import parsed_summaries

VERSION='phase5-capture-history-import-v5'
NS=uuid.UUID('520e98ee-e2d9-4c73-a2d5-535e16f6ce61')

def ident(*parts): return str(uuid.uuid5(NS,db.canonical(parts)))
def hashrow(value): return db.checksum(db.canonical(value).encode())
def utc(value,default=None):
    if value in (None,''): return default
    if isinstance(value,(int,float)):
        return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')
    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)  # n8n SQLite datetime convention
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
def array(value):
    value=json.loads(value) if isinstance(value,str) else value
    if value is None:return []
    if not isinstance(value,list):raise ValueError('Expected saved JSON array')
    return value

def stored_length(raw):
    values={k:raw[k] for k in ('contentLength','content_length','body_length') if k in raw}
    if not values:return None
    first=next(iter(values.values()))
    if any(type(v)!=type(first) or v!=first for v in values.values()):
        raise RuntimeError('Conflicting saved body length aliases: '+','.join(values))
    return first

def content_values(raw):
    def val(a,b,default=''):return raw.get(a,raw.get(b,default))
    return dict(key=val('articleKey','article_key'),url=val('originalUrl','original_url'),
        text=val('contentText','content_text') or '',markdown=val('contentMarkdown','content_markdown') or '',
        metadata=val('pageMetadata','page_metadata',{}) or {},non_content=val('nonContentText','non_content_text') or '',
        scope=val('extractionScope','extraction_scope') or '',status=val('contentStatus','content_status',raw.get('status')),
        stored_hash=val('contentHash','content_hash'),fetched=val('fetchedAt','fetched_at',raw.get('processed_at')),
        resolved=val('resolvedUrl','resolved_url'),method=val('extractionMethod','extraction_method'),
        reason=val('failureReason','failure_reason',raw.get('reason')),completeness=val('contentCompleteness','content_completeness'),
        retry=raw.get('retry_after'),stored_length=stored_length(raw),content_path=raw.get('content_path'))

def body_integrity(raw):
    v=content_values(raw);computed=db.checksum(v['text'].encode())
    if not v['text'] and ((v['stored_hash'] and v['stored_hash']!=computed) or (v['stored_length'] or 0)>0):
        return 'held_missing_body'
    if v['stored_hash'] and v['stored_hash']!=computed:return 'held_hash_mismatch'
    return 'consistent' if v['text'] else 'no_body'

def assess_content(row,candidates,cache_entries):
    """Multiple candidates always held. A single URL needs independent title/date/body support."""
    checks={'candidate_count':len(candidates),'title_match':False,'published_at_match':False,'nonempty_body_match':False,'cache_source_matches':False}
    if len(candidates)!=1:return False,'multiple_candidates' if candidates else 'no_candidate',checks
    a=candidates[0]
    for entry in cache_entries:
        v=content_values(entry)
        if v['key']!=row['article_key'] or v['url']!=row['original_url']:continue
        checks['cache_source_matches']=True
        title=entry.get('title')
        pub=entry.get('published_at')
        title_ok=bool(title and a.get('title') and title==a['title'])
        try: pub_ok=bool(pub and a.get('published_at') and utc(pub)==utc(a['published_at']))
        except (ValueError,TypeError):pub_ok=False
        body_ok=bool(v['text'] and row.get('content_text') and v['text']==row['content_text'] and
                     row.get('content_hash')==db.checksum(v['text'].encode()) and v['stored_hash']==row['content_hash'])
        checks['title_match']|=title_ok;checks['published_at_match']|=pub_ok;checks['nonempty_body_match']|=body_ok
        if title_ok and pub_ok and body_ok:
            checks['matched_cache_entry_hash']=hashrow(entry)
            return True,'title_publication_and_body_corroborated',checks
    return False,'insufficient_or_inconsistent_identity_evidence',checks

class Importer:
    def __init__(self,c,snapshot,run_id,stamp,files,identity_context=None,missing_body_context=None):
        self.identity_context=identity_context
        self.missing_body_context=missing_body_context
        self.c=c;self.snapshot=snapshot;self.root=snapshot/'files';self.run_id=run_id;self.stamp=stamp;self.files=files
        self.tables={};self.old_articles={};self.by_url=collections.defaultdict(list);self.content_map={};self.content_urls={}
        self.daily=[];self.day_inputs=collections.defaultdict(list);self.capture_by_day={};self.review_inputs=collections.defaultdict(list)
        self.cache_by_key=collections.defaultdict(list);self.summary_versions=collections.Counter();self.class_counts=collections.Counter()
        self.body_rows={};self.cache={};self.stats=collections.Counter()
    def put(self,table,**row):
        self.c.execute('INSERT INTO '+table+'('+','.join('"'+k+'"' for k in row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    def source(self,path,pos,raw):
        h=hashrow(raw);sid=ident('source',path,str(pos),h)
        self.put('source_records',id=sid,migration_run_id=self.run_id,source_path=path,record_position=str(pos),input_hash=h,importer_version=VERSION,target_kind=None,target_id=None,state='held',reason='processing',raw_json=db.canonical(raw))
        return sid
    def finish(self,sid,table,target,reason=None):
        self.c.execute('UPDATE source_records SET target_kind=?,target_id=?,state=?,reason=? WHERE id=?',(table,target,'held' if reason else 'imported',reason,sid))
    def conflict(self,aid,kind,reason,details):
        cid=ident('conflict',kind,aid,details)
        if not self.c.execute('SELECT 1 FROM consolidation_conflicts WHERE id=?',(cid,)).fetchone():
            self.put('consolidation_conflicts',id=cid,kind=kind,article_id=aid,reason=reason,status='unresolved',details_json=db.canonical(details),created_at=self.stamp)
    def article(self,aid,raw,held=False):
        if not self.c.execute('SELECT 1 FROM articles WHERE id=?',(aid,)).fetchone():
            self.put('articles',id=aid,title=raw.get('title') or '',url=raw.get('url',raw.get('original_url','')) or '',
                excerpt=raw.get('excerpt') or '',source=raw.get('source') or '',published_at=utc(raw.get('published_at',raw.get('publishedAt'))),
                created_at=utc(raw.get('createdAt'),self.stamp),updated_at=utc(raw.get('updatedAt'),self.stamp),identity_state='held' if held else 'identified')
        return aid
    def identifier(self,aid,source,kind,value,held=False):
        if value is None or str(value)=='':return
        value=str(value);iid=ident('identifier',aid,source,kind,value)
        if not self.c.execute('SELECT 1 FROM article_identifiers WHERE id=?',(iid,)).fetchone():
            self.put('article_identifiers',id=iid,article_id=aid,source=source,kind=kind,value=value,match_state='held' if held else 'exact')
    def provenance(self,sid,aid,path,pos,raw):
        v=content_values(raw)
        self.put('article_source_provenance',source_record_id=sid,article_id=aid,source_table=path if path.startswith('n8n:') else None,
            source_row_id=str(pos),old_article_key=v['key'] or raw.get('article_key'),original_url=v['url'] or raw.get('url'),
            input_hash=hashrow(raw),stored_content_hash=v['stored_hash'],fetch_status=v['status'])
    def held(self,path,pos,raw,reason,candidates=()):
        aid=ident('held-article',path,str(pos))
        self.article(aid,raw,True)
        self.conflict(aid,'identity',reason,{'source_path':path,'source_position':str(pos),'old_key':raw.get('article_key',raw.get('articleKey')),'candidates':list(candidates)})
        return aid
    def candidate_refs(self,candidates):
        return [dict(source_table=self.tables['articles'],source_row_id=a['id'],old_article_key=a['article_key'],article_id=ident('n8n-article',a['article_key']),input_hash=hashrow(a)) for a in candidates]
    def resolve_observation(self,raw,path,pos):
        key=raw.get('article_key',raw.get('articleKey'))
        if key in self.old_articles:
            return self.article(ident('n8n-article',key),self.old_articles[key]),None
        url=raw.get('url',raw.get('originalUrl',raw.get('original_url','')))
        candidates=self.by_url.get(url,[])
        # Daily observations are linked only when title AND publication time agree.
        if len(candidates)==1 and raw.get('title') and raw.get('publishedAt',raw.get('published_at')):
            a=candidates[0]
            try:match=raw['title']==a['title'] and utc(raw.get('publishedAt',raw.get('published_at')))==utc(a['published_at'])
            except (ValueError,TypeError):match=False
            if match:return self.article(ident('n8n-article',a['article_key']),a),None
        reason='observation_identity_not_proven'
        return self.held(path,pos,dict(raw,url=url),reason,self.candidate_refs(candidates)),reason
    def history(self,path,pos,raw,kind,reason=None,aid=None,tid=None):
        sid=self.source(path,pos,raw);hid=ident('history',sid)
        self.put('legacy_history_records',id=hid,source_record_id=sid,kind=kind,article_id=aid,talent_id=tid,raw_json=db.canonical(raw))
        self.finish(sid,'legacy_history_records',hid,reason)
        return sid
    def jsonl(self,path):
        for i,line in enumerate((self.root/path).read_text(encoding='utf-8-sig').splitlines(),1):
            if line.strip():yield 'line:'+str(i),json.loads(line)
    def paths(self,directory,suffix):
        return sorted(x['path'] for x in self.files if x['path'].startswith('content/'+directory+'/') and x['path'].endswith(suffix))
    def preload(self):
        with closing(baseline.ro(self.snapshot/'n8n.sqlite')) as source:
            self.tables={r['name']:'data_table_user_'+r['id'] for r in source.execute('SELECT id,name FROM data_table') if r['name'] in baseline.KEYS}
            self.dbrows={name:[dict(r) for r in source.execute('SELECT * FROM '+baseline.q(table)+' ORDER BY id')] for name,table in self.tables.items()}
        for a in self.dbrows['articles']:
            if a['article_key'] in self.old_articles:raise RuntimeError('Duplicate article business key')
            self.old_articles[a['article_key']]=a;self.by_url[a['url']].append(a)
        path=self.root/'content/article-body-captures/backfill-state.json'
        self.cache=json.loads(path.read_text()) if path.exists() else {}
        for entry in self.cache.get('entries',{}).values():
            self.cache_by_key[entry.get('article_key')].append(entry)
        for path in self.paths('article-body-captures','.jsonl'):
            self.capture_by_day[Path(path).stem]={o.get('originalUrl'):o for _,o in self.jsonl(path)}
    def archive_files(self):
        for item in self.files:
            raw=(self.root/item['path']).read_bytes()
            if len(raw)!=item['size'] or db.checksum(raw)!=item['sha256']:raise RuntimeError('Input file hash mismatch')
            self.put('migration_source_files',path=item['path'],migration_run_id=self.run_id,sha256=item['sha256'],size=len(raw),content=raw,state='imported',reason=None)
    def structured(self):
        for path in self.paths('structured-records','.jsonl'):
            rows=list(self.jsonl(path));runs=[(p,o) for p,o in rows if o.get('recordType')=='run']
            if len(runs)!=1:raise RuntimeError('Invalid collection run count')
            pos,raw=runs[0];sid=self.source(path,pos,raw);rid=ident('collection',path,pos)
            articles=[(p,o) for p,o in rows if o.get('recordType')=='article']
            declared=raw.get('capturedArticleCount',raw.get('articleCount'))
            if declared is not None and declared!=len(articles):raise RuntimeError('Declared collection count mismatch: '+path)
            self.put('collection_runs',id=rid,source=path,source_record=pos,run_date=raw['runDate'],workflow_execution_id=str(raw.get('workflowExecutionId','')) or None,
                search_conditions_json=db.canonical(raw),observed_at=utc(raw.get('generatedAt'),self.stamp))
            self.finish(sid,'collection_runs',rid)
            for pos,o in articles:
                sid=self.source(path,pos,o);a=o['article'];aid,reason=self.resolve_observation(a,path,pos);oid=ident('occurrence',path,pos)
                self.put('article_occurrences',id=oid,collection_run_id=rid,article_id=aid,source_record=pos,title=a.get('title') or '',excerpt=a.get('excerpt') or '',url=a['url'],published_at=utc(a.get('publishedAt')),observations_json=db.canonical(o))
                self.provenance(sid,aid,path,pos,a);self.finish(sid,'article_occurrences',oid,reason)
                day=Path(path).stem;cap=self.capture_by_day.get(day,{}).get(a['url'])
                self.day_inputs[(day,a['url'])].append((aid,a,cap))
                self.review_inputs[(a['url'],review_rules.input_hash(a,cap))].append((day,aid,a,cap))
            for pos,o in rows:
                if o.get('recordType') not in ('run','article'):self.history(path,pos,o,'unknown-structured-record','unsupported_record_type')
    def n8n(self):
        for a in self.dbrows['articles']:
            path='n8n:'+self.tables['articles'];pos=str(a['id']);sid=self.source(path,pos,a);aid=self.article(ident('n8n-article',a['article_key']),a)
            self.identifier(aid,path,'legacy_key',a['article_key']);self.identifier(aid,path,'original_url',a['url']);self.provenance(sid,aid,path,pos,a);self.finish(sid,'articles',aid)
        for url,rows in self.by_url.items():
            if len(rows)>1:self.conflict(None,'duplicate_article_url','Different legacy keys remain separate',{'url':url,'candidates':self.candidate_refs(rows)})
        self.talents={}
        for t in self.dbrows['talents']:
            path='n8n:'+self.tables['talents'];sid=self.source(path,t['id'],t);tid=ident('talent',t['talent_id']);self.talents[t['talent_id']]=tid
            self.put('talents',id=tid,display_name=t['display_name'],organization=t.get('organization') or '',status=t['status'],search_enabled=t['search_enabled'],auto_discovered=t['auto_discovered'],last_seen_at=utc(t.get('last_seen_at')),raw_json=db.canonical(t))
            for alias in sorted(set(array(t.get('aliases_json')))):self.put('talent_aliases',talent_id=tid,alias=alias)
            self.finish(sid,'talents',tid)
        pairs=collections.defaultdict(list)
        for r in self.dbrows['article_talents']:pairs[(r['article_key'],r['talent_id'])].append(r)
        for r in self.dbrows['article_talents']:
            path='n8n:'+self.tables['article_talents'];sid=self.source(path,r['id'],r);aid=ident('n8n-article',r['article_key']);tid=self.talents[r['talent_id']];iid=ident('relationship',r['relation_key'])
            duplicates=pairs[(r['article_key'],r['talent_id'])];reason='duplicate_relationship_pair_no_canonical_choice' if len(duplicates)>1 else None
            self.put('article_talents',id=iid,article_id=aid,talent_id=tid,review_id=None,evidence=r.get('evidence_text') or '',confidence=r['confidence'],state='held' if reason else 'current',raw_json=db.canonical(r))
            if reason:self.conflict(aid,'relationship_duplicates',reason,{'rows':[x['id'] for x in duplicates],'old_relation_keys':[x['relation_key'] for x in duplicates]})
            self.finish(sid,'article_talents',iid,reason)
        for name in ('article_classifications','article_feedback'):
            for r in self.dbrows[name]:
                sid=self.source('n8n:'+self.tables[name],r['id'],r);aid=ident('n8n-article',r['article_key']);iid=ident(name,str(r['id']))
                if name=='article_classifications':
                    self.put(name,id=iid,article_id=aid,review_id=None,article_type=r['article_type'],primary_category=r['primary_category'],relevance=r['relevance'],confidence=r['confidence'],evidence=r.get('evidence_text') or '',classified_at=utc(r['classified_at']),version=1,is_current=1,raw_json=db.canonical(r))
                    for category in sorted(set(array(r.get('secondary_categories_json')))):self.put('classification_secondary_categories',classification_id=iid,category=category)
                else:self.put(name,id=iid,article_id=aid,is_rejected=r['is_rejected'],reason_code=r.get('reason_code') or '',reviewed_at=utc(r['reviewed_at']),source=r.get('review_source') or 'legacy-n8n',version=1,is_current=1,raw_json=db.canonical(r))
                self.finish(sid,name,iid)
    def payload(self,aid,raw):
        v=content_values(raw)
        if body_integrity(raw).startswith('held_'):return None
        if not (v['text'] or v['markdown'] or v['metadata']):return None
        fields=dict(text=v['text'],markdown=v['markdown'],metadata_json=db.canonical(v['metadata']),non_content_text=v['non_content'],extraction_scope=v['scope'])
        fields['raw_json']=db.canonical(fields);h=hashrow(fields);pid=ident('payload',h)
        if not self.c.execute('SELECT 1 FROM content_payloads WHERE id=?',(pid,)).fetchone():self.put('content_payloads',id=pid,payload_hash=h,text_hash=db.checksum(v['text'].encode()),**fields)
        vid=ident('content-version',aid,pid);observed=utc(v['fetched'],self.stamp)
        if not self.c.execute('SELECT 1 FROM article_content_versions WHERE id=?',(vid,)).fetchone():self.put('article_content_versions',id=vid,article_id=aid,payload_id=pid,first_observed_at=observed,last_observed_at=observed)
        else:self.c.execute('UPDATE article_content_versions SET first_observed_at=min(first_observed_at,?),last_observed_at=max(last_observed_at,?) WHERE id=?',(observed,observed,vid))
        return vid
    def fetch(self,path,pos,raw,aid,reason=None,sid=None):
        sid=sid or self.source(path,pos,raw);v=content_values(raw);integrity=body_integrity(raw);vid=self.payload(aid,raw);iid=ident('fetch',sid)
        if integrity.startswith('held_'):
            reason=integrity
            references=[]
            for capture_path in self.paths('article-body-captures','.jsonl'):
                for capture_pos,entry in self.jsonl(capture_path):
                    cv=content_values(entry)
                    if cv['key']==v['key'] or cv['url']==v['url']:
                        references.append(dict(path=capture_path,position=capture_pos,input_hash=hashrow(entry),computed_body_hash=db.checksum(cv['text'].encode()),body_length=len(cv['text']),source_status=cv['status'],matches_saved_hash=db.checksum(cv['text'].encode())==v['stored_hash']))
            # Preserve existing conflict IDs/history; acceptance records all length aliases in preserved_missing_body_claim.
            details=dict(source_record_id=sid,source_path=path,source_position=str(pos),body_empty=not bool(v['text']),stored_body_hash=v['stored_hash'],stored_body_length=raw.get('contentLength',raw.get('content_length')),computed_body_hash=db.checksum(v['text'].encode()),source_status=v['status'],content_path=v['content_path'],capture_references=references,recovery_evidence='No matching body located; no substitution performed',git_investigation=dict(result='No body matching stored hashes found in current files or Git history for the 11 legacy rows',reported_by='user',policy_reference='docs/database-consolidation-progress.md#phase3-missing-body-policy'))
            if self.missing_body_context:details=self.missing_body_context.initial_details(sid,aid,details)
            self.conflict(aid,'body_integrity',integrity,details)

        if v['status'] not in ('verified','partial','unavailable','unverified','metadata_only','pending','failed'):
            self.finish(sid,None,None,'unknown_fetch_status');self.conflict(aid,'fetch_status','Unknown saved fetch status',{'source_record_id':sid,'status':v['status']});return
        self.put('content_fetch_attempts',id=iid,article_id=aid,version_id=vid,fetched_at=utc(v['fetched'],self.stamp),status='unverified' if integrity.startswith('held_') else v['status'],body_integrity=integrity,source_status=v['status'],stored_body_hash=v['stored_hash'],stored_body_length=v['stored_length'],source_content_path=v['content_path'],computed_body_hash=db.checksum(v['text'].encode()),completeness=v['completeness'],original_url=v['url'] or '',resolved_url=v['resolved'],failure_reason=v['reason'],extraction_method=v['method'],retry_after=utc(v['retry']),raw_json=db.canonical(raw))
        self.identifier(aid,path,'legacy_key',v['key'],bool(reason))
        self.identifier(aid,path,'original_url',v['url'],bool(reason));self.identifier(aid,path,'resolved_url',v['resolved'],bool(reason))
        self.provenance(sid,aid,path,pos,raw);self.finish(sid,'content_fetch_attempts',iid,reason)
    def identity_decision(self,r):
        path='n8n:'+self.tables['article_contents'];key=r['article_key'];candidates=self.by_url.get(r['original_url'],[]);checks={}
        if body_integrity(r).startswith('held_'):
            assessment=body_integrity(r);strategy='held'
            checks={'legacy_key_match':key in self.old_articles,'candidate_count':len(candidates)}
            aid=ident('held-article',path,str(r['id']))
        elif key in self.old_articles:
            aid=ident('n8n-article',key);strategy='legacy_key';assessment='exact_legacy_key'
        else:
            accepted,assessment,checks=assess_content(r,candidates,self.cache_by_key.get(key,[]))
            if accepted:aid=ident('n8n-article',candidates[0]['article_key']);strategy='corroborated'
            else:aid=ident('held-article',path,str(r['id']));strategy='held'
        return dict(source_record_id=ident('source',path,str(r['id']),hashrow(r)),article_id=aid,strategy=strategy,reason=assessment,candidates_json=db.canonical(self.candidate_refs(candidates)),checks_json=db.canonical(checks))
    def contents(self):
        path='n8n:'+self.tables['article_contents']
        for r in self.dbrows['article_contents']:
            sid=self.source(path,r['id'],r);current=self.identity_decision(r)
            applied=self.identity_context.choose(sid,r,current) if self.identity_context else current
            aid=applied['article_id'];reason=applied['reason'] if applied['strategy']=='held' else None
            if reason:
                if self.held(path,r['id'],r,reason,json.loads(applied['candidates_json']))!=aid:raise RuntimeError('historical_held_mapping_changed')
            self.content_map[r['article_key']]=aid;self.content_urls[r['article_key']]=r['original_url'];self.body_rows[r['article_key']]=r
            self.put('article_identity_assessments',**applied)
            self.fetch(path,r['id'],r,aid,reason,sid)
        for path in self.paths('article-body-captures','.jsonl'):
            for pos,r in self.jsonl(path):self.file_fetch(path,pos,r)
        for path in self.paths('article-body-captures','.json'):
            obj=json.loads((self.root/path).read_text())
            if Path(path).name=='backfill-state.json':
                self.history(path,'header',{k:v for k,v in obj.items() if k not in ('entries','resolvedUrls')},'cache-header')
                for key,r in obj.get('entries',{}).items():self.file_fetch(path,'entries/'+key,r)
                for key,v in obj.get('resolvedUrls',{}).items():
                    sid=self.source(path,'resolvedUrls/'+key,{'originalUrl':key,'value':v});iid=ident('runtime',sid)
                    self.put('content_runtime_state',id=iid,source_record_id=sid,state_key=key,state_json=db.canonical(v));self.finish(sid,'content_runtime_state',iid)
            else:
                sid=self.source(path,'$',obj);iid=ident('runtime',sid)
                self.put('content_runtime_state',id=iid,source_record_id=sid,state_key=Path(path).name,state_json=db.canonical(obj));self.finish(sid,'content_runtime_state',iid)
    def file_fetch(self,path,pos,r):
        v=content_values(r);key=v['key'];reason=None
        if key in self.content_map and self.content_urls[key]==v['url']:
            aid=self.content_map[key]
            if self.c.execute('SELECT identity_state FROM articles WHERE id=?',(aid,)).fetchone()[0]=='held':reason='preserved_held_content_identity'
        elif key in self.old_articles and self.old_articles[key]['url']==v['url']:aid=ident('n8n-article',key)
        else:reason='file_content_identity_not_proven';aid=self.held(path,pos,dict(r,url=v['url']),reason,self.candidate_refs(self.by_url.get(v['url'],[])))
        self.fetch(path,pos,r,aid,reason)
    def reviews(self):
        for path in self.paths('article-review-facts','.jsonl'):
            for pos,r in self.jsonl(path):
                sid=self.source(path,pos,r);inputs=self.review_inputs.get((r.get('url'),r.get('inputHash')),[])
                day=Path(path).stem;dated=[x for x in inputs if x[0]==day];inputs=dated or inputs
                matches={x[1]:x for x in inputs};reason=None
                if len(matches)==1:
                    _,aid,a,cap=next(iter(matches.values()))
                    # Current rules validate structure/quotes; the saved rule hash stays unchanged.
                    review_rules.validate_record(r,a,cap,r['policyHash'])
                    if r['policyHash']!=review_rules.policy_hash(self.root):reason='historical_review_policy_not_current'
                else:
                    reason='review_input_version_or_identity_not_uniquely_reconstructed'
                    aid=self.held(path,pos,{'url':r.get('url','')},reason)
                    a=cap=None
                rid=ident('review',sid)
                if not r.get('reviewedAt'):
                    self.finish(sid,None,None,'missing_review_timestamp');continue
                self.put('review_records',id=rid,article_id=aid,content_version_id=self.payload(aid,cap) if cap else None,input_hash=r['inputHash'],rule_hash=r['policyHash'],basis=r['basis'],reviewer=r['reviewedBy'],reviewed_at=utc(r['reviewedAt']),status='needs_review' if reason else 'current',raw_json=db.canonical(r))
                for task,status in r['taskStatus'].items():self.put('review_task_statuses',review_id=rid,task=task,status=status)
                if a is not None:self.put('review_input_snapshots',review_id=rid,input_hash=r['inputHash'],article_json=db.canonical(a),capture_json=db.canonical(cap))
                evid={};facts={}
                for e in r['evidence']:
                    eid=ident('evidence',rid,e['id']);evid[e['id']]=eid
                    self.put('review_evidence',id=eid,review_id=rid,input_field=e['field'],start_offset=e['start'],end_offset=e['end'],quote=e['quote'])
                for f in r['facts']:
                    fid=ident('fact',rid,f['id']);facts[f['id']]=fid
                    self.put('review_facts',id=fid,review_id=rid,fact=f['text'],raw_json=db.canonical(f))
                    for ref in sorted(set(f['evidenceIds'])):self.put('review_fact_evidence',fact_id=fid,evidence_id=evid[ref])
                for i,e in enumerate(r['entities']):
                    eid=ident('entity',rid,i);self.put('review_entities',id=eid,review_id=rid,name=e['name'],kind=e['kind'],raw_json=db.canonical(e))
                    for ref in sorted(set(e['factIds'])):self.put('review_entity_facts',entity_id=eid,fact_id=facts[ref])
                if reason:self.conflict(aid,'review_source',reason,{'source_record_id':sid,'input_hash':r['inputHash']})
                self.finish(sid,'review_records',rid,reason)
    def summaries(self):
        for path in self.paths('article-summaries','.md'):
            text=(self.root/path).read_text(encoding='utf-8-sig');day=Path(path).stem
            try:datetime.fromisoformat(day)
            except ValueError:continue
            for i,(links,summary) in enumerate(parsed_summaries(text,day)):
                for j,(title,url) in enumerate(links):
                    pos='summary:'+str(i)+':link:'+str(j);raw=dict(summary,links=links,url=url)
                    sid=self.source(path,pos,raw);inputs={x[0]:x for x in self.day_inputs.get((day,url),[]) if x[1].get('title')==title}
                    reason=None
                    if len(inputs)==1:aid=next(iter(inputs))
                    else:reason='summary_article_identity_not_unique';aid=self.held(path,pos,{'url':url,'title':title},reason)
                    self.summary_versions[aid]+=1;iid=ident('summary',sid)
                    saved=datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=9))).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
                    self.put('article_summaries',id=iid,article_id=aid,review_id=None,summary=summary['text'],saved_at=saved,version=self.summary_versions[aid],is_current=0,source=path,raw_json=db.canonical(raw))
                    self.finish(sid,'article_summaries',iid,reason)
        for row in self.c.execute('SELECT article_id,max(saved_at) AS latest FROM article_summaries GROUP BY article_id').fetchall():
            ids=[r[0] for r in self.c.execute('SELECT id FROM article_summaries WHERE article_id=? AND saved_at=?',(row['article_id'],row['latest']))]
            if len(ids)==1:self.c.execute('UPDATE article_summaries SET is_current=1 WHERE id=?',(ids[0],))
            else:self.conflict(row['article_id'],'summary_current','Multiple summaries on latest saved day; no winner chosen',{'summary_ids':ids})
    def histories(self):
        groups={'talent-index-proposals':('articles','talents','articleTalents'),'article-classification-proposals':('classifications',),'article-feedback-instructions':('feedback',),'official-talent-registry':('groups','talents')}
        for directory,keys in groups.items():
            for path in self.paths(directory,'.json'):
                obj=json.loads((self.root/path).read_text())
                self.history(path,'header',{k:v for k,v in obj.items() if k not in keys},directory+'-header')
                for key in keys:
                    for i,raw in enumerate(obj.get(key,[])):
                        self.history(path,key+'/'+str(i),raw,directory+'/'+key)
        # Every otherwise unparsed file is retained byte-for-byte with an explicit disposition.
        covered={r[0] for r in self.c.execute('SELECT DISTINCT source_path FROM source_records')}
        for item in self.files:
            path=item['path']
            if path not in covered:
                reason='supporting_document_preserved_without_business_projection'
                self.history(path,'file',{'sha256':item['sha256'],'size':item['size']},'supporting_document',reason)
                self.c.execute("UPDATE migration_source_files SET state='held',reason=? WHERE path=?",(reason,path))
    def verify(self):
        if self.c.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Import foreign key violation')
        if self.c.execute("SELECT count(*) FROM source_records WHERE reason='processing'").fetchone()[0]:raise RuntimeError('Undisposed source records')
        if self.c.execute('SELECT count(*) FROM migration_source_files').fetchone()[0]!=len(self.files):raise RuntimeError('File coverage mismatch')
        for row in self.c.execute('SELECT path,sha256,size,content FROM migration_source_files'):
            if len(row['content'])!=row['size'] or db.checksum(row['content'])!=row['sha256']:raise RuntimeError('Stored file hash mismatch')
        for name,rows in self.dbrows.items():
            records={(r['record_position'],r['input_hash']):r for r in self.c.execute('SELECT record_position,input_hash,raw_json FROM source_records WHERE source_path=?',('n8n:'+self.tables[name],))}
            if {k[0] for k in records}!={str(row['id']) for row in rows}:raise RuntimeError('n8n coverage mismatch')
            for row in rows:
                saved=records.get((str(row['id']),hashrow(row)))
                if saved is None:raise RuntimeError('n8n current source version missing')
                if saved['input_hash']!=hashrow(row) or json.loads(saved['raw_json'])!=row:raise RuntimeError('n8n source preservation mismatch')
        # Verify each JSONL record independently, not just per-file or DB counts.
        for item in self.files:
            path=item['path']
            if path.endswith('.jsonl'):
                expected={pos:raw for pos,raw in self.jsonl(path)}
                stored={(row['record_position'],row['input_hash']):row for row in self.c.execute('SELECT record_position,input_hash,raw_json FROM source_records WHERE source_path=?',(path,))}
                if set(expected)!={k[0] for k in stored}:raise RuntimeError('JSONL record coverage mismatch: '+path)
                for pos,raw in expected.items():
                    if (pos,hashrow(raw)) not in stored or json.loads(stored[(pos,hashrow(raw))]['raw_json'])!=raw:raise RuntimeError('JSONL record preservation mismatch: '+path)
        return {'source_files':len(self.files),'source_records':self.c.execute('SELECT count(*) FROM source_records').fetchone()[0],
            'dispositions':[dict(r) for r in self.c.execute('SELECT source_path,state,reason,count(*) AS count FROM source_records GROUP BY source_path,state,reason ORDER BY source_path,state,reason')],
            'identity_strategies':[dict(r) for r in self.c.execute('SELECT strategy,reason,count(*) AS count FROM article_identity_assessments GROUP BY strategy,reason')],
            'conflicts':self.c.execute('SELECT count(*) FROM consolidation_conflicts').fetchone()[0]}

def counts(c):
    return {r[0]:c.execute('SELECT count(*) FROM '+r[0]).fetchone()[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}

def import_snapshot(snapshot,database,guard=None):
    snapshot=Path(snapshot).resolve();manifest=json.loads((snapshot/'input-files.json').read_text());baseline_result=json.loads((snapshot/'phase1-result.json').read_text())
    if baseline_result['status']!='complete' or baseline.sha(snapshot/'n8n.sqlite')!=baseline_result['backup_sha256']:raise RuntimeError('Snapshot not verified')
    files=[r for r in manifest if any(r['path'].startswith('content/'+d+'/') for d in baseline.TARGETS)]
    for item in files:
        if baseline.sha(snapshot/'files'/item['path'])!=item['sha256']:raise RuntimeError('Snapshot input changed')
    input_hash=hashrow({'database':baseline_result['backup_sha256'],'files':files})
    run_id=ident('migration',VERSION,input_hash);implementation=VERSION+':'+baseline.sha(Path(__file__))
    if guard:guard()
    c=db.connect(database)
    try:
        before=counts(c)
        with db.transaction(c):
            existing=c.execute('SELECT * FROM migration_runs WHERE id=?',(run_id,)).fetchone()
            if existing:
                if existing['status']!='complete' or existing['importer_version']!=implementation:raise RuntimeError('Migration replay contract differs')
                result=json.loads(existing['result_json']);result['replayed']=True
            else:
                if c.execute('SELECT count(*) FROM migration_runs').fetchone()[0]:raise RuntimeError('Different snapshot requires an isolated target')
                stamp=utc(baseline_result['finished_at'])
                c.execute("INSERT INTO migration_runs(id,input_snapshot,importer_version,started_at,status) VALUES (?,?,?,?,'running')",(run_id,str(snapshot),implementation,db.now()))
                engine=Importer(c,snapshot,run_id,stamp,files)
                for stage in ('preload','archive_files','structured','n8n','contents','reviews','summaries','histories'):
                    print('Import stage: '+stage,flush=True)
                    getattr(engine,stage)()
                result=engine.verify();result.update(run_id=run_id,input_hash=input_hash,replayed=False,implementation=implementation)
                if guard:guard()
                c.execute("UPDATE migration_runs SET status='complete',completed_at=?,result_json=? WHERE id=?",(db.now(),db.canonical(result),run_id))
        result['row_deltas']={k:n-before.get(k,0) for k,n in counts(c).items()}
        return result
    finally:c.close()

def checked_import(snapshot,database):
    snapshot=Path(snapshot)
    expected=json.loads((snapshot/'database-baseline.json').read_text())
    manifest=json.loads((snapshot/'input-files.json').read_text())
    expected_files={r['path']:r for r in manifest if any(r['path'].startswith('content/'+d+'/') for d in baseline.TARGETS)}
    def guard():
        with closing(baseline.ro(Path.home()/'.n8n/database.sqlite')) as source:
            baseline.idle(source)
            source.execute('BEGIN')
            if baseline.baseline(source)!=expected:raise RuntimeError('Live source database changed')
            source.rollback()
        actual={r['path']:r for r in baseline.inventory() if any(r['path'].startswith('content/'+d+'/') for d in baseline.TARGETS)}
        if actual!=expected_files:raise RuntimeError('Live source files changed')
    return import_snapshot(snapshot,database,guard=guard)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--database',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    args=p.parse_args();os.umask(0o077)
    result=checked_import(args.snapshot,args.database);args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='dispositions'}))
if __name__=='__main__':sys.dont_write_bytecode=True;main()
