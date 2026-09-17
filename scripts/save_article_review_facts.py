#!/usr/bin/env python3
"""Validate and save explicitly reviewed facts; never infer review completion."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import article_review_facts as shared
from read_ai_inputs import ROOT, load_day, parse_day


def save_reviews(root: Path, run_date: str, payload, check_only: bool = False) -> dict:
    parse_day(run_date)
    rows = payload if isinstance(payload, list) else [payload]
    warnings = []
    _, articles, captures = load_day(root, run_date, warnings)
    policy = shared.policy_hash(root)
    if not policy:
        raise ValueError(f"Missing shared review policy: {shared.RULES}")
    sources = {(row['article']['url'], shared.input_hash(row['article'], captures.get(row['article']['url']))): row['article'] for row in articles}
    validated, keys = [], set()
    import project_readers
    if project_readers.source(root) == "project-db":
        from ai_input_minimization import expand_review
        with project_readers.reader(root) as reader:
            rows = [expand_review(reader.c,run_date,row) for row in rows]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('url'), str) or not isinstance(row.get('inputHash'), str):
            raise ValueError('Each review requires a URL and inputHash from the input reader')
        key = (row['url'], row['inputHash'])
        if key in keys:
            raise ValueError('Duplicate URL/inputHash in submitted batch')
        keys.add(key)
        article = sources.get(key)
        if article is None:
            raise ValueError('Review input changed or URL is absent from this run; re-read the source')
        shared.validate_record(row, article, captures.get(row['url']), policy)
        saved = dict(row, sourceDate=run_date, reviewedAt=datetime.now(timezone.utc).isoformat())
        shared.validate_record(saved, article, captures.get(row['url']), policy)
        validated.append(saved)
    relative = f'{shared.DIRECTORY}/{run_date}.jsonl'
    if not check_only and validated:
        import project_readers
        import project_business_writes as business
        database=project_readers.path_for(root)
        if database.exists() and business.route('ai-reader',database)=='project-db':
            # The original authored packet determines the ID; server-assigned save
            # times are created once inside the successful business transaction.
            import project_database as db
            operation='db-review-'+db.checksum(db.canonical(dict(day=run_date,records=rows)).encode())
            result=business.submit(operation,'reviews',dict(day=run_date,records=rows),database,root)
            return dict(checked=len(validated),saved=len(validated),checkOnly=False,path=relative,warnings=warnings,**result)
        shared.atomic_merge(root / relative, validated)
    return {'checked': len(validated), 'saved': 0 if check_only else len(validated),
            'checkOnly': check_only, 'path': relative, 'warnings': warnings}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-date', required=True)
    parser.add_argument('--input', required=True, type=Path, help='Authored JSON review object or array; not a body capture file')
    parser.add_argument('--check-only', action='store_true', help='Validate all rows without writes')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        payload = json.loads(args.input.read_text(encoding='utf-8-sig'))
        result = save_reviews(ROOT, args.run_date, payload, args.check_only)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
