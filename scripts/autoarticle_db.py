#!/usr/bin/env python3
"""Project database administration; does not modify legacy data."""
import argparse
import json
import os
from pathlib import Path
import project_database as db

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('init','migrate','status','conflicts'): sub.add_parser(name)
    backup=sub.add_parser('backup')
    backup.add_argument('--output',type=Path,required=True)
    restore=sub.add_parser('restore-check')
    restore.add_argument('--snapshot',type=Path,required=True)
    restore.add_argument('--output',type=Path,required=True)
    legacy=sub.add_parser('import-legacy')
    legacy.add_argument('--snapshot',type=Path,required=True)
    legacy.add_argument('--report',type=Path,required=True)
    prepare=sub.add_parser('prepare-sync')
    prepare.add_argument('--operation-id',required=True)
    prepare.add_argument('--date',required=True)
    prepare.add_argument('--step',required=True)
    prepare.add_argument('--output',type=Path,required=True)
    synchronize=sub.add_parser('sync')
    synchronize.add_argument('--operation-id',required=True)
    synchronize.add_argument('--snapshot',type=Path,required=True)
    inspect=sub.add_parser('sync-status')
    inspect.add_argument('--operation-id',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    if args.command in ('init','migrate'): result=db.migrate(args.database)
    elif args.command=='import-legacy':
        from import_legacy_database import checked_import
        result=checked_import(args.snapshot,args.database or db.database_path())
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        result={k:v for k,v in result.items() if k!='dispositions'}
    elif args.command=='prepare-sync':
        from prepare_legacy_sync import prepare
        result=prepare(args.operation_id,args.date,args.step,db.ROOT,Path.home()/'.n8n/database.sqlite',args.output)
    elif args.command=='sync':
        from legacy_sync_projection import synchronize_snapshot
        from prepare_legacy_sync import live_guard
        snapshot=args.snapshot.resolve()
        result=synchronize_snapshot(args.operation_id,snapshot,args.database or db.database_path(),snapshot/'projection.sqlite',snapshot/'sync-request.json',lambda receipt:live_guard(snapshot,db.ROOT,Path.home()/'.n8n/database.sqlite'))
    elif args.command=='sync-status':
        from continuous_database_sync import operation_status
        result=operation_status(args.operation_id,args.database)
    elif args.command=='status': result=db.status(args.database)
    elif args.command=='backup': result=db.backup(args.output,args.database)
    elif args.command=='restore-check': result=db.restore_check(args.snapshot,args.output)
    else:
        c=db.connect(args.database,readonly=True)
        try: result=[dict(r) for r in c.execute("SELECT * FROM consolidation_conflicts WHERE status='unresolved'")]
        finally: c.close()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
