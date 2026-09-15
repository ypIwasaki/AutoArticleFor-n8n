"""Compare legacy-compatible outputs reconstructed from the dedicated DB. No cutover."""
import contextlib,importlib.util,json,os,collections
from pathlib import Path
from unittest.mock import patch
import project_database as db
import database_phase1 as base
from compare_phase4_database import Compare,Difference,fingerprint
import read_ai_inputs,weekly_metrics

def main():
    root=base.ROOT;run=Path((root/'.operation-state/database-phase4/latest-path.txt').read_text());assert json.loads((run/'details/comparison-result.json').read_text())['status']=='detail_checks_passed'
    out=run/'outputs';out.mkdir(exist_ok=True);export=out/'database-compatibility-files';export.mkdir(exist_ok=True)
    before=fingerprint(root/'data/autoarticle.sqlite');report=dict(phase=4,status='running',phase5_started=False,mode='Read-only legacy-compatible projection; operational held/unverified states remain separately recorded')
    try:
        with contextlib.closing(db.connect(root/'data/autoarticle.sqlite',readonly=True)) as c,contextlib.closing(base.ro(Path.home()/'.n8n/database.sqlite')) as legacy:
            c.execute('BEGIN');legacy.execute('BEGIN');audit=Compare(c,legacy,root,out)
            for row in c.execute('SELECT * FROM migration_source_files'):
                p=export/row['path'];assert export.resolve() in p.resolve().parents;p.parent.mkdir(parents=True,exist_ok=True)
                if not p.exists():p.write_bytes(row['content'])
                assert base.sha(p)==row['sha256']
            # Out-of-scope code/configuration are the same immutable comparison inputs on both sides.
            for relative in ('scripts','config','docs/ai-rules','content/ai-keyword-candidates','content/keyword-candidates','content/analysis','content/weekly-reports'):
                p=export/relative;p.parent.mkdir(parents=True,exist_ok=True)
                if not p.exists():p.symlink_to(root/relative,target_is_directory=True)
            days=sorted(p.stem for p in (root/'content/structured-records').glob('*.jsonl'))
            audit.stage='daily_aggregates'
            for day in days:
                raw=[json.loads(line) for line in (root/'content/structured-records'/(day+'.jsonl')).read_text().splitlines() if line.strip()];articles=[x['article'] for x in raw if x.get('recordType')=='article']
                actual=[dict(x) for x in c.execute('SELECT o.title,o.url,o.published_at,o.excerpt FROM article_occurrences o JOIN collection_runs r ON r.id=o.collection_run_id WHERE r.run_date=?',(day,))]
                audit.current=dict(source_path='content/structured-records/'+day+'.jsonl')
                audit.equal('daily_articles',len(articles),len(actual));audit.equal('daily_urls',dict(collections.Counter(x['url'] for x in articles)),dict(collections.Counter(x['url'] for x in actual)))
            audit.stage='weekly_outputs';weekly_errors=[];weekly_successes=[];original_errors=[]
            def weekly_result(source_root,day):
                try:return dict(status='success',output=weekly_metrics.build_metrics(source_root,day,as_of=day))
                except ValueError as exc:
                    if not str(exc).startswith(('could not convert string to float:','Incomplete keyword candidate row:','Invalid keyword candidate decision:')):raise
                    original_errors.append(dict(day=day,source='legacy' if source_root==root else 'project_compatibility',message=str(exc)))
                    return dict(status='existing_keyword_parser_error',error_type=type(exc).__name__,message=str(exc).replace(str(source_root),'<comparison-root>'))
            for day in days:
                audit.current=dict(through=day,as_of=day);old=weekly_result(root,day);new=weekly_result(export,day)
                audit.equal('weekly_complete_result',old,new)
                if old['status']=='success':weekly_successes.append(day)
                else:
                    from datetime import date,timedelta
                    cutoff=date.fromisoformat(day);monday=cutoff-timedelta(days=cutoff.weekday());references=[]
                    for p in weekly_metrics.dated_paths(root/'content/ai-keyword-candidates','.md',day,monday.isoformat()):
                        for number,line in enumerate(p.read_text(encoding='utf-8-sig').splitlines(),1):
                            if not line.startswith('|'):continue
                            cells=[v.strip() for v in line.strip().strip('|').split('|')]
                            if cells[0]=='Candidate' or cells[0].startswith('---'):continue
                            if len(cells)<6:
                                references.append(dict(path=str(p.relative_to(root)),line=number,field_count=len(cells),file_hash=base.sha(p)));continue
                            try:float(cells[2])
                            except ValueError:references.append(dict(path=str(p.relative_to(root)),line=number,saved_confidence=cells[2],file_hash=base.sha(p)))
                    weekly_errors.append(dict(day=day,legacy=old,project=new,references=references,original_errors=[x for x in original_errors if x['day']==day],scope='Unchanged out-of-scope AI keyword candidate file; not a migration difference'))
                base.save(out/('weekly-'+day+'.json'),dict(old_hash=db.checksum(db.canonical(old).encode()),new_hash=db.checksum(db.canonical(new).encode()),equal=True,status=old['status']))
            base.save(out/'weekly-existing-errors.json',weekly_errors)
            assert days[-1] in weekly_successes, 'Latest weekly representative cannot be generated'

            audit.stage='daily_reader_representatives'
            representative_days=sorted(set((days[0],days[len(days)//2],days[-1])))
            for day in representative_days:
                for task in read_ai_inputs.TASKS:
                    audit.current=dict(day=day,task=task)
                    old=read_ai_inputs.build_payload(root,day,task,offset=0,limit=20,include_body=True);new=read_ai_inputs.build_payload(export,day,task,offset=0,limit=20,include_body=True)
                    audit.equal('reader_complete_output',old,new)
            audit.stage='dashboard_complete_output'
            spec=importlib.util.spec_from_file_location('phase4_dashboard',root/'apps/talent-dashboard/server.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            from datetime import datetime,timezone
            fixed_time=datetime.fromisoformat(json.loads((run/'gate.json').read_text())['checked_at'].replace('Z','+00:00'))
            class ComparisonDatetime(datetime):
                @classmethod
                def now(cls,tz=None):return fixed_time.astimezone(tz) if tz else fixed_time.replace(tzinfo=None)
            with patch.object(module,'datetime',ComparisonDatetime):old=module.build_dashboard()
            audit.equal('legacy_no_fallback',None,old.get('sourceError'))
            payload={};names={'data_table_user_'+x['id']:x['name'] for x in legacy.execute('SELECT id,name FROM data_table')}
            for source in c.execute("SELECT * FROM source_records WHERE source_path LIKE 'n8n:%' ORDER BY CAST(record_position AS INTEGER)"):
                name=names[source['source_path'][4:]]
                if name=='article_contents':continue
                raw=json.loads(source['raw_json']);row=c.execute('SELECT * FROM '+name+' WHERE id=?',(source['target_id'],)).fetchone()
                # Identity fields and historical metadata are retained from provenance; active columns use DB values.
                fields={'articles':('title','url','excerpt','source'),'talents':('display_name','organization','status','search_enabled','auto_discovered'),'article_talents':('confidence',),'article_classifications':('article_type','primary_category','relevance','confidence'),'article_feedback':('is_rejected','reason_code')}.get(name,())
                for field in fields:
                    value=row[field];raw[field]=None if raw.get(field) is None and value=='' else value
                payload.setdefault(name,[]).append(module.normalise_row(raw))
            payload['_article_feedback_available']=True
            with patch.object(module,'PROJECT_ROOT',export),patch.object(module,'load_from_n8n',return_value=(payload,'n8n-data-tables')),patch.object(module,'datetime',ComparisonDatetime):new=module.build_dashboard()
            audit.equal('project_no_fallback',None,new.get('sourceError'))
            # Use the same rendering instant for both paths; do not mask a data difference.
            audit.equal('generatedAt',old['generatedAt'],new['generatedAt'])
            old_without_time={k:v for k,v in old.items() if k!='generatedAt'};new_without_time={k:v for k,v in new.items() if k!='generatedAt'}
            audit.equal('dashboard_all_business_fields',old_without_time,new_without_time)
            base.save(out/'dashboard-old.json',old);base.save(out/'dashboard-project.json',new)
            report.update(status='passed',daily_dates=len(days),weekly_outputs=len(days),weekly_successful_outputs=len(weekly_successes),weekly_existing_error_parity=len(weekly_errors),weekly_error_dates=[x['day'] for x in weekly_errors],latest_weekly_successful=True,reader_representatives=len(representative_days)*len(read_ai_inputs.TASKS),dashboard_business_fields_equal=True)
    except Difference as exc:report.update(status='stopped',reason=str(exc),difference_record='outputs/differences.jsonl')
    finally:
        if 'audit' in locals():audit.diffs.close();report.update(field_checks=dict(audit.stats),field_hashes={k:v.hexdigest() for k,v in audit.hashes.items()})
        report['project_database_unchanged']=before==fingerprint(root/'data/autoarticle.sqlite');report['checked_at']=base.now();base.save(out/'comparison-result.json',report);print(json.dumps(report))
if __name__=='__main__':main()
