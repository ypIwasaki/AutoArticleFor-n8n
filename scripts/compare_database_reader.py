"""Small routed comparison entry; never runs migration/full-history acceptance."""
import argparse
import collections
import io
import json
from pathlib import Path
from compare_phase4_database import Compare, Difference, stamp
import read_ai_inputs
import project_readers as project
import project_database as db


def compare(old, new):
    audit = object.__new__(Compare)
    audit.stats = collections.Counter(); audit.hashes = {}; audit.stage = 'reader'
    audit.current = {}; audit.diffs = io.StringIO()
    try:
        audit.equal('business_value', old, new)
    except Difference:
        return dict(equal=False, differences=[json.loads(line) for line in audit.diffs.getvalue().splitlines()])
    return dict(equal=True, differences=[])


def representative(root, day, index=0):
    adopted = project.source(root, 'comparison')
    if adopted == 'project-db':
        with project.reader(root) as reader:
            _, rows, captures = reader.load_day(day, [])
    else:
        _, rows, captures = read_ai_inputs.load_day(root, day, [], feature='comparison')
    row=rows[index];article=row['article'];capture=captures.get(article['url']) or {}
    value=dict(article={key:article.get(key) for key in ('url','title','excerpt','source')},
               publishedAt=stamp(article.get('publishedAt')), status=capture.get('contentStatus','not_captured'),
               bodyHash=db.checksum(str(capture.get('contentText') or '').encode()))
    return dict(source=adopted,runDate=day,articleIndex=row.get('articleIndex',index+1),value=value)


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--date',required=True);parser.add_argument('--index',type=int,default=0)
    args=parser.parse_args();print(json.dumps(representative(db.ROOT,args.date,args.index),ensure_ascii=False))
