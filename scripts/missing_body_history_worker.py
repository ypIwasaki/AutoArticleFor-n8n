"""Replay registered historical inputs with their saved implementation, in isolation."""
import argparse,json,sys
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--origin',required=True);parser.add_argument('--database',required=True)
    args=parser.parse_args();origin=Path(args.origin).resolve();destination=Path(args.database).resolve()
    if destination.exists():raise RuntimeError('historical_replay_destination_exists')
    sys.dont_write_bytecode=True;sys.path.insert(0,str(origin/'implementation/scripts'))
    import project_database as db
    import import_legacy_database as importer
    if Path(importer.__file__).resolve().parent!=origin/'implementation/scripts':raise RuntimeError('historical_implementation_fallback')
    meta=json.loads((origin/'origin.json').read_text());db.migrate(destination);c=db.connect(destination);c.execute('PRAGMA cache_size=-262144')
    try:
        with db.transaction(c):
            run=meta['original_migration_id']
            c.execute("INSERT INTO migration_runs(id,input_snapshot,importer_version,started_at,status) VALUES (?,?,?,?,'complete')",(run,str(origin),importer.VERSION,meta['recorded_at']))
            files=[dict(x,path=x['path'][6:]) for x in meta['files'] if x['path'].startswith('files/content/article-body-captures/')]
            engine=importer.Importer(c,origin,run,importer.utc(meta['epoch']),files)
            for stage in ('preload','n8n','contents'):getattr(engine,stage)()
        if c.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('historical_replay_foreign_key_violation')
    finally:c.close()

if __name__=='__main__':main()
