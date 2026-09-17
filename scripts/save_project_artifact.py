#!/usr/bin/env python3
"""Explicitly validate/save an authored staging artifact to DB before compatibility output."""
import argparse
import json
from pathlib import Path
import project_database as db
import project_business_writes as business


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kind',required=True,choices=['summary','proposal'])
    p.add_argument('--run-date',required=True)
    p.add_argument('--input',required=True,type=Path)
    p.add_argument('--directory',choices=['talent-index-proposals','article-classification-proposals','official-talent-registry','article-feedback-instructions'])
    p.add_argument('--operation-id')
    args=p.parse_args();text=args.input.read_text(encoding='utf-8-sig')
    payload=dict(day=args.run_date)
    if args.kind=='summary':
        if text.lstrip().startswith('{'):
            payload['summaries']=json.loads(text)['summaries']
        else:payload['text']=text
    else:
        if not args.directory:p.error('--directory is required for proposals')
        payload.update(directory=args.directory,document=json.loads(text))
    operation=args.operation_id or 'db-artifact-'+args.kind+'-'+db.checksum(db.canonical(payload).encode())
    result=business.submit(operation,args.kind,payload)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
