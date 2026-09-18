"""Narrow DB-first business adapters. Compatibility is emitted after DB commit."""
from contextlib import closing
import json
from pathlib import Path
import re
import project_database as db
import project_record_values as record_values
import project_write_outbox as outbox
import project_readers


def source(path, position, raw):
    """Describe the original record used to build a database row."""
    return {
        "path": path,
        "position": str(position),
        "old_key": raw.get("article_key", raw.get("articleKey")),
        "raw": raw,
        "input_hash": record_values.record_hash(raw),
    }


def provenance(unit, source_record, target_kind, target_id, reason=None):
    """Save an immutable source reference, or verify its existing destination."""
    source_id = record_values.record_id(
        "source",
        source_record["path"],
        source_record["position"],
        source_record["input_hash"],
    )
    existing = unit.c.execute(
        "SELECT * FROM source_records WHERE id=?", (source_id,)
    ).fetchone()
    if existing:
        same_destination = (
            existing["target_kind"] == target_kind
            and existing["target_id"] == target_id
            and existing["raw_json"] == db.canonical(source_record["raw"])
        )
        if not same_destination:
            raise ValueError("Existing source disposition differs")
        return source_id

    unit.save(
        "sourceRecord",
        {
            "id": source_id,
            "migration_run_id": None,
            "source_path": source_record["path"],
            "record_position": source_record["position"],
            "input_hash": source_record["input_hash"],
            "importer_version": outbox.VERSION,
            "target_kind": target_kind,
            "target_id": target_id,
            "state": "held" if reason else "imported",
            "reason": reason,
            "raw_json": db.canonical(source_record["raw"]),
        },
        source_record,
    )
    return source_id


def day_value(value):
    if not isinstance(value,str) or not re.fullmatch(r'20\d\d-\d\d-\d\d',value):raise ValueError('Explicit run date required')
    from datetime import date
    date.fromisoformat(value)
    return value


def route(feature,path=None):
    with closing(db.connect(path,readonly=True)) as c:
        row=c.execute('SELECT write_target FROM cutover_state WHERE feature=?',(feature,)).fetchone()
        if not row:raise ValueError('Missing write route')
        return row[0]


def article_for_key(c,key):
    rows=c.execute("SELECT DISTINCT i.article_id FROM article_identifiers i WHERE i.kind='legacy_key' AND i.value=? AND i.source IN (SELECT DISTINCT source_path FROM source_records WHERE target_kind='articles')",(key,)).fetchall()
    if len(rows)!=1:raise ValueError('Article key does not identify one registered article')
    return rows[0][0]


def build_collection(unit,request):
    records=request['records']
    if not isinstance(records,list) or not records:raise ValueError('Collection records required')
    runs=[r for r in records if r.get('recordType')=='run']
    if len(runs)!=1 or records[0]!=runs[0]:raise ValueError('One leading run record required')
    run=runs[0];day=day_value(run['runDate']);path='content/structured-records/'+day+'.jsonl'
    articles=records[1:]
    if any(r.get('recordType')!='article' or r.get('runDate')!=day for r in articles):raise ValueError('Invalid collection article record')
    if run.get('capturedArticleCount')!=len(articles):raise ValueError('Collection count mismatch')
    if unit.c.execute('SELECT 1 FROM collection_runs WHERE run_date=?',(day,)).fetchone():raise outbox.WriteStopped('collection_day_already_saved_use_original_operation_id')
    stamp=record_values.utc_timestamp(run['generatedAt']);rid=record_values.record_id('collection',path,'line:1')
    s=source(path,'line:1',run)
    unit.save('collection',dict(id=rid,source=path,source_record='line:1',run_date=day,workflow_execution_id=str(run['workflowExecutionId']),search_conditions_json=db.canonical(run),observed_at=stamp),s)
    provenance(unit,s,'collection_runs',rid)
    for i,raw in enumerate(articles,2):
        a=raw['article'];pos='line:'+str(i);s=source(path,pos,raw)
        if not a.get('url'):raise ValueError('Article URL required')
        candidates=unit.c.execute("SELECT a.* FROM articles a WHERE a.url=? AND a.title=? AND a.published_at=? AND a.id IN (SELECT target_id FROM source_records WHERE target_kind='articles')",(a['url'],a.get('title',''),record_values.utc_timestamp(a.get('publishedAt')))).fetchall()
        if len(candidates)==1 and a.get('title') and a.get('publishedAt'):
            aid=candidates[0]['id'];reason=None
        else:
            aid=record_values.record_id('held-article',path,pos);reason='observation_identity_not_proven'
            unit.save('article',dict(id=aid,title=a.get('title',''),url=a['url'],excerpt=a.get('excerpt',''),source=a.get('source',''),published_at=record_values.utc_timestamp(a.get('publishedAt')),created_at=stamp,updated_at=stamp,identity_state='held'),s)
            details={'source_path':path,'source_position':pos,'candidates':[x['id'] for x in candidates]}
            unit.save('conflict',dict(id=record_values.record_id('conflict','identity',aid,details),kind='identity',article_id=aid,reason=reason,status='unresolved',details_json=db.canonical(details),created_at=stamp),s)
        oid=record_values.record_id('occurrence',path,pos)
        unit.save('occurrence',dict(id=oid,collection_run_id=rid,article_id=aid,source_record=pos,title=a.get('title',''),url=a['url'],excerpt=a.get('excerpt',''),published_at=record_values.utc_timestamp(a.get('publishedAt')),observations_json=db.canonical(raw)),s)
        sid=provenance(unit,s,'article_occurrences',oid,reason)
        unit.save('provenance',dict(source_record_id=sid,article_id=aid,source_table=None,source_row_id=pos,old_article_key=s['old_key'],original_url=a['url'],input_hash=s['input_hash'],stored_content_hash=None,fetch_status=None),s)
    # Emit from saved DB rows, preserving the original transport representation.
    stored=[json.loads(unit.c.execute('SELECT search_conditions_json FROM collection_runs WHERE id=?',(rid,)).fetchone()[0])]
    stored.extend(json.loads(x[0]) for x in unit.c.execute("SELECT observations_json FROM article_occurrences WHERE collection_run_id=? ORDER BY CAST(replace(source_record,'line:','') AS INTEGER)",(rid,)))
    if stored!=records:raise outbox.WriteStopped('collection_semantic_comparison_failed')
    unit.file(path,''.join(json.dumps(x,ensure_ascii=False,separators=(',',':'))+'\n' for x in stored))


def submit_collection(operation_id,records,path=None,root=db.ROOT,fault=None):
    path=Path(path or db.database_path())
    if route('n8n-daily',path)!='project-db':raise outbox.WriteStopped('collection_write_route_is_legacy')
    return outbox.commit(operation_id,{'version':outbox.VERSION,'kind':'collection','records':records},path,root,build_collection,fault=fault)


def body_version(unit, article_id, capture_record, source_record, observed_at):
    """Store a reusable body payload and its article-specific observation range."""
    content = record_values.content_values(capture_record)
    if record_values.body_integrity(capture_record).startswith("held_"):
        return None
    if not (content["text"] or content["markdown"] or content["metadata"]):
        return None
    if (
        content["stored_length"] is not None
        and len(content["text"]) != content["stored_length"]
    ):
        raise ValueError("Body length does not match payload")

    payload_fields = {
        "text": content["text"],
        "markdown": content["markdown"],
        "metadata_json": db.canonical(content["metadata"]),
        "non_content_text": content["non_content"],
        "extraction_scope": content["scope"],
    }
    payload_fields["raw_json"] = db.canonical(payload_fields)
    payload_hash = record_values.record_hash(payload_fields)
    payload_id = record_values.record_id("payload", payload_hash)
    unit.save(
        "body",
        {
            "id": payload_id,
            "payload_hash": payload_hash,
            "text_hash": db.checksum(content["text"].encode()),
            **payload_fields,
        },
        source_record,
    )

    version_id = record_values.record_id("content-version", article_id, payload_id)
    existing_version = unit.c.execute(
        "SELECT * FROM article_content_versions WHERE id=?", (version_id,)
    ).fetchone()
    first_observed_at = observed_at
    last_observed_at = observed_at
    if existing_version:
        first_observed_at = min(existing_version["first_observed_at"], observed_at)
        last_observed_at = max(existing_version["last_observed_at"], observed_at)

    unit.save(
        "bodyVersion",
        {
            "id": version_id,
            "article_id": article_id,
            "payload_id": payload_id,
            "first_observed_at": first_observed_at,
            "last_observed_at": last_observed_at,
        },
        source_record,
    )
    return version_id


def file_rows(c,path,kind):
    return [(s['record_position'],json.loads(s['raw_json'])) for s in project_readers.Reader(c).sources(kind,path)]


def merged_position(rows,field,value):
    matches=[pos for pos,row in rows if row.get(field)==value]
    if len(matches)>1:raise ValueError('Ambiguous compatibility record')
    return matches[0] if matches else 'line:'+str(max([int(pos[5:]) for pos,_ in rows if pos.startswith('line:')]+[0])+1)


def emit_jsonl(unit,path,kind):
    rows=file_rows(unit.c,path,kind)
    unit.file(path,''.join(json.dumps(row,ensure_ascii=False)+'\n' for _,row in rows))


def build_capture(unit,request):
    day=day_value(request['day']);raw=request['record'];v=record_values.content_values(raw)
    reader=project_readers.Reader(unit.c);_,articles,captures=reader.load_day(day,[])
    matches=[a for a in articles if a['article']['url']==v['url']]
    if len(matches)!=1:raise ValueError('Capture occurrence is not unique')
    previous=captures.get(v['url']);aid=previous['_project']['articleId'] if previous else matches[0]['_project']['articleId']
    path='content/article-body-captures/'+day+'.jsonl'
    rows=file_rows(unit.c,path,'content_fetch_attempts');pos=merged_position(rows,'originalUrl',v['url']);s=source(path,pos,raw)
    stamp=record_values.utc_timestamp(v['fetched']);integrity=record_values.body_integrity(raw);sid=record_values.record_id('source',path,pos,s['input_hash']);fid=record_values.record_id('fetch',sid)
    vid=body_version(unit,aid,raw,s,stamp)
    unit.save('fetchAttempt',dict(id=fid,article_id=aid,version_id=vid,fetched_at=stamp,status='unverified' if integrity.startswith('held_') else v['status'],completeness=v['completeness'],original_url=v['url'],resolved_url=v['resolved'],failure_reason=v['reason'],extraction_method=v['method'],retry_after=record_values.utc_timestamp(v['retry']),raw_json=db.canonical(raw),body_integrity=integrity,source_status=v['status'],stored_body_hash=v['stored_hash'],stored_body_length=v['stored_length'],source_content_path=v['content_path'],computed_body_hash=db.checksum(v['text'].encode())),s)
    reason=integrity if integrity.startswith('held_') else None
    provenance(unit,s,'content_fetch_attempts',fid,reason)
    unit.save('provenance',dict(source_record_id=sid,article_id=aid,source_table=None,source_row_id=pos,old_article_key=v['key'],original_url=v['url'],input_hash=s['input_hash'],stored_content_hash=v['stored_hash'],fetch_status=v['status']),s)
    if reason:
        details=dict(source_record_id=sid,source_path=path,source_position=pos,stored_body_hash=v['stored_hash'],stored_body_length=v['stored_length'],computed_body_hash=db.checksum(v['text'].encode()),source_status=v['status'],body_empty=not bool(v['text']))
        unit.save('conflict',dict(id=record_values.record_id('new-body-integrity',sid),kind='body_integrity',article_id=aid,reason=reason,status='unresolved',details_json=db.canonical(details),created_at=stamp),s)
    emit_jsonl(unit,path,'content_fetch_attempts')
    if 'cacheEntry' in request:
        entry=request['cacheEntry'];cache_path='content/article-body-captures/backfill-state.json'
        cv=record_values.content_values(entry)
        if cv['text']!=v['text'] or cv['url']!=v['url'] or cv['status']!=v['status'] or cv['stored_length']!=v['stored_length']:
            raise ValueError('Cache entry does not match captured body')
        cache_source=source(cache_path,'entries/'+v['url'],entry)
        cache_id=record_values.record_id('source',cache_path,cache_source['position'],cache_source['input_hash'])
        runtime_id=record_values.record_id('runtime',cache_id)
        provenance(unit,cache_source,'content_runtime_state',runtime_id,reason)
        unit.save('runtime',dict(id=runtime_id,source_record_id=cache_id,state_key='entries/'+v['url'],state_json=db.canonical(entry)),cache_source)
        resolved={'originalUrl':v['url'],'value':v['resolved']}
        rs=source(cache_path,'resolvedUrls/'+v['url'],resolved);rsid=record_values.record_id('source',cache_path,rs['position'],rs['input_hash']);rtid=record_values.record_id('runtime',rsid)
        provenance(unit,rs,'content_runtime_state',rtid)
        unit.save('runtime',dict(id=rtid,source_record_id=rsid,state_key=v['url'],state_json=db.canonical(v['resolved'])),rs)
        watermark=unit.c.execute('SELECT max(rowid) FROM source_records').fetchone()[0]
        unit.file_reference(cache_path,dict(format='capture-cache-v1',sourceWatermark=watermark,generatedAt=stamp))
        # The optional legacy Data Table receives the same saved claim after DB commit.
        import project_compatibility as compat
        data={n:entry.get(m,'') for n,m in [('article_key','article_key'),('original_url','original_url'),('resolved_url','resolved_url'),('source_domain','source_host'),('content_type','content_type'),('content_status','status'),('content_text','content_text'),('content_hash','content_hash'),('extraction_method','extraction_method'),('failure_reason','reason'),('content_path','content_path'),('fetched_at','processed_at')]}
        data['content_length']=entry['body_length']
        if request.get('syncContents',True):compat.enqueue(unit,'article_contents',data)


def _save_review_evidence_and_facts(unit, review_id, review, source_record):
    """Persist evidence, facts, and entity links in dependency order."""
    evidence_ids = {}
    fact_ids = {}
    for evidence in review['evidence']:
        evidence_id = record_values.record_id('evidence', review_id, evidence['id'])
        evidence_ids[evidence['id']] = evidence_id
        unit.save('evidence', dict(
            id=evidence_id, review_id=review_id, input_field=evidence['field'],
            start_offset=evidence['start'], end_offset=evidence['end'], quote=evidence['quote'],
        ), source_record)

    for fact in review['facts']:
        fact_id = record_values.record_id('fact', review_id, fact['id'])
        fact_ids[fact['id']] = fact_id
        unit.save('fact', dict(
            id=fact_id, review_id=review_id, fact=fact['text'], raw_json=db.canonical(fact),
        ), source_record)
        for evidence_reference in set(fact['evidenceIds']):
            unit.save('factEvidence', dict(
                fact_id=fact_id, evidence_id=evidence_ids[evidence_reference],
            ), source_record)

    for index, entity in enumerate(review['entities']):
        entity_id = record_values.record_id('entity', review_id, index)
        unit.save('entity', dict(
            id=entity_id, review_id=review_id, name=entity['name'], kind=entity['kind'],
            raw_json=db.canonical(entity),
        ), source_record)
        for fact_reference in set(entity['factIds']):
            unit.save('entityFact', dict(
                entity_id=entity_id, fact_id=fact_ids[fact_reference],
            ), source_record)


def build_reviews(unit, request):
    import article_review_facts as rules
    from ai_input_minimization import expand_review, record_legacy_hold_assessments

    day = day_value(request['day'])
    source_path = rules.DIRECTORY + '/' + day + '.jsonl'
    reader = project_readers.Reader(unit.c)
    _, articles, captures = reader.load_day(day, [])
    policy_hash = rules.policy_hash(unit.root)
    if not policy_hash:
        raise ValueError('Review rules missing')

    for authored in request['records']:
        expanded = expand_review(unit.c, day, authored)
        review = dict(expanded, sourceDate=day, reviewedAt=expanded.get('reviewedAt') or db.now())
        matching_articles = [
            occurrence for occurrence in articles
            if occurrence['article']['url'] == review['url']
            and rules.input_hash(occurrence['article'], captures.get(review['url'])) == review['inputHash']
        ]
        if len(matching_articles) != 1:
            raise ValueError('Review input is not unique')
        occurrence = matching_articles[0]
        capture = captures.get(review['url'])
        rules.validate_record(review, occurrence['article'], capture, policy_hash)

        article_id = occurrence['_project']['articleId']
        reviewed_at = record_values.utc_timestamp(review['reviewedAt'])
        position = merged_position(file_rows(unit.c, source_path, 'review_records'), 'url', review['url'])
        source_record = source(source_path, position, review)
        source_id = record_values.record_id('source', source_path, position, source_record['input_hash'])
        review_id = record_values.record_id('review', source_id)
        content_version_id = (
            body_version(unit, article_id, capture, source_record, reviewed_at) if capture else None
        )
        unit.save('review', dict(
            id=review_id, article_id=article_id, content_version_id=content_version_id,
            input_hash=review['inputHash'], rule_hash=review['policyHash'],
            basis=review['basis'], reviewer=review['reviewedBy'], reviewed_at=reviewed_at,
            status='current', raw_json=db.canonical(review),
        ), source_record)
        for task, state in review['taskStatus'].items():
            unit.save('taskStatus', dict(review_id=review_id, task=task, status=state), source_record)
        capture_snapshot = (
            {field: value for field, value in capture.items() if field != '_project'} if capture else None
        )
        unit.save('reviewInput', dict(
            review_id=review_id, input_hash=review['inputHash'],
            article_json=db.canonical(occurrence['article']),
            capture_json=db.canonical(capture_snapshot),
        ), source_record)
        _save_review_evidence_and_facts(unit, review_id, review, source_record)
        provenance(unit, source_record, 'review_records', review_id)
        record_legacy_hold_assessments(unit, article_id)
    emit_jsonl(unit, source_path, 'review_records')


BUILDERS={'collection':build_collection,'capture':build_capture,'reviews':build_reviews}


def submit(operation_id,kind,payload,path=None,root=db.ROOT,fault=None):
    feature='n8n-daily' if kind=='collection' else ('dashboard' if kind in ('talent','classification','feedback') else 'ai-reader')
    path=Path(path or db.database_path())
    if route(feature,path)!='project-db':raise outbox.WriteStopped('business_write_route_is_legacy')
    request=dict(payload,version=outbox.VERSION,kind=kind)
    import project_compatibility
    result=outbox.commit(operation_id,request,path,root,BUILDERS[kind],deliver=project_compatibility.deliver,fault=fault)
    if kind in ('talent','classification'):
        fields=('articles','talents','articleTalents') if kind=='talent' else ('classifications',)
        result=dict(result,accepted=True,proposalDate=payload['proposal']['proposalDate'],counts={k:len(payload['proposal'][k]) for k in fields})
    elif kind=='feedback':
        f=payload['feedback'];result=dict(result,accepted=True,articleKey=f['article_key'],decision='rejected' if f['is_rejected'] else 'approved',reasonCode=f['reason_code'],reviewedAt=f['reviewed_at'])
    return result


def build_summary(unit,request):
    from ai_input_minimization import expand_artifact, record_artifact
    request=expand_artifact(unit,request)
    from article_artifact_formats import parsed_summaries
    day=day_value(request['day']);path='content/article-summaries/'+day+'.md'
    reader=project_readers.Reader(unit.c);_,articles,captures=reader.load_day(day,[])
    parsed=list(parsed_summaries(request['text'],day))
    if not parsed:raise ValueError('No summaries in authored document')
    stamp=record_values.utc_timestamp(day+'T00:00:00+09:00')
    for i,(links,summary) in enumerate(parsed):
        for j,(title,url) in enumerate(links):
            matches=[a for a in articles if a['article']['url']==url and a['article']['title']==title]
            if len(matches)!=1:raise ValueError('Summary occurrence is not unique')
            aid=matches[0]['_project']['articleId']
            from ai_input_minimization import valid_review
            prior=unit.c.execute('SELECT * FROM article_summaries WHERE article_id=? AND source=? AND is_current=1',(aid,path)).fetchone()
            if prior and prior['summary']==summary['text'] and valid_review(unit.c,prior['review_id'],aid,'article-summary'):
                continue
            import article_review_facts as rules
            input_hash=rules.input_hash(matches[0]['article'],captures.get(url))
            reviews=unit.c.execute("SELECT r.* FROM review_records r JOIN review_task_statuses t ON t.review_id=r.id WHERE r.article_id=? AND r.input_hash=? AND r.rule_hash=? AND r.status='current' AND t.task='article-summary' AND t.status='ready' ORDER BY r.reviewed_at DESC,r.id",(aid,input_hash,rules.policy_hash(unit.root))).fetchall()
            if not reviews:raise ValueError('Summary requires an explicit current ready review')
            pos='summary:'+str(i)+':link:'+str(j);raw=dict(summary,links=links,url=url);s=source(path,pos,raw)
            sid=record_values.record_id('source',path,pos,s['input_hash']);iid=record_values.record_id('summary',sid)
            previous=unit.c.execute('SELECT * FROM article_summaries WHERE id=?',(iid,)).fetchone()
            if not previous:
                current=unit.c.execute('SELECT * FROM article_summaries WHERE article_id=? AND is_current=1',(aid,)).fetchall()
                for old in current:unit.save('summary',dict(old,is_current=0),s)
                version=unit.c.execute('SELECT coalesce(max(version),0)+1 FROM article_summaries WHERE article_id=?',(aid,)).fetchone()[0]
                unit.save('summary',dict(id=iid,article_id=aid,review_id=reviews[0]['id'],summary=summary['text'],saved_at=stamp,version=version,is_current=1,source=path,raw_json=db.canonical(raw)),s)
            provenance(unit,s,'article_summaries',iid)
    # Authored non-business Markdown is also retained in the immutable request.
    record_artifact(unit,request)
    unit.file(path,request['text'])


def build_proposal(unit,request):
    from ai_input_minimization import expand_artifact, record_artifact
    request=expand_artifact(unit,request)
    from ai_input_minimization import validate_proposal
    validate_proposal(unit,request)
    day=day_value(request['day']);directory=request['directory']
    groups={'talent-index-proposals':('articles','talents','articleTalents'),'article-classification-proposals':('classifications',),'official-talent-registry':('groups','talents'),'official-talent-registry/proposals':('articles','talents','articleTalents'),'article-feedback-instructions':('feedback',)}
    if directory not in groups:raise ValueError('Unsupported proposal document')
    value=request['document'];path='content/'+directory+'/'+day+'.json'
    fields=groups[directory]
    rows=[('header',{k:v for k,v in value.items() if k not in fields},directory+'-header')]
    for key in fields:
        for index,raw in enumerate(value.get(key,[])):rows.append((key+'/'+str(index),raw,directory+'/'+key))
    for pos,raw,kind in rows:
        s=source(path,pos,raw);sid=record_values.record_id('source',path,pos,s['input_hash']);hid=record_values.record_id('history',sid)
        # Source references and immutable document content are in the same commit.
        provenance(unit,s,'legacy_history_records',hid)
        unit.save('history',dict(id=hid,source_record_id=sid,kind=kind,article_id=None,talent_id=None,raw_json=db.canonical(raw)),s)
    record_artifact(unit,request)
    unit.file(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    if 'markdown' in request:unit.file('content/'+directory+'/'+day+'.md',request['markdown'])

BUILDERS.update(summary=build_summary,proposal=build_proposal)


def native_source(name,raw,key):
    return source('n8n:project-writes:'+name,raw[key],raw)


def native_article(unit,raw):
    key=raw['article_key'];s=native_source('articles',raw,'article_key')
    matches=unit.c.execute("SELECT DISTINCT i.article_id FROM article_identifiers i WHERE i.kind='legacy_key' AND i.value=? AND i.source IN (SELECT DISTINCT source_path FROM source_records WHERE target_kind='articles')",(key,)).fetchall()
    if len(matches)>1:raise ValueError('Article key ambiguous')
    aid=matches[0][0] if matches else record_values.record_id('n8n-article',key)
    if not matches:
        candidates=unit.c.execute('SELECT id FROM articles WHERE url=? AND title=? AND published_at=?',(raw['url'],raw['title'],record_values.utc_timestamp(raw.get('published_at')))).fetchall()
        if len(candidates)==1:aid=candidates[0]['id']
        elif len(candidates)>1:raise ValueError('Proposed article identity is ambiguous')
    old=unit.c.execute('SELECT * FROM articles WHERE id=?',(aid,)).fetchone();stamp=record_values.utc_timestamp(raw['last_seen_at'])
    unit.save('article',dict(id=aid,title=raw['title'],url=raw['url'],excerpt=raw.get('excerpt',''),source=raw.get('source',''),published_at=record_values.utc_timestamp(raw.get('published_at')),created_at=old['created_at'] if old else stamp,updated_at=stamp,identity_state=old['identity_state'] if old else 'identified'),s)
    unit.save('identifier',dict(id=record_values.record_id('identifier',aid,s['path'],'legacy_key',key),article_id=aid,source=s['path'],kind='legacy_key',value=key,match_state='exact'),s)
    unit.save('identifier',dict(id=record_values.record_id('identifier',aid,s['path'],'original_url',raw['url']),article_id=aid,source=s['path'],kind='original_url',value=raw['url'],match_state='exact'),s)
    provenance(unit,s,'articles',aid)
    return aid


def native_version(unit,entity,table,aid,raw,fields):
    s=native_source(table,raw,'article_key');iid=record_values.record_id('project-version',table,aid,s['input_hash'])
    existing=unit.c.execute('SELECT * FROM '+table+' WHERE id=?',(iid,)).fetchone()
    if not existing:
        for old in unit.c.execute('SELECT * FROM '+table+' WHERE article_id=? AND is_current=1',(aid,)).fetchall():unit.save(entity,dict(old,is_current=0),s)
        version=unit.c.execute('SELECT coalesce(max(version),0)+1 FROM '+table+' WHERE article_id=?',(aid,)).fetchone()[0]
        unit.save(entity,dict(id=iid,article_id=aid,version=version,is_current=1,raw_json=db.canonical(raw),**fields),s)
    provenance(unit,s,table,iid)
    return iid,s


def build_native(unit,request):
    import project_compatibility as compat
    kind=request['kind']
    if kind=='feedback':
        raw=request['feedback'];aid=article_for_key(unit.c,raw['article_key'])
        if raw.get('review_source') not in ('talent-dashboard','article-review-markdown'):raise ValueError('Explicit review tool source required')
        if raw['reason_code'] not in ('approved','suspicious_source','irrelevant','unavailable','outdated'):raise ValueError('Unknown feedback reason')
        if bool(raw['is_rejected'])==(raw['reason_code']=='approved'):raise ValueError('Feedback decision/reason mismatch')
        native_version(unit,'feedback','article_feedback',aid,raw,dict(is_rejected=int(raw['is_rejected']),reason_code=raw['reason_code'],reviewed_at=record_values.utc_timestamp(raw['reviewed_at']),source=raw['review_source']))
        compat.enqueue(unit,'article_feedback',raw)
        return
    proposal=request['proposal']
    if proposal.get('proposalVersion')!=1:raise ValueError('Unsupported proposal version')
    day_value(proposal['proposalDate'])
    import autoarticle_apply
    reader=project_readers.Reader(unit.c)
    current={'articles':reader.table('articles'),'talents':reader.table('talents')}
    groups=autoarticle_apply.preflight(kind,proposal,current)
    normalized={name:rows for name,key,rows in groups}
    proposal=dict(proposal)
    if kind=='talent':
        proposal.update(articles=normalized['articles'],talents=normalized['talents'],articleTalents=normalized['article_talents'])
    else:proposal['classifications']=normalized['article_classifications']
    if kind=='talent':
        for raw in proposal['articles']:
            native_article(unit,raw);compat.enqueue(unit,'articles',raw)
        for raw in proposal['talents']:
            tid=record_values.record_id('talent',raw['talent_id']);s=native_source('talents',raw,'talent_id')
            old=unit.c.execute('SELECT * FROM talents WHERE id=?',(tid,)).fetchone()
            if raw['status']!=(old['status'] if old else 'pending') or bool(raw['search_enabled'])!=bool(old['search_enabled'] if old else 0):raise ValueError('Talent approval/search change requires separate explicit review')
            unit.save('talent',dict(id=tid,display_name=raw['display_name'],organization=raw['organization'],status=raw['status'],search_enabled=int(raw['search_enabled']),auto_discovered=int(raw['auto_discovered']),last_seen_at=record_values.utc_timestamp(raw['last_seen_at']),raw_json=db.canonical(raw)),s)
            aliases=set(record_values.json_array(raw['aliases_json']));old_aliases={x[0] for x in unit.c.execute('SELECT alias FROM talent_aliases WHERE talent_id=?',(tid,))}
            if old_aliases-aliases:raise ValueError('Alias removal is outside this operation')
            for alias in sorted(aliases):unit.save('alias',dict(talent_id=tid,alias=alias),s)
            provenance(unit,s,'talents',tid);compat.enqueue(unit,'talents',raw)
        for raw in proposal['articleTalents']:
            aid=article_for_key(unit.c,raw['article_key']);tid=record_values.record_id('talent',raw['talent_id']);s=native_source('article_talents',raw,'relation_key')
            iid=record_values.record_id('relationship',raw['relation_key']);old=unit.c.execute('SELECT * FROM article_talents WHERE id=?',(iid,)).fetchone()
            # Reviewed application preserves an existing hold and never promotes it.
            state=old['state'] if old else 'proposed'
            unit.save('relationship',dict(id=iid,article_id=aid,talent_id=tid,review_id=None,evidence=raw['evidence_text'],confidence=raw['confidence'],state=state,raw_json=db.canonical(raw)),s)
            provenance(unit,s,'article_talents',iid,'proposed_relationship_requires_review' if state in ('proposed','held') else None)
            compat.enqueue(unit,'article_talents',raw)
    elif kind=='classification':
        for raw in proposal['classifications']:
            aid=article_for_key(unit.c,raw['article_key'])
            iid,s=native_version(unit,'classification','article_classifications',aid,raw,dict(review_id=None,article_type=raw['article_type'],primary_category=raw['primary_category'],relevance=raw['relevance'],confidence=raw['confidence'],evidence=raw['evidence_text'],classified_at=record_values.utc_timestamp(raw['classified_at'])))
            for category in set(record_values.json_array(raw['secondary_categories_json'])):unit.save('secondaryCategory',dict(classification_id=iid,category=category),s)
            compat.enqueue(unit,'article_classifications',raw)
    else:raise ValueError('Unsupported native business operation')

BUILDERS.update(talent=build_native,classification=build_native,feedback=build_native)


def render_compatibility_file(reference,database):
    if reference.get('format')!='capture-cache-v1':raise ValueError('Unsupported file projection')
    path='content/article-body-captures/backfill-state.json';limit=reference['sourceWatermark']
    with closing(db.connect(database,readonly=True)) as c:
        rows=c.execute("""SELECT s.* FROM source_records s WHERE source_path=? AND rowid<=?
          AND rowid=(SELECT max(v.rowid) FROM source_records v WHERE v.source_path=s.source_path AND v.record_position=s.record_position AND v.rowid<=?)""",(path,limit,limit)).fetchall()
        value={'schemaVersion':2,'generatedAt':reference['generatedAt'],'entries':{},'resolvedUrls':{}}
        for s in rows:
            pos=s['record_position'];raw=json.loads(s['raw_json'])
            if pos.startswith('entries/'):value['entries'][pos[len('entries/'):]]=raw
            elif pos.startswith('resolvedUrls/'):value['resolvedUrls'][pos[len('resolvedUrls/'):]]=raw['value']
        return json.dumps(value,ensure_ascii=False,indent=2)+'\n'


def build_runtime(unit,request):
    name=request['name']
    if name!='rate-limit-state.json':raise ValueError('Unsupported runtime state')
    path='content/article-body-captures/'+name;raw=request['value'];s=source(path,'$',raw)
    sid=record_values.record_id('source',path,'$',s['input_hash']);iid=record_values.record_id('runtime',sid)
    provenance(unit,s,'content_runtime_state',iid)
    unit.save('runtime',dict(id=iid,source_record_id=sid,state_key=name,state_json=db.canonical(raw)),s)
    unit.file(path,json.dumps(raw,ensure_ascii=False,indent=2)+'\n')

BUILDERS['runtime']=build_runtime


def build_documents(unit,request):
    for item in request['documents']:build_proposal(unit,item)

BUILDERS['documents']=build_documents

from ai_input_minimization import build_references
BUILDERS['ai-references']=build_references
