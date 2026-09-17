"""Stage selection and source-bound compact inputs over the existing DB adapters.

References/completion receipts use immutable history and the normal DB transaction.
They never copy body payloads, infer approval from ready, or fetch external pages.
"""
import json
from collections import Counter
import article_review_facts as shared
import project_database as db
import project_record_values as record_values
import project_readers as project

TASKS=shared.TASKS
STEP_TASK={'summary':'article-summary','talent-review':'talent-index','classification-review':'article-classification'}
UNAVAILABLE={'unavailable','metadata_only'}
def history(c,kind,article_id):
    return [json.loads(r[0]) for r in c.execute(
        "SELECT raw_json FROM legacy_history_records WHERE kind=? AND article_id=? ORDER BY rowid",
        (kind,article_id))]
def save_history(unit,kind,value,article_id=None,key=None):
    import project_business_writes as business
    key=key or shared.digest(value)
    s=business.source('ai-input/'+kind,key,value)
    sid=record_values.record_id('source',s['path'],s['position'],s['input_hash'])
    hid=record_values.record_id(kind,key)
    business.provenance(unit,s,'legacy_history_records',hid)
    unit.save('history',dict(id=hid,source_record_id=sid,kind=kind,article_id=article_id,
                            talent_id=None,raw_json=db.canonical(value)),s)
def reference_id(mapping):
    return 'a'+shared.digest(mapping)[:16]
def build_references(unit,request):
    for mapping in request['references']:
        ref=reference_id(mapping)
        save_history(unit,'ai-reference',mapping,mapping['articleId'],ref)
    for value in request.get('holdAssessments', []):
        save_hold_assessment(unit,value)
def resolve(c,ref,day,task=None):
    row=c.execute("SELECT raw_json FROM legacy_history_records WHERE id=? AND kind='ai-reference'",
                  (record_values.record_id('ai-reference',ref),)).fetchone()
    if not row:raise ValueError('Unknown article reference')
    mapping=json.loads(row[0])
    if reference_id(mapping)!=ref or mapping['day']!=day:raise ValueError('Reference belongs to another input/day')
    if task and mapping['task']!=task:raise ValueError('Reference belongs to another task')
    occurrence=c.execute('SELECT * FROM article_occurrences WHERE id=? AND article_id=?',
                         (mapping['occurrenceId'],mapping['articleId'])).fetchone()
    if not occurrence:raise ValueError('Reference occurrence missing')
    article=json.loads(occurrence['observations_json'])['article']
    capture=None
    if mapping['fetchAttemptId']:
        fetch=c.execute('SELECT * FROM content_fetch_attempts WHERE id=? AND article_id=?',
                        (mapping['fetchAttemptId'],mapping['articleId'])).fetchone()
        if not fetch or fetch['version_id']!=mapping['contentVersionId']:raise ValueError('Reference body mismatch')
        capture=project.Reader(c).capture(fetch)
    review=None
    if mapping['reviewId']:
        review=c.execute('SELECT * FROM review_records WHERE id=? AND article_id=?',
                         (mapping['reviewId'],mapping['articleId'])).fetchone()
        if not review or review['content_version_id']!=mapping['contentVersionId']:raise ValueError('Reference review/body mismatch')
        review=json.loads(review['raw_json'])
        shared.validate_record(review,article,capture,review['policyHash'])
    return mapping,article,capture,review
def valid_review(c, rid, aid, task):
    """Validate saved work against its historical inputs and policy."""
    review_row = c.execute(
        'SELECT * FROM review_records WHERE id=? AND article_id=?', (rid, aid),
    ).fetchone()
    if not review_row:
        return False
    snapshot = c.execute(
        'SELECT * FROM review_input_snapshots WHERE review_id=?', (rid,),
    ).fetchone()
    if not snapshot:
        return False
    review_record = json.loads(review_row['raw_json'])
    try:
        shared.validate_record(
            review_record,
            json.loads(snapshot['article_json']),
            json.loads(snapshot['capture_json']),
            review_row['rule_hash'],
        )
    except (ValueError, TypeError, KeyError):
        return False
    return review_record['taskStatus'][task] == 'ready'


def _completed_artifact(connection, article_id, task):
    """Find saved business records backed by their own historical review."""
    if task == 'article-summary':
        summaries = connection.execute(
            'SELECT id,review_id FROM article_summaries WHERE article_id=? ORDER BY saved_at DESC',
            (article_id,),
        )
        for summary in summaries:
            if valid_review(connection, summary['review_id'], article_id, task):
                return dict(
                    task=task, reviewId=summary['review_id'],
                    artifactId=summary['id'], state='saved',
                )
        return None

    table = 'article_talents' if task == 'talent-index' else 'article_classifications'
    artifacts = connection.execute(
        'SELECT id,review_id,raw_json FROM ' + table + ' WHERE article_id=?',
        (article_id,),
    )
    for artifact in artifacts:
        record = json.loads(artifact['raw_json'])
        method = record.get('detection_method', record.get('classification_method'))
        if method == 'ai_review' and valid_review(connection, artifact['review_id'], article_id, task):
            return dict(
                task=task, reviewId=artifact['review_id'],
                artifactId=artifact['id'], state='saved',
            )
    return None


def _proposal_matches_article(connection, proposal, source_path, article_id, legacy_keys, task):
    """Match a legacy key or an unambiguous URL within the proposal's day."""
    if proposal.get('article_key') in legacy_keys:
        return True

    day = source_path.rsplit('/', 1)[-1][:-5]
    source_url = proposal.get('article_url')
    if task == 'talent-index' and proposal.get('article_key'):
        documents = project.Reader(connection).documents('talent-index-proposals', day)
        matching_articles = [
            article
            for path, document in documents if path == source_path
            for article in document.get('articles', [])
            if article.get('article_key') == proposal['article_key']
        ]
        if len(matching_articles) == 1:
            source_url = matching_articles[0].get('url')
    if not source_url:
        return False

    matching_ids = {
        row[0] for row in connection.execute(
            'SELECT o.article_id FROM article_occurrences o '
            'JOIN collection_runs r ON r.id=o.collection_run_id '
            'WHERE r.run_date=? AND o.url=?',
            (day, source_url),
        )
    }
    return matching_ids == {article_id}


def _completed_proposal(connection, article_id, task):
    """Require an explicit AI proposal and matching, grounded review evidence."""
    legacy_keys = {
        row[0] for row in connection.execute(
            "SELECT value FROM article_identifiers WHERE article_id=? AND kind='legacy_key'",
            (article_id,),
        )
    }
    proposal_kind = (
        'talent-index-proposals/articleTalents' if task == 'talent-index'
        else 'article-classification-proposals/classifications'
    )
    proposals = connection.execute(
        'SELECT h.id,h.raw_json,s.source_path FROM legacy_history_records h '
        'JOIN source_records s ON s.id=h.source_record_id WHERE h.kind=? ORDER BY h.rowid DESC',
        (proposal_kind,),
    )
    for proposal_row in proposals:
        proposal = json.loads(proposal_row['raw_json'])
        if not _proposal_matches_article(
            connection, proposal, proposal_row['source_path'], article_id, legacy_keys, task,
        ):
            continue
        method = proposal.get('detection_method', proposal.get('classification_method'))
        if method != 'ai_review':
            continue
        evidence_text = proposal.get('evidence_text', '').strip()
        if not evidence_text:
            continue

        reviews = connection.execute(
            'SELECT id,raw_json FROM review_records WHERE article_id=? ORDER BY rowid DESC',
            (article_id,),
        )
        for review_row in reviews:
            review = json.loads(review_row['raw_json'])
            grounded_texts = (
                [evidence['quote'] for evidence in review['evidence']]
                + [fact['text'] for fact in review['facts']]
            )
            if valid_review(connection, review_row['id'], article_id, task) and any(
                evidence_text in text or text in evidence_text for text in grounded_texts if text
            ):
                return dict(
                    task=task, reviewId=review_row['id'],
                    artifactId=proposal_row['id'], state='saved',
                )
    return None


def completed(c, aid, task):
    """Prefer completion receipts, then saved artifacts, then legacy proposals."""
    receipts = history(c, 'ai-stage-completion', aid)
    for receipt in reversed(receipts):
        if receipt['task'] == task and valid_review(c, receipt['reviewId'], aid, task):
            return receipt

    saved_artifact = _completed_artifact(c, aid, task)
    if saved_artifact:
        return saved_artifact
    return _completed_proposal(c, aid, task)


def task_facts(record,task):
    explicit=record.get('taskFacts',{}).get(task)
    if explicit is None:
        if task=='talent-index':
            explicit={fid for e in record['entities'] for fid in e['factIds']}
            # Without entities, retain facts so a negative finding has evidence.
            if not explicit:explicit={f['id'] for f in record['facts']}
        else:explicit={f['id'] for f in record['facts']}
    return [f for f in record['facts'] if f['id'] in explicit]
def packet(record,task):
    facts=task_facts(record,task)
    ids={f['id'] for f in facts}
    evidence={eid for f in facts for eid in f['evidenceIds']}
    result=dict(facts=[{k:v for k,v in f.items() if k!='topics'} for f in facts],
                evidence=[e for e in record['evidence'] if e['id'] in evidence])
    entities=[e for e in record['entities'] if ids.intersection(e['factIds'])]
    if entities:result['entities']=[dict(e,factIds=[f for f in e['factIds'] if f in ids]) for e in entities]
    return result
def material(record, task, topics):
    """Identify task facts by their text and quotes for the missing topics."""
    evidence_by_id = {item['id']: item for item in record['evidence']}
    requested_topics = set(topics)
    material_hashes = set()
    for fact in task_facts(record, task):
        if not requested_topics.intersection(fact.get('topics', [])):
            continue
        quotes = sorted(evidence_by_id[evidence_id]['quote'] for evidence_id in fact['evidenceIds'])
        material_hashes.add(shared.digest(dict(text=fact['text'], quotes=quotes)))
    return material_hashes


def hold_state(c, aid, task, current, audit=None):
    """Reopen the latest hold only when its missing topics gain material."""
    records = [
        json.loads(row[0])
        for row in c.execute(
            'SELECT raw_json FROM review_records WHERE article_id=? ORDER BY rowid',
            (aid,),
        )
    ]
    held_records = [record for record in records if record['taskStatus'].get(task) == 'held']
    if not held_records:
        return None

    previous_hold = held_records[-1]
    hold_details = previous_hold.get('holds', {}).get(task, {})
    missing_topics = hold_details.get('missingTopics', [])
    if not missing_topics:
        import legacy_hold_relevance as legacy_hold

        held_row, current_row = legacy_hold.held_rows(c, aid, task)
        result, assessment = legacy_hold.assess(c, aid, task, held_row, current_row)
        if audit is not None:
            audit.append(assessment)
        return result

    new_material = set()
    if current:
        current_material = material(current, task, missing_topics)
        previous_material = material(previous_hold, task, missing_topics)
        new_material = current_material - previous_material
    if new_material:
        return dict(
            state='resumed',
            reason=hold_details.get('reason'),
            newMaterialCount=len(new_material),
        )
    reason = hold_details.get('reason') or '; '.join(previous_hold['unresolved'])
    return dict(state='held', reason=reason)


def select(c, day, row, capture, task, policy, audit=None):
    """Apply saved, held, unavailable, and review checks in that order."""
    article_id = row['_project']['articleId']
    completion_receipt = completed(c, article_id, task)
    if completion_receipt:
        return dict(state='saved', receipt=completion_receipt)

    if capture and capture['_project']['articleId'] != article_id:
        raise ValueError('Article/capture identity mismatch')
    latest_review = c.execute(
        'SELECT * FROM review_records WHERE article_id=? ORDER BY rowid DESC LIMIT 1',
        (article_id,),
    ).fetchone()
    review_record = json.loads(latest_review['raw_json']) if latest_review else None
    hold = hold_state(c, article_id, task, review_record, audit)
    if hold and hold['state'] == 'held':
        return hold

    content_status = (capture or {}).get('contentStatus')
    if content_status in UNAVAILABLE and task in ('article-summary', 'article-classification'):
        return dict(state='unavailable', reason=(capture or {}).get('failureReason'))

    review_is_valid = False
    if review_record and latest_review['status'] == 'current':
        try:
            shared.validate_record(review_record, row['article'], capture, policy)
            review_is_valid = True
        except (ValueError, TypeError, KeyError):
            pass

    task_is_ready = review_is_valid and review_record['taskStatus'][task] == 'ready'
    result = dict(
        state='ready' if task_is_ready else 'needs_review',
        record=review_record if review_is_valid else None,
        reviewId=latest_review['id'] if review_is_valid else None,
    )
    if hold:
        result['resume'] = hold
    return result


def _select_input_candidates(reader, day, rows, captures, task, policy_hash, article_url, audit):
    """Select before pagination so totals include every eligible article."""
    candidates = []
    excluded = Counter()
    exclusion_reasons = Counter()
    for row in rows:
        if article_url and row['article']['url'] != article_url:
            continue
        capture = captures.get(row['article']['url'])
        if capture is None:
            latest_fetch = reader.c.execute(
                'SELECT * FROM content_fetch_attempts WHERE article_id=? ORDER BY rowid DESC LIMIT 1',
                (row['_project']['articleId'],),
            ).fetchone()
            if latest_fetch:
                capture = reader.capture(latest_fetch)

        selection = select(reader.c, day, row, capture, task, policy_hash, audit)
        state = selection['state']
        if state in ('saved', 'held', 'unavailable'):
            excluded[state] += 1
            if selection.get('reason'):
                reason = str(selection['reason'])[:300]
                exclusion_reasons[(state, reason)] += 1
            continue
        candidates.append((row, capture, selection))
    return candidates, excluded, exclusion_reasons


def _build_input_article(day, task, row, capture, selection, policy_hash,
                         content_offset, max_content_chars, include_body):
    """Bind the displayed facts and body chunk to the same source reference."""
    from read_ai_inputs import content_chunk

    capture_references = (capture or {}).get('_project', {})
    mapping = dict(
        day=day,
        task=task,
        articleId=row['_project']['articleId'],
        occurrenceId=row['_project']['occurrenceId'],
        fetchAttemptId=capture_references.get('fetchAttemptId'),
        contentVersionId=capture_references.get('contentVersionId'),
        reviewId=selection['reviewId'],
        policyHash=policy_hash,
    )
    article = row['article']
    view = dict(
        ref=reference_id(mapping),
        title=article.get('title', ''),
        publishedAt=article.get('publishedAt', ''),
        contentStatus=(capture or {}).get('contentStatus', 'not_captured'),
        state=selection['state'],
    )
    if selection.get('resume'):
        view['resume'] = selection['resume']
    if selection['record']:
        view.update(packet(selection['record'], task))
    if selection['state'] == 'needs_review' or include_body or content_offset:
        if capture:
            view['content'] = content_chunk(capture, content_offset, max_content_chars)
        if article.get('excerpt'):
            view['excerpt'] = article['excerpt']
    return view, mapping


def _save_input_references(root, mappings, audit):
    """Persist references and hold assessments through the normal transaction."""
    if not mappings and not audit:
        return
    import project_business_writes as business

    payload = dict(references=mappings)
    if audit:
        payload['holdAssessments'] = audit
    business.submit(
        'ai-refs-' + shared.digest(payload),
        'ai-references',
        payload,
        project.path_for(root),
        root,
    )


def build_payload(root, day, task, offset=0, limit=20, article_url=None,
                  content_offset=0, max_content_chars=6000, include_body=False):
    from read_ai_inputs import parse_day

    parse_day(day)
    if offset < 0 or limit < 1 or content_offset < 0 or max_content_chars < 1:
        raise ValueError('Invalid page bounds')

    views = []
    mappings = []
    audit = []
    with project.reader(root) as reader:
        requested_urls = {article_url} if article_url else None
        _, rows, captures = reader.load_day(day, [], capture_urls=requested_urls)
        policy_hash = shared.policy_hash(root)
        candidates, excluded, exclusion_reasons = _select_input_candidates(
            reader, day, rows, captures, task, policy_hash, article_url, audit,
        )
        if offset > len(candidates):
            raise ValueError('Offset exceeds eligible article count')
        page = candidates[offset:offset + limit]
        if content_offset and (not article_url or len(page) != 1):
            raise ValueError('Continuation requires one article; prefer --article-ref')

        for row, capture, selection in page:
            view, mapping = _build_input_article(
                day, task, row, capture, selection, policy_hash,
                content_offset, max_content_chars, include_body,
            )
            views.append(view)
            mappings.append(mapping)

    # Close the read snapshot before starting the reference write transaction.
    _save_input_references(root, mappings, audit)
    next_offset = offset + len(page)
    result = dict(
        inputVersion=2,
        task=task,
        runDate=day,
        totalArticles=len(rows),
        matchingArticles=len(candidates),
        offset=offset,
        returnedArticles=len(views),
        nextOffset=next_offset if next_offset < len(candidates) else None,
        excluded=dict(excluded),
        articles=views,
    )
    if exclusion_reasons:
        result['exclusionReasons'] = [
            dict(state=state, reason=reason, count=count)
            for (state, reason), count in exclusion_reasons.items()
        ]
    return result

def additional(root,day,task,ref,kind='body',offset=0,maximum=1000,ids=None):
    from read_ai_inputs import content_chunk
    with project.reader(root) as reader:
        mapping,article,capture,record=resolve(reader.c,ref,day,task)
        if completed(reader.c,mapping['articleId'],task):return dict(ref=ref,state='saved')
        latest=reader.c.execute('SELECT raw_json FROM review_records WHERE article_id=? ORDER BY rowid DESC LIMIT 1',(mapping['articleId'],)).fetchone()
        hold=hold_state(reader.c,mapping['articleId'],task,json.loads(latest[0]) if latest else None)
        if hold and hold['state']=='held':return dict(ref=ref,**hold)
        if kind=='detail':
            value=dict(ref=ref,title=article.get('title',''),publishedAt=article.get('publishedAt',''),
                       contentStatus=(capture or {}).get('contentStatus','not_captured'),
                       state='ready' if record and record['taskStatus'][task]=='ready' else 'needs_review')
            if record:value.update(packet(record,task))
            elif capture:value['content']=content_chunk(capture,offset,maximum)
            return value
        if kind=='body':
            if not capture:return dict(ref=ref,state='no_saved_body')
            return dict(ref=ref,content=content_chunk(capture,offset,maximum))
        if kind=='source':return dict(ref=ref,url=article['url'],source=article.get('source'),references=mapping)
        if kind not in ('facts','evidence'):raise ValueError('Unknown reference kind')
        if not ids:raise ValueError('Select fact/evidence IDs explicitly')
        values=[x for x in (record or {}).get(kind,[]) if x['id'] in ids]
        if set(ids)!={x['id'] for x in values}:raise ValueError('Unknown fact/evidence ID in this reference')
        return dict(ref=ref,**{kind:values})
def expand_review(c,day,authored):
    if 'ref' not in authored:return authored
    raw=dict(authored);ref=raw.pop('ref')
    mapping,article,capture,_=resolve(c,ref,day)
    if completed(c,mapping['articleId'],mapping['task']):raise ValueError('Stage already saved')
    for key,value in dict(url=article['url'],inputHash=shared.input_hash(article,capture),policyHash=mapping['policyHash'],reviewVersion=1).items():
        if key in raw and raw[key]!=value:raise ValueError('Authored reference disagrees with source')
        raw[key]=value
    return raw
def completion(unit,ref,day,task,artifact):
    mapping,article,capture,record=resolve(unit.c,ref,day,task)
    if not record or record['taskStatus'][task]!='ready':raise ValueError('Saved artifact requires reviewed ready evidence')
    prior=completed(unit.c,mapping['articleId'],task)
    if prior and prior.get('reviewId')==mapping['reviewId']:return
    # New artifacts must still match the live source and rules.
    _,rows,captures=project.Reader(unit.c).load_day(day,[])
    matches=[r for r in rows if r['_project']['occurrenceId']==mapping['occurrenceId']]
    if len(matches)!=1:raise ValueError('Source occurrence changed')
    current=captures.get(article['url'])
    shared.validate_record(record,matches[0]['article'],current,shared.policy_hash(unit.root))
    value=dict(task=task,articleId=mapping['articleId'],reviewId=mapping['reviewId'],contentVersionId=mapping['contentVersionId'],
               ref=ref,artifact=artifact,state='saved')
    save_history(unit,'ai-stage-completion',value,mapping['articleId'])
def expand_artifact(unit,request):
    """Explicit reviewed refs only; empty proposal is never blanket completion."""
    request=dict(request)
    if request['kind']=='summary' and 'summaries' in request:
        entries=[]
        for item in request['summaries']:
            if not isinstance(item.get('text'),str) or not item['text'].strip():raise ValueError('Summary text required')
            mapping,article,cap,record=resolve(unit.c,item['ref'],request['day'],'article-summary')
            if not record or record['taskStatus']['article-summary']!='ready':raise ValueError('Summary not reviewed')
            if any(x in article['title'] for x in ('[',']','\n')) or any(x in article['url'] for x in ('(',')','\n')):
                raise ValueError('Use supported escaped Markdown authoring for special source characters')
            entries.append('- ['+article['title']+']('+article['url']+') - 本文確認: 確認済み - 要約: '+item['text'])
        supplied={resolve(unit.c,item['ref'],request['day'],'article-summary')[1]['url'] for item in request['summaries']}
        path='content/article-summaries/'+request['day']+'.md'
        for saved in unit.c.execute('SELECT raw_json FROM article_summaries WHERE source=? AND is_current=1 ORDER BY rowid',(path,)):
            raw=json.loads(saved[0])
            if raw['url'] in supplied:continue
            title,url=raw['links'][0]
            entries.append('- ['+title+']('+url+') - 本文確認: 確認済み - 要約: '+raw['text'])
        request['text']=request.get('preamble','')+'\n## Source-by-source Notes\n'+'\n'.join(entries)+'\n'
    if request['kind']=='proposal':
        value=json.loads(json.dumps(request['document']))
        task='talent-index' if request['directory']=='talent-index-proposals' else 'article-classification'
        for name in ('articles','classifications'):
            for row in value.get(name,[]):
                if 'ref' not in row:continue
                mapping,a,_,_=resolve(unit.c,row.pop('ref'),request['day'],task)
                field='url' if name=='articles' else 'article_url'
                if field in row and row[field]!=a['url']:raise ValueError('Wrong source URL')
                row[field]=a['url']
                if name=='articles':
                    row['title']=a['title']
                    try:key=project.Reader(unit.c).legacy_key(mapping['articleId'])
                    except ValueError:key=db.checksum(mapping['articleId'].encode())
                    if 'article_key' in row and row['article_key']!=key:raise ValueError('Authored article key disagrees with reference')
                    row['article_key']=key
                    row.setdefault('excerpt',a.get('excerpt',''));row.setdefault('source',a.get('source',''))
                    row.setdefault('published_at',a.get('publishedAt',''))
        for row in value.get('articleTalents',[]):
            if 'articleRef' not in row:continue
            mapping,a,_,_=resolve(unit.c,row.pop('articleRef'),request['day'],'talent-index')
            try:key=project.Reader(unit.c).legacy_key(mapping['articleId'])
            except ValueError:key=db.checksum(mapping['articleId'].encode())
            if 'article_key' in row and row['article_key']!=key:raise ValueError('Relation source disagrees with reference')
            row['article_key']=key
            row.setdefault('relation_key',key+'-'+row['talent_id'])
        # Retain other articles already saved in this daily proposal.
        existing=[doc for path,doc in project.Reader(unit.c).documents(request['directory'],request['day'])
                  if path=='content/'+request['directory']+'/'+request['day']+'.json']
        if existing:
            old=existing[0]
            keys={'articles':'article_key','talents':'talent_id','articleTalents':'relation_key',
                  'classifications':'article_url','reviewedArticles':'ref'}
            for name,key in keys.items():
                if name not in value and name not in old:continue
                rows={}
                for item in old.get(name,[])+value.get(name,[]):
                    identity=item.get(key) or item.get('article_key')
                    if not identity:raise ValueError('Proposal merge requires an explicit stable key')
                    rows[identity]=item
                value[name]=list(rows.values())
        request['document']=value
    return request
def record_artifact(unit,request):
    if request['kind']=='summary':
        for item in request.get('summaries',[]):completion(unit,item['ref'],request['day'],'article-summary',dict(kind='summary'))
    if request['kind']=='proposal' and request['directory'] in ('talent-index-proposals','article-classification-proposals'):
        task='talent-index' if request['directory']=='talent-index-proposals' else 'article-classification'
        document=request['document']
        for item in document.get('reviewedArticles',[]):
            if item.get('result') not in ('confirmed','none'):raise ValueError('Explicit confirmed/none result required')
            mapping,article,_,_=resolve(unit.c,item['ref'],request['day'],task)
            # Bind each receipt to an actual per-article proposal, including explicit negative findings.
            if item['result']=='confirmed':
                rows=document.get('articles' if task=='talent-index' else 'classifications',[])
                if not any(r.get('url',r.get('article_url'))==article['url'] for r in rows):raise ValueError('Reviewed article missing from proposal')
            completion(unit,item['ref'],request['day'],task,dict(kind='proposal',directory=request['directory'],
                       documentHash=shared.digest(document),result=item['result']))
def saved_for_day(root,day,task):
    if project.source(root)!='project-db':return None
    with project.reader(root) as r:
        _, rows = r.load_day_articles(day)
        return {row['article']['url']:completed(r.c,row['_project']['articleId'],task) for row in rows}

def validate_proposal(unit,request):
    if request['directory'] not in ('talent-index-proposals','article-classification-proposals'):return
    import autoarticle_artifacts as artifacts
    from autoarticle_progress import Progress
    value=request['document'];task='talent-index' if request['directory']=='talent-index-proposals' else 'article-classification'
    p=Progress(unit.root,request['day'],'http://127.0.0.1:5678')
    fields=('articles','talents','articleTalents') if task=='talent-index' else ('classifications',)
    if value.get('proposalVersion')!=1 or value.get('proposalDate')!=request['day'] or any(not isinstance(value.get(f),list) for f in fields):
        raise ValueError('Invalid proposal contract/date')
    check=artifacts.Check()
    artifacts.proposals(p,'talent-review' if task=='talent-index' else 'classification-review',check,
                       lambda: project.Reader(unit.c).dashboard(),staged=value)
    if check.count:raise ValueError('Invalid proposal: '+str(check.errors))

def selection_counts(root,day,task):
    if project.source(root)!='project-db':return None
    with project.reader(root) as reader:
        _,rows,captures=reader.load_day(day,[])
        policy_hash = shared.policy_hash(root)
        states = (
            select(reader.c, day, row, captures.get(row['article']['url']), task, policy_hash)['state']
            for row in rows
        )
        return dict(Counter(states))


def save_hold_assessment(unit,value):
    """Recheck source binding inside the existing transaction, then append only."""
    import legacy_hold_relevance as legacy_hold
    aid,task=value['articleId'],value['task']
    if completed(unit.c,aid,task):return
    held=unit.c.execute('SELECT * FROM review_records WHERE id=? AND article_id=?',
                        (value['heldReviewId'],aid)).fetchone()
    current=unit.c.execute('SELECT * FROM review_records WHERE id=? AND article_id=?',
                          (value['currentReviewId'],aid)).fetchone()
    if not held or not current:raise ValueError('Hold assessment source missing')
    if json.loads(held['raw_json'])['taskStatus'][task]!='held':
        raise ValueError('Hold assessment needs a historical hold')
    _,verified=legacy_hold.assess(unit.c,aid,task,held,current)
    if verified!=value:raise ValueError('Hold assessment differs from grounded inputs')
    save_history(unit,'ai-legacy-hold-assessment',value,aid)

def record_legacy_hold_assessments(unit,aid):
    """Only an article receiving new reviewed material; no bulk backfill."""
    for task in TASKS:
        if completed(unit.c,aid,task):continue
        audit=[]
        hold_state(unit.c,aid,task,None,audit)
        for value in audit:save_hold_assessment(unit,value)
