"""Read-only phase 4 comparison; stop at the first unexplained difference."""
import argparse,collections,contextlib,hashlib,json,os,sqlite3
from datetime import datetime,timezone
from pathlib import Path
import project_database as db
import database_phase1 as base
from article_artifact_formats import parsed_summaries

class Difference(RuntimeError):pass

def stamp(v):
    if v is None or v=='':return None
    if isinstance(v,(int,float)):return datetime.fromtimestamp(v,timezone.utc).isoformat()
    d=datetime.fromisoformat(v.replace('Z','+00:00'))
    return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)).isoformat()

def fingerprint(path):
    result={}
    with contextlib.closing(db.connect(path,readonly=True)) as c:
        c.execute('PRAGMA cache_size=-262144')
        c.execute('BEGIN')
        for name, in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            cols=list(c.execute('PRAGMA table_info('+name+')'));keys=[x['name'] for x in sorted(cols,key=lambda x:x['pk']) if x['pk']];h=hashlib.sha256();count=0
            for row in c.execute('SELECT * FROM '+name+' ORDER BY '+','.join('"'+k+'"' for k in keys)):
                o={k:({'blob_sha256':db.checksum(v),'size':len(v)} if isinstance(v,bytes) else v) for k,v in dict(row).items()};h.update((db.canonical(o)+chr(10)).encode());count+=1
            result[name]=dict(rows=count,sha256=h.hexdigest())
        result['sqlite_schema']=dict(sha256=db.checksum(db.canonical([dict(x) for x in c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]).encode()))
    return result

class Compare:
    def __init__(self,c,legacy,root,out):
        self.c=c;self.legacy=legacy;self.root=root;self.out=out;self.stats=collections.Counter();self.hashes={};self.stage='initial';self.current={};self.diffs=(out/'differences.jsonl').open('w');self.documents={}
    def equal(self,field,old,new,reason=None):
        self.stats[self.stage]+=1
        h=self.hashes.setdefault(self.stage,hashlib.sha256());h.update((db.canonical([field,old,new])+chr(10)).encode())
        if db.canonical(old)==db.canonical(new):return
        detail=dict(stage=self.stage,target=self.current,field=field,old_value=old,new_value=new,reason=reason,source=self.current.get('source_path'),approval_status='approved' if reason else 'unapproved')
        self.diffs.write(db.canonical(detail)+chr(10));self.diffs.flush()
        if not reason:raise Difference(self.stage+': '+field)
    def doc(self,path):
        if path not in self.documents:
            p=self.root/path
            if path.endswith('.jsonl'):self.documents[path]={'line:'+str(n):json.loads(line) for n,line in enumerate(p.read_text(encoding='utf-8-sig').splitlines(),1) if line.strip()}
            elif path.endswith('.json'):self.documents[path]=json.loads(p.read_text(encoding='utf-8-sig'))
            else:self.documents[path]=p.read_text(encoding='utf-8-sig')
        return self.documents[path]
    def original(self,s):
        path=s['source_path'];pos=s['record_position']
        if path.startswith('n8n:'):
            row=self.legacy.execute('SELECT * FROM '+base.q(path[4:])+' WHERE id=?',(pos,)).fetchone()
            return dict(row) if row else None
        if pos=='file':
            p=self.root/path;return dict(sha256=base.sha(p),size=p.stat().st_size)
        o=self.doc(path)
        if path.endswith('.jsonl'):return o.get(pos)
        if path.endswith('.json'):
            if pos=='$':return o
            if pos=='header':
                keys={'talent-index-proposals':('articles','talents','articleTalents'),'article-classification-proposals':('classifications',),'article-feedback-instructions':('feedback',),'official-talent-registry':('groups','talents'),'article-body-captures':('entries','resolvedUrls')}[Path(path).parts[1]]
                return {k:v for k,v in o.items() if k not in keys}
            key,idx=pos.split('/',1)
            if key=='entries':return o[key][idx]
            if key=='resolvedUrls':return dict(originalUrl=idx,value=o[key][idx])
            return o[key][int(idx)]
        if pos.startswith('summary:'):
            _,i,_,j=pos.split(':');links,summary=list(parsed_summaries(o,Path(path).stem))[int(i)];return dict(summary,links=links,url=links[int(j)][1])
        raise Difference('Unsupported source position '+path+':'+pos)
    def files(self):
        self.stage='source_files'
        expected={x['path']:x for x in base.inventory() if any(x['path'].startswith('content/'+d+'/') for d in base.TARGETS)}
        actual={x[0] for x in self.c.execute('SELECT path FROM migration_source_files')};self.equal('file_paths',sorted(expected),sorted(actual))
        for row in self.c.execute('SELECT * FROM migration_source_files'):
            self.current=dict(source_path=row['path']);raw=(self.root/row['path']).read_bytes();self.equal('file_bytes_sha256',db.checksum(raw),db.checksum(row['content']));self.equal('stored_file_hash',db.checksum(raw),row['sha256']);self.equal('file_size',len(raw),row['size'])
    def sources(self):
        self.stage='all_source_units'
        for s in self.c.execute('SELECT * FROM source_records ORDER BY source_path,record_position'):
            self.current=dict(source_record_id=s['id'],source_path=s['source_path'],position=s['record_position'],target_kind=s['target_kind'],target_id=s['target_id']);raw=self.original(s)
            self.equal('original_row',raw,json.loads(s['raw_json']));self.equal('input_hash',db.checksum(db.canonical(raw).encode()),s['input_hash'])
            if s['state']=='held':self.equal('hold_reason_present',True,bool(s['reason']) and s['reason']!='processing')
            if not s['target_kind']:
                self.equal('unprojected_record_is_held','held',s['state']);continue
            t=self.c.execute('SELECT * FROM '+base.q(s['target_kind'])+' WHERE id=?',(s['target_id'],)).fetchone();self.equal('target_exists',True,t is not None)
            field='raw_json' if 'raw_json' in t.keys() else {'collection_runs':'search_conditions_json','article_occurrences':'observations_json','content_runtime_state':'state_json'}.get(s['target_kind'])
            if field:
                expected=raw['value'] if s['target_kind']=='content_runtime_state' and s['record_position'].startswith('resolvedUrls/') else raw
                self.equal('target_preserved_row',expected,json.loads(t[field]))
    def n8n(self):
        self.stage='current_n8n_fields'
        mapping={x['name']:'data_table_user_'+x['id'] for x in self.legacy.execute('SELECT id,name FROM data_table') if x['name'] in base.KEYS}
        fields={'articles':('title','url','excerpt','source','published_at','createdAt','updatedAt'),'talents':('display_name','organization','status','search_enabled','auto_discovered','last_seen_at'),'article_talents':('evidence_text','confidence'),'article_classifications':('article_type','primary_category','relevance','confidence','evidence_text','classified_at'),'article_feedback':('is_rejected','reason_code','reviewed_at','review_source')}
        rename={'createdAt':'created_at','updatedAt':'updated_at','evidence_text':'evidence','review_source':'source'}
        for name,keys in fields.items():
            for raw in self.legacy.execute('SELECT * FROM '+base.q(mapping[name])+' ORDER BY id'):
                raw=dict(raw);s=self.c.execute('SELECT * FROM source_records WHERE source_path=? AND record_position=?',('n8n:'+mapping[name],str(raw['id']))).fetchone();t=self.c.execute('SELECT * FROM '+name+' WHERE id=?',(s['target_id'],)).fetchone();self.current=dict(source_path=s['source_path'],position=s['record_position'],target_id=s['target_id'])
                for key in keys:
                    old=raw.get(key);new=t[rename.get(key,key)]
                    if key.endswith('_at') or key in ('createdAt','updatedAt'):old,new=stamp(old),stamp(new)
                    elif key in ('excerpt','source','organization','evidence_text','reason_code'):old=old or ''
                    elif key=='review_source':old=old or 'legacy-n8n'
                    self.equal(key,old,new)
                if name=='talents':self.equal('aliases',sorted(set(json.loads(raw['aliases_json'] or '[]'))),sorted(x[0] for x in self.c.execute('SELECT alias FROM talent_aliases WHERE talent_id=?',(t['id'],))))
                if name=='article_classifications':self.equal('secondary_categories',sorted(set(json.loads(raw['secondary_categories_json'] or '[]'))),sorted(x[0] for x in self.c.execute('SELECT category FROM classification_secondary_categories WHERE classification_id=?',(t['id'],))))
                if name in ('articles','article_talents','article_classifications','article_feedback'):
                    aid=t['id'] if name=='articles' else t['article_id'];identifier=self.c.execute("SELECT article_id FROM article_identifiers WHERE source=? AND kind='legacy_key' AND value=?",('n8n:'+mapping['articles'],raw['article_key'])).fetchone();self.equal('old_key_article_id',identifier[0] if identifier else None,aid)
                if name in ('article_classifications','article_feedback'):
                    self.equal('current_version',1,t['version']);self.equal('is_current',1,t['is_current'])
                if name=='article_talents':
                    duplicate_count=self.legacy.execute('SELECT count(*) FROM '+base.q(mapping[name])+' WHERE article_key=? AND talent_id=?',(raw['article_key'],raw['talent_id'])).fetchone()[0]
                    self.equal('relationship_state','held' if duplicate_count>1 else 'current',t['state'])
                    talent=self.c.execute("SELECT target_id FROM source_records WHERE source_path=? AND json_extract(raw_json,'$.talent_id')=?",('n8n:'+mapping['talents'],raw['talent_id'])).fetchone();self.equal('talent_relation_id',talent[0],t['talent_id'])
    def collections(self):
        self.stage='collection_history'
        for s in self.c.execute("SELECT * FROM source_records WHERE target_kind IN ('collection_runs','article_occurrences') ORDER BY source_path,record_position"):
            raw=self.original(s);t=self.c.execute('SELECT * FROM '+s['target_kind']+' WHERE id=?',(s['target_id'],)).fetchone();self.current=dict(source_path=s['source_path'],position=s['record_position'],target_id=s['target_id'])
            if s['target_kind']=='collection_runs':
                self.equal('run_date',raw['runDate'],t['run_date']);self.equal('execution_id',str(raw.get('workflowExecutionId','')) or None,t['workflow_execution_id'])
                if raw.get('generatedAt'):self.equal('observed_at',stamp(raw['generatedAt']),stamp(t['observed_at']))
                count=sum(x.get('recordType')=='article' for x in self.doc(s['source_path']).values());self.equal('occurrence_count',count,self.c.execute('SELECT count(*) FROM article_occurrences WHERE collection_run_id=?',(t['id'],)).fetchone()[0])
            else:
                for key in ('title','excerpt','url'):self.equal(key,raw['article'].get(key) or '',t[key])
                self.equal('published_at',stamp(raw['article'].get('publishedAt')),stamp(t['published_at']))

    def identifiers(self):
        self.stage='old_new_identifiers'
        for p in self.c.execute('SELECT * FROM article_source_provenance'):
            s=self.c.execute('SELECT * FROM source_records WHERE id=?',(p['source_record_id'],)).fetchone();raw=self.original(s)
            if raw.get('recordType')=='article':raw=raw['article']
            self.current=dict(source_path=s['source_path'],position=s['record_position'],article_id=p['article_id'])
            self.equal('provenance_input_hash',db.checksum(db.canonical(raw).encode()),p['input_hash'])
            key=raw.get('articleKey',raw.get('article_key')) or raw.get('article_key');url=raw.get('originalUrl',raw.get('original_url')) or raw.get('url')
            self.equal('old_key',key,p['old_article_key']);self.equal('original_url',url,p['original_url'])
            target=self.c.execute('SELECT * FROM '+base.q(s['target_kind'])+' WHERE id=?',(s['target_id'],)).fetchone()
            self.equal('article_mapping',target['id'] if s['target_kind']=='articles' else target['article_id'],p['article_id'])

    def run(self):
        for stage in ('files','sources','n8n','collections','identifiers'):
            print('Compare: '+stage,flush=True);getattr(self,stage)()

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();out=args.output;out.mkdir(exist_ok=True,parents=True);root=base.ROOT;target=root/'data/autoarticle.sqlite';before=fingerprint(target);base.save(out/'target-before.json',before)
    report=dict(phase=4,status='running',stages_completed=[],phase5_started=False)
    try:
        with contextlib.closing(db.connect(target,readonly=True)) as c,contextlib.closing(base.ro(Path.home()/'.n8n/database.sqlite')) as legacy:
            c.execute('BEGIN');legacy.execute('BEGIN');compare=Compare(c,legacy,root,out);compare.run();report['status']='core_checks_passed'
    except Difference as exc:report.update(status='stopped',reason=str(exc),difference_record='differences.jsonl')
    finally:
        if 'compare' in locals():
            compare.diffs.close();report['field_checks']=dict(compare.stats);report['field_hashes']={k:v.hexdigest() for k,v in compare.hashes.items()};report['last_stage']=compare.stage
        after=fingerprint(target);base.save(out/'target-after.json',after);report['project_database_unchanged']=before==after;report['checked_at']=base.now();base.save(out/'comparison-result.json',report);print(json.dumps(report))
if __name__=='__main__':main()
