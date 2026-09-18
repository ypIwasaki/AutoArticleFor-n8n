"""Expand authored references into summary text and merged proposal documents."""

import json
import project_database as db
import project_readers as project
from ai_input_references import resolve


def _expand_summary_text(unit, request):
    """Render reviewed summaries and retain saved articles absent from this update."""
    entries = []
    for item in request['summaries']:
        if not isinstance(item.get('text'), str) or not item['text'].strip():
            raise ValueError('Summary text required')
        _, article, _, review = resolve(unit.c, item['ref'], request['day'], 'article-summary')
        if not review or review['taskStatus']['article-summary'] != 'ready':
            raise ValueError('Summary not reviewed')
        title_needs_escaping = any(character in article['title'] for character in ('[', ']', '\n'))
        url_needs_escaping = any(character in article['url'] for character in ('(', ')', '\n'))
        if title_needs_escaping or url_needs_escaping:
            raise ValueError('Use supported escaped Markdown authoring for special source characters')
        entries.append(
            '- [' + article['title'] + '](' + article['url']
            + ') - 本文確認: 確認済み - 要約: ' + item['text']
        )

    supplied_urls = {
        resolve(unit.c, item['ref'], request['day'], 'article-summary')[1]['url']
        for item in request['summaries']
    }
    source_path = 'content/article-summaries/' + request['day'] + '.md'
    saved_summaries = unit.c.execute(
        'SELECT raw_json FROM article_summaries WHERE source=? AND is_current=1 ORDER BY rowid',
        (source_path,),
    )
    for saved_summary in saved_summaries:
        record = json.loads(saved_summary[0])
        if record['url'] in supplied_urls:
            continue
        title, url = record['links'][0]
        entries.append('- [' + title + '](' + url + ') - 本文確認: 確認済み - 要約: ' + record['text'])
    return request.get('preamble', '') + '\n## Source-by-source Notes\n' + '\n'.join(entries) + '\n'


def _proposal_article_key(connection, article_id):
    """Use the legacy key when available, preserving the existing fallback."""
    try:
        return project.Reader(connection).legacy_key(article_id)
    except ValueError:
        return db.checksum(article_id.encode())


def _merge_proposal_records(previous, incoming):
    """Replace matching records in place and append new identities in input order."""
    identity_fields = {
        'articles': 'article_key',
        'talents': 'talent_id',
        'articleTalents': 'relation_key',
        'classifications': 'article_url',
        'reviewedArticles': 'ref',
    }
    for collection, identity_field in identity_fields.items():
        if collection not in incoming and collection not in previous:
            continue
        records_by_identity = {}
        for record in previous.get(collection, []) + incoming.get(collection, []):
            identity = record.get(identity_field) or record.get('article_key')
            if not identity:
                raise ValueError('Proposal merge requires an explicit stable key')
            records_by_identity[identity] = record
        incoming[collection] = list(records_by_identity.values())


def _expand_proposal_document(unit, request):
    """Resolve authored references in a copy, then retain other saved records."""
    document = json.loads(json.dumps(request['document']))
    task = 'talent-index' if request['directory'] == 'talent-index-proposals' else 'article-classification'
    for collection in ('articles', 'classifications'):
        for record in document.get(collection, []):
            if 'ref' not in record:
                continue
            mapping, article, _, _ = resolve(unit.c, record.pop('ref'), request['day'], task)
            url_field = 'url' if collection == 'articles' else 'article_url'
            if url_field in record and record[url_field] != article['url']:
                raise ValueError('Wrong source URL')
            record[url_field] = article['url']
            if collection == 'articles':
                record['title'] = article['title']
                article_key = _proposal_article_key(unit.c, mapping['articleId'])
                if 'article_key' in record and record['article_key'] != article_key:
                    raise ValueError('Authored article key disagrees with reference')
                record['article_key'] = article_key
                record.setdefault('excerpt', article.get('excerpt', ''))
                record.setdefault('source', article.get('source', ''))
                record.setdefault('published_at', article.get('publishedAt', ''))

    for relation in document.get('articleTalents', []):
        if 'articleRef' not in relation:
            continue
        mapping, _, _, _ = resolve(unit.c, relation.pop('articleRef'), request['day'], 'talent-index')
        article_key = _proposal_article_key(unit.c, mapping['articleId'])
        if 'article_key' in relation and relation['article_key'] != article_key:
            raise ValueError('Relation source disagrees with reference')
        relation['article_key'] = article_key
        relation.setdefault('relation_key', article_key + '-' + relation['talent_id'])

    source_path = 'content/' + request['directory'] + '/' + request['day'] + '.json'
    existing_documents = [
        document for path, document in project.Reader(unit.c).documents(request['directory'], request['day'])
        if path == source_path
    ]
    if existing_documents:
        _merge_proposal_records(existing_documents[0], document)
    return document


def expand_artifact(unit, request):
    """Expand authored references without treating an empty proposal as completion."""
    expanded = dict(request)
    if expanded['kind'] == 'summary' and 'summaries' in expanded:
        expanded['text'] = _expand_summary_text(unit, expanded)
    if expanded['kind'] == 'proposal':
        expanded['document'] = _expand_proposal_document(unit, expanded)
    return expanded
