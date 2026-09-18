"""Read source-bound article references and validate their recorded versions."""

import json
import article_review_facts as shared
import project_record_values as record_values
import project_readers as project


def reference_id(mapping):
    return 'a' + shared.digest(mapping)[:16]


def _resolve_reference_capture(connection, mapping):
    """Load the exact fetch attempt and body version recorded in a reference."""
    if not mapping['fetchAttemptId']:
        return None
    fetch_attempt = connection.execute(
        'SELECT * FROM content_fetch_attempts WHERE id=? AND article_id=?',
        (mapping['fetchAttemptId'], mapping['articleId']),
    ).fetchone()
    if not fetch_attempt or fetch_attempt['version_id'] != mapping['contentVersionId']:
        raise ValueError('Reference body mismatch')
    return project.Reader(connection).capture(fetch_attempt)


def _resolve_reference_review(connection, mapping, article, capture):
    """Validate the referenced review against its recorded article and body."""
    if not mapping['reviewId']:
        return None
    review_row = connection.execute(
        'SELECT * FROM review_records WHERE id=? AND article_id=?',
        (mapping['reviewId'], mapping['articleId']),
    ).fetchone()
    if not review_row or review_row['content_version_id'] != mapping['contentVersionId']:
        raise ValueError('Reference review/body mismatch')
    review_record = json.loads(review_row['raw_json'])
    shared.validate_record(review_record, article, capture, review_record['policyHash'])
    return review_record


def resolve(c, ref, day, task=None):
    """Restore source-bound inputs, rejecting mismatched references in order."""
    reference_row = c.execute(
        "SELECT raw_json FROM legacy_history_records WHERE id=? AND kind='ai-reference'",
        (record_values.record_id('ai-reference', ref),),
    ).fetchone()
    if not reference_row:
        raise ValueError('Unknown article reference')

    mapping = json.loads(reference_row[0])
    if reference_id(mapping) != ref or mapping['day'] != day:
        raise ValueError('Reference belongs to another input/day')
    if task and mapping['task'] != task:
        raise ValueError('Reference belongs to another task')

    occurrence = c.execute(
        'SELECT * FROM article_occurrences WHERE id=? AND article_id=?',
        (mapping['occurrenceId'], mapping['articleId']),
    ).fetchone()
    if not occurrence:
        raise ValueError('Reference occurrence missing')
    article = json.loads(occurrence['observations_json'])['article']
    capture = _resolve_reference_capture(c, mapping)
    review = _resolve_reference_review(c, mapping, article, capture)
    return mapping, article, capture, review
