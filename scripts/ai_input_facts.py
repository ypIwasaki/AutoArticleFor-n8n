"""Pure selection of task facts and their linked evidence and entities."""

import article_review_facts as shared


def task_facts(record, task):
    """Select task facts in source order, honoring an explicitly empty selection."""
    selected_fact_ids = record.get('taskFacts', {}).get(task)
    if selected_fact_ids is None:
        if task == 'talent-index':
            selected_fact_ids = {
                fact_id
                for entity in record['entities']
                for fact_id in entity['factIds']
            }
            # A negative finding still needs evidence when no entity cites a fact.
            if not selected_fact_ids:
                selected_fact_ids = {fact['id'] for fact in record['facts']}
        else:
            selected_fact_ids = {fact['id'] for fact in record['facts']}
    return [fact for fact in record['facts'] if fact['id'] in selected_fact_ids]


def packet(record, task):
    """Include the selected facts and only their linked evidence and entities."""
    selected_facts = task_facts(record, task)
    selected_fact_ids = {fact['id'] for fact in selected_facts}
    selected_evidence_ids = {
        evidence_id
        for fact in selected_facts
        for evidence_id in fact['evidenceIds']
    }
    result = dict(
        facts=[
            {field: value for field, value in fact.items() if field != 'topics'}
            for fact in selected_facts
        ],
        evidence=[
            evidence for evidence in record['evidence']
            if evidence['id'] in selected_evidence_ids
        ],
    )

    selected_entities = []
    for entity in record['entities']:
        if not selected_fact_ids.intersection(entity['factIds']):
            continue
        linked_fact_ids = [
            fact_id for fact_id in entity['factIds'] if fact_id in selected_fact_ids
        ]
        selected_entities.append(dict(entity, factIds=linked_fact_ids))
    if selected_entities:
        result['entities'] = selected_entities
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
