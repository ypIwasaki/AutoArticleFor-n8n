#!/usr/bin/env python3
"""Inspect/resume DB-first operations and switch writes only while writers are stopped."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import project_database as db
import project_write_outbox as writes
import project_compatibility


def stopped():
    import database_phase1
    with database_phase1.ro(project_compatibility.legacy_path()) as c:database_phase1.idle(c)
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name)==os.getpid():continue
        try:args=(process/'cmdline').read_bytes().decode(errors='replace').split('\0')
        except (FileNotFoundError,ProcessLookupError):continue
        if any(Path(a).name=='autoarticle_db_service.py' for a in args[:2]):
            raise writes.WriteStopped('stop_project_db_service_before_write_cutover')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','resume','switch'])
    parser.add_argument('--operation-id')
    parser.add_argument('--feature',choices=['n8n-daily','ai-reader','dashboard'])
    parser.add_argument('--target',choices=['legacy','project-db'])
    args=parser.parse_args()
    if args.action=='status':
        if args.operation_id:result=writes.status(args.operation_id)
        else:
            with closing(db.connect(readonly=True)) as c:
                result={'routes':[dict(x) for x in c.execute('SELECT * FROM cutover_state')],
                        'operations':[dict(x) for x in c.execute("SELECT s.id,s.status,s.outcome,p.db_committed_at FROM sync_runs s LEFT JOIN project_write_requests p ON p.operation_id=s.id WHERE s.operation LIKE 'project-write:%' ORDER BY s.started_at")]}
    elif args.action=='resume':
        if not args.operation_id:parser.error('--operation-id required')
        result=writes.drain(args.operation_id,db.database_path(),db.ROOT,project_compatibility.deliver)
    else:
        if not args.feature or not args.target:parser.error('--feature and --target required')
        stopped();writes.set_write_target(args.feature,args.target)
        result={'feature':args.feature,'writeTarget':args.target}
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
