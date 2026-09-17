"""Explicit compatibility exports/rollback after automatic legacy delivery stops.

Deferred outbox entries are retained, not acknowledged as delivered. Maintenance
rejects business commits; catch-up is an explicit operator action under that gate.
"""
from contextlib import closing
import argparse
import fcntl
import json
from pathlib import Path
import project_database as db

AUTOMATIC = "NOT EXISTS(SELECT 1 FROM compatibility_delivery_schedule s WHERE s.operation_id=d.operation_id AND s.ordinal=d.ordinal AND s.mode='on-demand')"


def mode(c):
    return c.execute('SELECT mode FROM compatibility_policy WHERE id=1').fetchone()[0]


def pending(c, operation_id=None, automatic=True):
    query="SELECT count(*) FROM compatibility_deliveries d WHERE d.status='pending'"
    args=[]
    if automatic:query+=' AND '+AUTOMATIC
    if operation_id is not None:query+=' AND d.operation_id=?';args.append(operation_id)
    return c.execute(query,args).fetchone()[0]


def schedule(c, operation_id, ordinal, kind, target, payload):
    selected=mode(c)
    if selected=='maintenance':raise RuntimeError('legacy_delivery_maintenance')
    deferred=selected=='on-demand' and (kind=='data-table' or Path(target).suffix in ('.json','.jsonl'))
    previous=None
    if deferred:
        rows=c.execute("SELECT d.* FROM compatibility_deliveries d WHERE d.kind=? AND d.target=? AND d.status='pending' AND NOT(d.operation_id=? AND d.ordinal=?) ORDER BY d.rowid DESC",(kind,target,operation_id,ordinal))
        for row in rows:
            if kind=='file' or json.loads(row['payload_json']).get('rowKey')==payload.get('rowKey'):
                previous=row;break
    c.execute('INSERT INTO compatibility_delivery_schedule VALUES(?,?,?,?,?)',(operation_id,ordinal,'on-demand' if deferred else 'automatic',previous['operation_id'] if previous else None,previous['ordinal'] if previous else None))


def render(row, database):
    import continuous_database_sync as sync
    from project_write_outbox import WriteStopped
    value=json.loads(row['payload_json'])
    if sync.digest(value)!=row['payload_hash']:raise WriteStopped('outbox_payload_hash_changed')
    if row['kind']=='data-table':return json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    if 'text' in value:return value['text']
    from project_business_writes import render_compatibility_file
    return render_compatibility_file(value,database)


def predecessor(c,row):
    s=c.execute('SELECT * FROM compatibility_delivery_schedule WHERE operation_id=? AND ordinal=?',(row['operation_id'],row['ordinal'])).fetchone()
    if not s or s['previous_operation_id'] is None:return None
    p=c.execute('SELECT * FROM compatibility_deliveries WHERE operation_id=? AND ordinal=?',(s['previous_operation_id'],s['previous_ordinal'])).fetchone()
    if not p or p['status']!='complete':raise RuntimeError('previous_delivery_not_complete')
    return p


def delivery_view(c,row,database):
    """Use the immutable predecessor chain, never rewrite saved request payloads."""
    value=dict(row);parent=predecessor(c,row)
    if not parent:return value
    if row['kind']=='file':
        value['previous_hash']=db.checksum(render(parent,database).encode())
    else:
        current=json.loads(row['payload_json']);before=dict(current['before'] or {})
        chain=[]
        while parent:
            render(parent,database)  # validate every immutable payload
            chain.append(json.loads(parent['payload_json'])['data']);parent=predecessor(c,parent)
        for changes in reversed(chain):before.update(changes)
        value['expected_before']={k:before.get(k) for k in current['data']}
    return value


def set_mode(value,database):
    if value not in ('automatic','on-demand','maintenance'):raise ValueError('Invalid policy')
    with closing(db.connect(database)) as c,db.transaction(c):
        if pending(c):raise RuntimeError('automatic_delivery_pending')
        if value=='automatic' and pending(c,automatic=False):raise RuntimeError('catch_up_deferred_before_automatic')
        c.execute('UPDATE compatibility_policy SET mode=?,changed_at=? WHERE id=1',(value,db.now()))


def export(operation_id,directory,database):
    """Write a new, separate export directory; never touch canonical legacy files."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    records=[]
    with closing(db.connect(database,readonly=True)) as c:
        rows=c.execute('SELECT * FROM compatibility_deliveries WHERE operation_id=? ORDER BY ordinal',(operation_id,)).fetchall()
        if not rows:raise ValueError('No saved output for operation')
        for row in rows:
            relative=row['target'] if row['kind']=='file' else 'data-tables/'+row['target']+'-'+str(row['ordinal'])+'.json'
            if Path(relative).is_absolute() or '..' in Path(relative).parts:raise ValueError('Unsafe export path')
            destination=directory/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            body=render(row,database).encode();destination.write_bytes(body)
            records.append(dict(path=relative,sha256=db.checksum(body),operation_id=operation_id,ordinal=row['ordinal']))
    (directory/'manifest.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    return dict(operationId=operation_id,files=len(records),directory=str(directory))


def catch_up(database,root,deliver=None,fault=None):
    """Only explicit rollback: stop writers, set maintenance, start local API only."""
    from project_write_outbox import deliver_file
    import continuous_database_sync as sync
    with Path(database).with_suffix('.write.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with closing(db.connect(database)) as c:
            if mode(c)!='maintenance':raise RuntimeError('maintenance_required')
            rows=c.execute("SELECT * FROM compatibility_deliveries WHERE status='pending' ORDER BY rowid").fetchall()
            for row in rows:
                view=delivery_view(c,row,database)
                if row['kind']=='file':deliver_file(view,root,database)
                elif deliver:deliver(view)
                else:raise RuntimeError('compatibility_adapter_unavailable')
                if fault:fault('after_delivery',row['ordinal'])
                with db.transaction(c):
                    c.execute("UPDATE compatibility_deliveries SET status='complete',completed_at=? WHERE operation_id=? AND ordinal=?",(db.now(),row['operation_id'],row['ordinal']))
                    request=c.execute('SELECT request_hash FROM sync_runs WHERE id=?',(row['operation_id'],)).fetchone()[0]
                    sync.event(c,row['operation_id'],request,'replayed',{'stage':'explicit_legacy_catch_up','ordinal':row['ordinal']})
            return dict(delivered=len(rows),remaining=pending(c,automatic=False))


def capture_report(day,database):
    import project_readers
    lines=['# 本文取得記録 '+day,'','出典: 専用DB。取得状態は意味的レビュー完了を示さない。','']
    with project_readers.reader(database=database) as reader:
        _,articles,captures=reader.load_day(day,[])
        titles={x['article']['url']:x['article']['title'] for x in articles}
        for url,record in captures.items():
            body=record.get('contentText','')
            lines.extend(['## '+titles.get(url,url).replace('\n',' '),'',
                '- URL: '+url,'- 取得状態: '+record['contentStatus'],
                '- 本文文字数: '+str(len(body)),
                '- 本文SHA-256: '+(db.checksum(body.encode()) if body else '本文なし'),
                '- 取得理由: '+str(record.get('failureReason') or 'なし'),
                '- 再試行予定: '+str(record.get('retry_after') or 'なし'),
                '- 専用DB参照: '+db.canonical(record.get('_project',{})),
                '', '> '+body[:160].replace('\n',' ') if body else '> 本文を生成・補完していない。',''])
    return '\n'.join(lines)+'\n'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',type=Path,default=db.database_path())
    sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('policy');s.add_argument('mode',choices=['automatic','on-demand','maintenance']);s.add_argument('--writers-stopped',action='store_true',required=True)
    e=sub.add_parser('export');e.add_argument('--operation-id',required=True);e.add_argument('--directory',type=Path,required=True)
    report=sub.add_parser('report');report.add_argument('--day',required=True);report.add_argument('--output',type=Path,required=True)
    sub.add_parser('catch-up');sub.add_parser('status');a=p.parse_args()
    if a.command=='policy':
        # The DB transaction enforces the delivery gates; the caller also stops
        # external n8n/article writers before changing the maintenance policy.
        from project_write_control import stopped
        stopped();set_mode(a.mode,a.database);result={'mode':a.mode}
    elif a.command=='export':result=export(a.operation_id,a.directory,a.database)
    elif a.command=='report':
        text=capture_report(a.day,a.database)
        a.output.parent.mkdir(parents=True,exist_ok=True)
        with a.output.open('x') as out:out.write(text)
        result=dict(report=str(a.output),day=a.day,source='project-db')
    elif a.command=='catch-up':
        from project_compatibility import deliver
        result=catch_up(a.database,db.ROOT,deliver)
    else:
        with closing(db.connect(a.database,readonly=True)) as c:result=dict(mode=mode(c),pendingAutomatic=pending(c),pendingTotal=pending(c,automatic=False))
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
