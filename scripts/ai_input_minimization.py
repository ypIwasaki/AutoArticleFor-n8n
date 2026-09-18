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

# Re-export existing entry points for CLI, writers, and callers.
from ai_input_facts import (
    task_facts,
    packet,
    material,
)
from ai_input_references import (
    reference_id,
    _resolve_reference_capture,
    _resolve_reference_review,
    resolve,
)
from ai_artifact_expansion import (
    _expand_summary_text,
    _proposal_article_key,
    _merge_proposal_records,
    _expand_proposal_document,
    expand_artifact,
)

TASKS=shared.TASKS
STEP_TASK={'summary':'article-summary','talent-review':'talent-index','classification-review':'article-classification'}
UNAVAILABLE={'unavailable','metadata_only'}
def history(c, kind, article_id):
    """Read an article's immutable history in insertion order."""
    rows = c.execute(
        "SELECT raw_json FROM legacy_history_records WHERE kind=? AND article_id=? ORDER BY rowid",
        (kind, article_id),
    )
    return [json.loads(row[0]) for row in rows]


def save_history(unit, kind, value, article_id=None, key=None):
    """Save provenance and history using the same deterministic source identity."""
    import project_business_writes as business

    history_key = key or shared.digest(value)
    source_record = business.source('ai-input/' + kind, history_key, value)
    source_id = record_values.record_id(
        'source', source_record['path'], source_record['position'], source_record['input_hash'],
    )
    history_id = record_values.record_id(kind, history_key)
    business.provenance(unit, source_record, 'legacy_history_records', history_id)
    history_record = dict(
        id=history_id,
        source_record_id=source_id,
        kind=kind,
        article_id=article_id,
        talent_id=None,
        raw_json=db.canonical(value),
    )
    unit.save('history', history_record, source_record)


def build_references(unit, request):
    """Persist source references before validating any accompanying hold assessments."""
    for mapping in request['references']:
        reference = reference_id(mapping)
        save_history(unit, 'ai-reference', mapping, mapping['articleId'], reference)
    for assessment in request.get('holdAssessments', []):
        save_hold_assessment(unit, assessment)


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

def _additional_reference_payload(ref, task, mapping, article, capture, review,
                                  kind, offset, maximum, ids):
    """Format the requested part of an already resolved source reference."""
    from read_ai_inputs import content_chunk

    if kind == 'detail':
        task_is_ready = review and review['taskStatus'][task] == 'ready'
        detail = dict(
            ref=ref,
            title=article.get('title', ''),
            publishedAt=article.get('publishedAt', ''),
            contentStatus=(capture or {}).get('contentStatus', 'not_captured'),
            state='ready' if task_is_ready else 'needs_review',
        )
        if review:
            detail.update(packet(review, task))
        elif capture:
            detail['content'] = content_chunk(capture, offset, maximum)
        return detail

    if kind == 'body':
        if not capture:
            return dict(ref=ref, state='no_saved_body')
        return dict(ref=ref, content=content_chunk(capture, offset, maximum))

    if kind == 'source':
        return dict(ref=ref, url=article['url'], source=article.get('source'), references=mapping)

    if kind not in ('facts', 'evidence'):
        raise ValueError('Unknown reference kind')
    if not ids:
        raise ValueError('Select fact/evidence IDs explicitly')
    selected_items = [item for item in (review or {}).get(kind, []) if item['id'] in ids]
    if set(ids) != {item['id'] for item in selected_items}:
        raise ValueError('Unknown fact/evidence ID in this reference')
    return dict(ref=ref, **{kind: selected_items})


def additional(root, day, task, ref, kind='body', offset=0, maximum=1000, ids=None):
    """Check current saved/held status before returning the referenced inputs."""
    with project.reader(root) as reader:
        mapping, article, capture, review = resolve(reader.c, ref, day, task)
        article_id = mapping['articleId']
        if completed(reader.c, article_id, task):
            return dict(ref=ref, state='saved')

        latest_review = reader.c.execute(
            'SELECT raw_json FROM review_records WHERE article_id=? ORDER BY rowid DESC LIMIT 1',
            (article_id,),
        ).fetchone()
        current_review = json.loads(latest_review[0]) if latest_review else None
        hold = hold_state(reader.c, article_id, task, current_review)
        if hold and hold['state'] == 'held':
            return dict(ref=ref, **hold)

        return _additional_reference_payload(
            ref, task, mapping, article, capture, review, kind, offset, maximum, ids,
        )


def expand_review(c, day, authored):
    """Fill source fields from the reference without changing authored facts."""
    if 'ref' not in authored:
        return authored
    expanded = dict(authored)
    reference = expanded.pop('ref')
    mapping, article, capture, _ = resolve(c, reference, day)
    if completed(c, mapping['articleId'], mapping['task']):
        raise ValueError('Stage already saved')

    source_fields = dict(
        url=article['url'],
        inputHash=shared.input_hash(article, capture),
        policyHash=mapping['policyHash'],
        reviewVersion=1,
    )
    for field, expected_value in source_fields.items():
        if field in expanded and expanded[field] != expected_value:
            raise ValueError('Authored reference disagrees with source')
        expanded[field] = expected_value
    return expanded


def _validate_current_completion_inputs(unit, day, mapping, article, review):
    """Require new completion records to match the current source and rules."""
    _, occurrences, captures = project.Reader(unit.c).load_day(day, [])
    matching_occurrences = [
        occurrence for occurrence in occurrences
        if occurrence['_project']['occurrenceId'] == mapping['occurrenceId']
    ]
    if len(matching_occurrences) != 1:
        raise ValueError('Source occurrence changed')
    current_article = matching_occurrences[0]['article']
    current_capture = captures.get(article['url'])
    shared.validate_record(
        review, current_article, current_capture, shared.policy_hash(unit.root),
    )


def completion(unit, ref, day, task, artifact):
    """Record completion after reviewed evidence and current inputs agree."""
    mapping, article, capture, review = resolve(unit.c, ref, day, task)
    if not review or review['taskStatus'][task] != 'ready':
        raise ValueError('Saved artifact requires reviewed ready evidence')

    prior_completion = completed(unit.c, mapping['articleId'], task)
    if prior_completion and prior_completion.get('reviewId') == mapping['reviewId']:
        return
    _validate_current_completion_inputs(unit, day, mapping, article, review)
    receipt = dict(
        task=task,
        articleId=mapping['articleId'],
        reviewId=mapping['reviewId'],
        contentVersionId=mapping['contentVersionId'],
        ref=ref,
        artifact=artifact,
        state='saved',
    )
    save_history(unit, 'ai-stage-completion', receipt, mapping['articleId'])


def _record_proposal_completions(unit, request):
    """Bind each explicit review result to its article before recording completion."""
    task = 'talent-index' if request['directory'] == 'talent-index-proposals' else 'article-classification'
    document = request['document']
    for reviewed_article in document.get('reviewedArticles', []):
        result = reviewed_article.get('result')
        if result not in ('confirmed', 'none'):
            raise ValueError('Explicit confirmed/none result required')
        reference = reviewed_article['ref']
        _, article, _, _ = resolve(unit.c, reference, request['day'], task)

        # A confirmed result needs a matching proposal; an explicit negative does not.
        if result == 'confirmed':
            collection = 'articles' if task == 'talent-index' else 'classifications'
            proposed_articles = document.get(collection, [])
            matches_source = any(
                proposal.get('url', proposal.get('article_url')) == article['url']
                for proposal in proposed_articles
            )
            if not matches_source:
                raise ValueError('Reviewed article missing from proposal')

        artifact = dict(
            kind='proposal',
            directory=request['directory'],
            documentHash=shared.digest(document),
            result=result,
        )
        completion(unit, reference, request['day'], task, artifact)


def record_artifact(unit, request):
    """Record only the per-article results explicitly supplied by the author."""
    if request['kind'] == 'summary':
        for summary in request.get('summaries', []):
            completion(unit, summary['ref'], request['day'], 'article-summary', dict(kind='summary'))
    if request['kind'] == 'proposal' and request['directory'] in (
        'talent-index-proposals', 'article-classification-proposals',
    ):
        _record_proposal_completions(unit, request)


def saved_for_day(root,day,task):
    if project.source(root)!='project-db':return None
    with project.reader(root) as r:
        _, rows = r.load_day_articles(day)
        return {row['article']['url']:completed(r.c,row['_project']['articleId'],task) for row in rows}

def validate_proposal(unit, request):
    """Check the document contract before validating its proposed business records."""
    if request['directory'] not in ('talent-index-proposals', 'article-classification-proposals'):
        return
    import autoarticle_artifacts as artifacts
    from autoarticle_progress import Progress

    document = request['document']
    is_talent_proposal = request['directory'] == 'talent-index-proposals'
    progress = Progress(unit.root, request['day'], 'http://127.0.0.1:5678')
    required_collections = (
        ('articles', 'talents', 'articleTalents') if is_talent_proposal else ('classifications',)
    )
    if (
        document.get('proposalVersion') != 1
        or document.get('proposalDate') != request['day']
        or any(not isinstance(document.get(field), list) for field in required_collections)
    ):
        raise ValueError('Invalid proposal contract/date')

    validation = artifacts.Check()
    review_step = 'talent-review' if is_talent_proposal else 'classification-review'
    artifacts.proposals(
        progress, review_step, validation,
        lambda: project.Reader(unit.c).dashboard(), staged=document,
    )
    if validation.count:
        raise ValueError('Invalid proposal: ' + str(validation.errors))


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


def save_hold_assessment(unit, value):
    """Recheck source binding inside the existing transaction, then append only."""
    import legacy_hold_relevance as legacy_hold

    article_id = value['articleId']
    task = value['task']
    if completed(unit.c, article_id, task):
        return

    held_review = unit.c.execute(
        'SELECT * FROM review_records WHERE id=? AND article_id=?',
        (value['heldReviewId'], article_id),
    ).fetchone()
    current_review = unit.c.execute(
        'SELECT * FROM review_records WHERE id=? AND article_id=?',
        (value['currentReviewId'], article_id),
    ).fetchone()
    if not held_review or not current_review:
        raise ValueError('Hold assessment source missing')
    held_record = json.loads(held_review['raw_json'])
    if held_record['taskStatus'][task] != 'held':
        raise ValueError('Hold assessment needs a historical hold')

    _, verified_assessment = legacy_hold.assess(unit.c, article_id, task, held_review, current_review)
    if verified_assessment != value:
        raise ValueError('Hold assessment differs from grounded inputs')
    save_history(unit, 'ai-legacy-hold-assessment', value, article_id)


def record_legacy_hold_assessments(unit, aid):
    """Assess only this article's unfinished tasks after new reviewed material."""
    for task in TASKS:
        if completed(unit.c, aid, task):
            continue
        assessments = []
        hold_state(unit.c, aid, task, None, assessments)
        for assessment in assessments:
            save_hold_assessment(unit, assessment)
