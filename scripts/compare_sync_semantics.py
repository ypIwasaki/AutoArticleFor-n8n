"""Independent all-source semantic audit across registered input versions."""
from pathlib import Path
from contextlib import ExitStack,closing
import json,collections
import project_database as db
import database_phase1 as base
from compare_phase4_database import Compare,Difference,stamp
from compare_phase4_details import Details
from article_artifact_formats import parsed_summaries
import identity_history_dependencies as origins
import missing_body_acceptance as acceptance

class SemanticCompare(Details):
    def __init__(self,c,snapshot,out,stack):
        from sync_snapshot_dependencies import verify_project_snapshot
        verify_project_snapshot(snapshot)
        self.snapshot=Path(snapshot);self.manifest=json.loads((self.snapshot/'input-files.json').read_text())
        legacy=stack.enter_context(closing(base.ro(self.snapshot/'n8n.sqlite')))
        super().__init__(c,legacy,self.snapshot/'files',out)
        policy=json.loads((self.snapshot/'policy-dependencies.json').read_text())
        self.readers=[self]
        for folder,origin in origins.verify(self.snapshot,policy['identity_history']):
            old=stack.enter_context(closing(base.ro(folder/'n8n.sqlite')))
            reader=object.__new__(Compare)
            reader.c=c;reader.legacy=old;reader.root=folder/'files';reader.documents={};self.readers.append(reader)
        self.prior=stack.enter_context(closing(db.connect(self.snapshot/'project.sqlite',readonly=True)))
    def original(self,s):
        for reader in self.readers:
            try:
                if s['source_path'].startswith('n8n:'):
                    row=reader.legacy.execute('SELECT * FROM '+base.q(s['source_path'][4:])+' WHERE id=?',(int(s['record_position']),)).fetchone();raw=dict(row) if row else None
                else:raw=Compare.original(reader,s)
            except (FileNotFoundError,KeyError,IndexError):continue
            if raw is not None and db.checksum(db.canonical(raw).encode())==s['input_hash']:return raw
        previous=self.prior.execute('SELECT * FROM source_records WHERE id=?',(s['id'],)).fetchone()
        if previous and previous['source_path']==s['source_path'] and previous['record_position']==s['record_position'] and previous['input_hash']==s['input_hash']:
            raw=json.loads(previous['raw_json'])
            if db.checksum(db.canonical(raw).encode())==s['input_hash']:return raw
        raise Difference('source_version_not_in_registered_inputs:'+s['source_path']+':'+s['record_position'])
    def coverage(self):
        self.stage='source_population';expected=set();groups={'talent-index-proposals':('articles','talents','articleTalents'),'article-classification-proposals':('classifications',),'article-feedback-instructions':('feedback',),'official-talent-registry':('groups','talents')}
        def add(path,pos,raw):expected.add((path,pos,db.checksum(db.canonical(raw).encode())))
        for table in self.legacy.execute('SELECT id,name FROM data_table'):
            if table['name'] not in base.KEYS:continue
            path='n8n:data_table_user_'+table['id']
            for raw in self.legacy.execute('SELECT * FROM '+base.q(path[4:])):add(path,str(raw['id']),dict(raw))
        for item in self.manifest:
            path=item['path'];file=self.root/path;before=len(expected);directory=Path(path).parts[1]
            if path.endswith('.jsonl'):
                for number,line in enumerate(file.read_text(encoding='utf-8-sig').splitlines(),1):
                    if line.strip():add(path,'line:'+str(number),json.loads(line))
            elif path.endswith('.json') and directory=='article-body-captures':
                raw=json.loads(file.read_text())
                if Path(path).name=='backfill-state.json':
                    add(path,'header',{k:v for k,v in raw.items() if k not in ('entries','resolvedUrls')})
                    for key,value in raw.get('entries',{}).items():add(path,'entries/'+key,value)
                    for key,value in raw.get('resolvedUrls',{}).items():add(path,'resolvedUrls/'+key,dict(originalUrl=key,value=value))
                else:add(path,'$',raw)
            elif path.endswith('.json') and directory in groups:
                raw=json.loads(file.read_text());keys=groups[directory];add(path,'header',{k:v for k,v in raw.items() if k not in keys})
                for key in keys:
                    for number,value in enumerate(raw.get(key,[])):add(path,key+'/'+str(number),value)
            elif path.endswith('.md') and directory=='article-summaries':
                from datetime import datetime
                try:datetime.fromisoformat(Path(path).stem)
                except ValueError:pass
                else:
                    for i,(links,summary) in enumerate(parsed_summaries(file.read_text(encoding='utf-8-sig'),Path(path).stem)):
                        for j,(_,url) in enumerate(links):add(path,'summary:'+str(i)+':link:'+str(j),dict(summary,links=links,url=url))
            if len(expected)==before:add(path,'file',dict(sha256=item['sha256'],size=item['size']))
        actual={(x['source_path'],x['record_position'],x['input_hash']) for x in self.c.execute('SELECT * FROM source_records')}
        self.equal('missing_current_input_units',[],sorted(expected-actual))
        # Additional rows must independently resolve to an actual historical input.
        self.equal('current_input_units_covered',len(expected),len(expected&actual))
    def summary_fields(self):
        self.stage='summaries'
        for s in self.c.execute("SELECT * FROM source_records WHERE target_kind='article_summaries'"):
            raw=self.original(s);t=self.c.execute('SELECT * FROM article_summaries WHERE id=?',(s['target_id'],)).fetchone();self.current=dict(source_path=s['source_path'],position=s['record_position'],target_id=s['target_id'])
            self.equal('text',raw['text'],t['summary']);self.equal('source',s['source_path'],t['source']);self.equal('review_binding',None,t['review_id'])
        for a in self.c.execute('SELECT DISTINCT article_id FROM article_summaries'):
            rows=[dict(x) for x in self.c.execute('SELECT * FROM article_summaries WHERE article_id=?',(a[0],))];latest=max(x['saved_at'] for x in rows);last=[x for x in rows if x['saved_at']==latest]
            self.current=dict(article_id=a[0]);self.equal('versions',list(range(1,len(rows)+1)),sorted(x['version'] for x in rows));self.equal('current',[last[0]['id']] if len(last)==1 else [],[x['id'] for x in rows if x['is_current']])
    def conflicts(self):
        self.stage='conflicts';old={x['id']:dict(x) for x in self.prior.execute('SELECT * FROM consolidation_conflicts')};new={x['id']:dict(x) for x in self.c.execute('SELECT * FROM consolidation_conflicts')}
        for key,row in old.items():self.current={'conflict_id':key};self.equal('preserved_all_fields',row,new.get(key))
        tables={x['name']:'data_table_user_'+x['id'] for x in self.legacy.execute('SELECT id,name FROM data_table')};articles=[dict(x) for x in self.legacy.execute('SELECT * FROM '+base.q(tables['articles'])+' ORDER BY id')]
        for key in sorted(set(new)-set(old)):
            row=new[key];detail=json.loads(row['details_json']);self.current=dict(conflict_id=key,source_path=detail.get('source_path'))
            self.equal('unresolved','unresolved',row['status']);self.equal('identity_kind','identity',row['kind'])
            path=detail.get('source_path');position=str(detail.get('source_position'));sources=list(self.c.execute('SELECT * FROM source_records WHERE source_path=? AND record_position=?',(path,position)))
            self.equal('source_present',True,bool(sources));raw=self.original(sources[-1]);url=raw.get('originalUrl',raw.get('original_url',raw.get('url','')));key_value=raw.get('articleKey',raw.get('article_key'))
            self.equal('old_key',key_value,detail['old_key']);self.equal('held_article','held',self.c.execute('SELECT identity_state FROM articles WHERE id=?',(row['article_id'],)).fetchone()[0])
            if path.startswith('content/article-body-captures/'):
                candidates=[x for x in articles if x['url']==url];actual=detail['candidates']
                self.equal('candidates',[(tables['articles'],x['id'],x['article_key'],db.checksum(db.canonical(x).encode())) for x in candidates],[(x['source_table'],x['source_row_id'],x['old_article_key'],x['input_hash']) for x in actual])
                exact=[x for x in articles if x['article_key']==key_value and x['url']==url];self.equal('no_exact_article_key',[],exact)
                self.equal('reason','file_content_identity_not_proven',row['reason'])
            elif path.startswith('content/article-summaries/'):
                self.equal('reason','summary_article_identity_not_unique',row['reason']);matches=[]
                # Summary links retain titles; locate same-day exact title/URL observations.
                day=Path(path).stem;title=next((title for title,u in raw['links'] if u==raw['url']),None)
                for s in self.c.execute("SELECT * FROM source_records WHERE source_path=? AND target_kind='article_occurrences'",('content/structured-records/'+day+'.jsonl',)):
                    value=self.original(s)['article']
                    if value['url']==url and value.get('title')==title:matches.append(self.c.execute('SELECT article_id FROM article_occurrences WHERE id=?',(s['target_id'],)).fetchone()[0])
                self.equal('identity_not_unique',True,len(set(matches))!=1)
            else:raise Difference('unverified_new_conflict_kind:'+str(path))
        acceptance.plan(self.c,acceptance.load(self.root/acceptance.LEDGER_PATH))

def verify(c,snapshot,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    with ExitStack() as stack:
        check=SemanticCompare(c,snapshot,out,stack)
        try:
            for stage in ('coverage','sources','n8n','collections','identifiers','bodies','reviews','summary_fields','conflicts'):getattr(check,stage)()
            report={'status':'passed','field_checks':dict(check.stats),'field_hashes':{k:v.hexdigest() for k,v in check.hashes.items()},'unapproved_differences':0}
        finally:check.diffs.close()
    base.save(out/'result.json',report);return report
