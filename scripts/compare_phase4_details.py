"""Detailed read-only semantic comparisons, following the phase 4 source audit."""
import contextlib,json,collections
from pathlib import Path
from compare_phase4_database import Compare,Difference,stamp,fingerprint
import project_database as db
import database_phase1 as base
import article_review_facts as rules

class Details(Compare):
    def bodies(self):
        self.stage='bodies_versions_attempts';times=collections.defaultdict(list)
        for s in self.c.execute("SELECT * FROM source_records WHERE target_kind='content_fetch_attempts' ORDER BY source_path,record_position"):
            raw=self.original(s);t=self.c.execute('SELECT * FROM content_fetch_attempts WHERE id=?',(s['target_id'],)).fetchone();self.current=dict(source_path=s['source_path'],position=s['record_position'],target_id=t['id'])
            def val(a,b,default=None):return raw.get(a,raw.get(b,default))
            text=val('contentText','content_text','') or '';stored=val('contentHash','content_hash','');status=val('contentStatus','content_status',raw.get('status'));length=val('contentLength','content_length',raw.get('body_length'))
            self.equal('source_status',status,t['source_status']);self.equal('stored_body_hash',stored,t['stored_body_hash']);self.equal('stored_body_length',length,t['stored_body_length'],'Schema 003 INTEGER representation of an equal integral legacy numeric value; original row representation retained' if isinstance(length,(int,float)) and not isinstance(length,bool) and length==t['stored_body_length'] and int(length)==length else None)
            self.equal('original_url',val('originalUrl','original_url','') or '',t['original_url']);self.equal('resolved_url',val('resolvedUrl','resolved_url',''),t['resolved_url']);self.equal('content_path',raw.get('content_path'),t['source_content_path'])
            self.equal('failure_reason',val('failureReason','failure_reason',raw.get('reason')),t['failure_reason']);self.equal('extraction_method',val('extractionMethod','extraction_method',''),t['extraction_method']);self.equal('completeness',val('contentCompleteness','content_completeness',''),t['completeness'])
            self.equal('retry_after',stamp(raw.get('retry_after')),stamp(t['retry_after']))
            fetched=val('fetchedAt','fetched_at',raw.get('processed_at'))
            if fetched:self.equal('fetched_at',stamp(fetched),stamp(t['fetched_at']))
            missing=not text and bool(length and length>0) and bool(stored) and stored!=db.checksum(b'')
            if missing:
                self.equal('held_integrity','held_missing_body',t['body_integrity']);self.equal('payload_version',None,t['version_id']);self.equal('source_hold','held',s['state'])
                self.equal('operational_status',status,t['status'],'User-approved missing-body policy: source claim retained, unavailable body is unverified')
                self.equal('expected_operational_status','unverified',t['status'])
                continue
            self.equal('operational_status',status,t['status'])
            self.equal('computed_body_hash',db.checksum(text.encode()),t['computed_body_hash'])
            markdown=val('contentMarkdown','content_markdown','') or '';metadata=val('pageMetadata','page_metadata',{}) or {}
            if text or markdown or metadata:
                v=self.c.execute('SELECT v.*,p.text,p.text_hash,p.markdown,p.metadata_json,p.non_content_text,p.extraction_scope FROM article_content_versions v JOIN content_payloads p ON p.id=v.payload_id WHERE v.id=?',(t['version_id'],)).fetchone();self.equal('version_exists',True,v is not None)
                for key,old,new in [('text',text,v['text']),('text_hash',db.checksum(text.encode()),v['text_hash']),('markdown',markdown,v['markdown']),('metadata',metadata,json.loads(v['metadata_json'])),('non_content_text',val('nonContentText','non_content_text','') or '',v['non_content_text']),('scope',val('extractionScope','extraction_scope','') or '',v['extraction_scope']),('article_id',t['article_id'],v['article_id'])]:self.equal(key,old,new)
                times[v['id']].append(stamp(t['fetched_at']))
            else:self.equal('no_content_version',None,t['version_id'])
        # Fetch and review inputs can both observe a version.
        for x in self.c.execute('SELECT r.content_version_id,i.capture_json FROM review_records r JOIN review_input_snapshots i ON i.review_id=r.id WHERE r.content_version_id IS NOT NULL'):
            raw=json.loads(x['capture_json']);fetched=raw.get('fetchedAt',raw.get('fetched_at',raw.get('processed_at')))
            if fetched:times[x['content_version_id']].append(stamp(fetched))
        for v in self.c.execute('SELECT * FROM article_content_versions'):
            self.current=dict(version_id=v['id']);observed=times[v['id']];self.equal('version_has_observation',True,bool(observed))
            from datetime import datetime
            self.equal('first_observed_at',min(observed,key=datetime.fromisoformat),stamp(v['first_observed_at']));self.equal('last_observed_at',max(observed,key=datetime.fromisoformat),stamp(v['last_observed_at']))
    def reviews(self):
        self.stage='reviews_facts_entities_quotes'
        for s in self.c.execute("SELECT * FROM source_records WHERE target_kind='review_records' ORDER BY source_path,record_position"):
            raw=self.original(s);t=self.c.execute('SELECT * FROM review_records WHERE id=?',(s['target_id'],)).fetchone();rid=t['id'];self.current=dict(source_path=s['source_path'],position=s['record_position'],review_id=rid)
            for old,new in [('inputHash','input_hash'),('policyHash','rule_hash'),('basis','basis'),('reviewedBy','reviewer')]:self.equal(new,raw[old],t[new])
            self.equal('reviewed_at',stamp(raw['reviewedAt']),stamp(t['reviewed_at']));self.equal('task_statuses',raw['taskStatus'],{x['task']:x['status'] for x in self.c.execute('SELECT * FROM review_task_statuses WHERE review_id=?',(rid,))})
            evidence=[dict(x) for x in self.c.execute('SELECT * FROM review_evidence WHERE review_id=?',(rid,))]
            expected=sorted([(x['field'],x['start'],x['end'],x['quote']) for x in raw['evidence']]);actual=sorted([(x['input_field'],x['start_offset'],x['end_offset'],x['quote']) for x in evidence]);self.equal('quotes_and_positions',expected,actual)
            facts=[dict(x) for x in self.c.execute('SELECT * FROM review_facts WHERE review_id=?',(rid,))];entities=[dict(x) for x in self.c.execute('SELECT * FROM review_entities WHERE review_id=?',(rid,))]
            self.equal('facts',sorted(db.canonical(x) for x in raw['facts']),sorted(db.canonical(json.loads(x['raw_json'])) for x in facts));self.equal('entities',sorted(db.canonical(x) for x in raw['entities']),sorted(db.canonical(json.loads(x['raw_json'])) for x in entities))
            evidence_by_old_id={x['id']:x for x in raw['evidence']}
            for f in facts:
                fr=json.loads(f['raw_json']);self.equal('fact_text',fr['text'],f['fact'])
                expected=sorted((evidence_by_old_id[k]['field'],evidence_by_old_id[k]['start'],evidence_by_old_id[k]['end'],evidence_by_old_id[k]['quote']) for k in set(fr['evidenceIds']))
                actual=sorted(tuple(x) for x in self.c.execute('SELECT e.input_field,e.start_offset,e.end_offset,e.quote FROM review_fact_evidence l JOIN review_evidence e ON e.id=l.evidence_id WHERE l.fact_id=?',(f['id'],)))
                self.equal('fact_evidence_links',expected,actual)
            for e in entities:
                er=json.loads(e['raw_json']);self.equal('entity_name',er['name'],e['name']);self.equal('entity_kind',er['kind'],e['kind'])
                actual=[json.loads(x[0])['id'] for x in self.c.execute('SELECT f.raw_json FROM review_entity_facts l JOIN review_facts f ON f.id=l.fact_id WHERE l.entity_id=?',(e['id'],))];self.equal('entity_fact_links',sorted(set(er['factIds'])),sorted(actual))
            snap=self.c.execute('SELECT * FROM review_input_snapshots WHERE review_id=?',(rid,)).fetchone()
            if snap:
                article=json.loads(snap['article_json']);capture=json.loads(snap['capture_json']);self.equal('reconstructed_input_hash',raw['inputHash'],rules.input_hash(article,capture));rules.validate_record(raw,article,capture,raw['policyHash'])
            else:self.equal('unreconstructed_review_held',True,t['status'] in ('held','needs_review') and s['state']=='held' and bool(s['reason']))
    def summaries_conflicts(self):
        self.stage='summaries_conflicts'
        for s in self.c.execute("SELECT * FROM source_records WHERE target_kind='article_summaries'"):
            raw=self.original(s);t=self.c.execute('SELECT * FROM article_summaries WHERE id=?',(s['target_id'],)).fetchone();self.current=dict(source_path=s['source_path'],position=s['record_position'],target_id=s['target_id']);self.equal('summary_text',raw['text'],t['summary']);self.equal('summary_source',s['source_path'],t['source'])
            self.equal('summary_saved_day',Path(s['source_path']).stem+'T00:00:00+09:00',__import__('datetime').datetime.fromisoformat(t['saved_at'].replace('Z','+00:00')).astimezone(__import__('datetime').timezone(__import__('datetime').timedelta(hours=9))).isoformat())
        for a in self.c.execute('SELECT DISTINCT article_id FROM article_summaries'):
            rows=[dict(x) for x in self.c.execute('SELECT * FROM article_summaries WHERE article_id=?',(a[0],))];latest=max(x['saved_at'] for x in rows);latest_rows=[x for x in rows if x['saved_at']==latest]
            self.current=dict(article_id=a[0]);self.equal('summary_versions',list(range(1,len(rows)+1)),sorted(x['version'] for x in rows))
            expected=[latest_rows[0]['id']] if len(latest_rows)==1 else []
            self.equal('current_summary',expected,[x['id'] for x in rows if x['is_current']])
        previous=json.loads((self.root/'docs/database-phase3-verification.json').read_text());prior=self.root/previous['conflict_records']
        expected={x['id']:x for x in (json.loads(line) for line in prior.read_text().splitlines())}
        actual={x['id']:dict(x) for x in self.c.execute('SELECT * FROM consolidation_conflicts')};self.equal('all_conflicts',expected,actual)
        for x in actual.values():
            self.current=dict(conflict_id=x['id']);self.equal('unresolved_reason',True,x['status']=='unresolved' and bool(x['reason']))
        import accept_phase3_database as acceptance
        accepted=acceptance.verify(self.c);base.save(self.out/'missing-body-comparison.json',accepted)
    def run(self):
        for stage in ('bodies','reviews','summaries_conflicts'):
            print('Compare: '+stage,flush=True);getattr(self,stage)()

def main():
    root=base.ROOT;out=Path((root/'.operation-state/database-phase4/latest-path.txt').read_text());prior=json.loads((out/'comparison-result.json').read_text());assert prior['status']=='core_checks_passed'
    folder=out/'details';folder.mkdir(exist_ok=True);target=root/'data/autoarticle.sqlite';before=fingerprint(target);report=dict(phase=4,status='running',phase5_started=False)
    try:
        with contextlib.closing(db.connect(target,readonly=True)) as c,contextlib.closing(base.ro(Path.home()/'.n8n/database.sqlite')) as legacy:
            c.execute('BEGIN');legacy.execute('BEGIN');compare=Details(c,legacy,root,folder);compare.run();report['status']='detail_checks_passed'
    except Difference as exc:report.update(status='stopped',reason=str(exc),difference_record='details/differences.jsonl')
    finally:
        if 'compare' in locals():compare.diffs.close();report.update(field_checks=dict(compare.stats),last_stage=compare.stage,field_hashes={k:v.hexdigest() for k,v in compare.hashes.items()})
        after=fingerprint(target);report['project_database_unchanged']=before==after;report['checked_at']=base.now();base.save(folder/'comparison-result.json',report);print(json.dumps(report))
if __name__=='__main__':main()
