"""Remaining phase5 acceptance; synthetic inputs never count as normal operations."""
import contextlib
import copy
import json
import unittest
import project_database as db
import continuous_database_sync as sync
from test_continuous_database_sync import AllBusinessScopeTests

class RemainingAcceptanceTests(AllBusinessScopeTests):
    def independent_compare(self, expected):
        frozen=copy.deepcopy(expected)
        def compare(c, request):
            differences=[]
            for change in frozen:
                table=sync.ENTITIES[change['entity']]
                key=change['key']
                row=c.execute('SELECT * FROM '+table+' WHERE '+' AND '.join('"'+k+'"=?' for k in key), list(key.values())).fetchone()
                actual=dict(row) if row else None
                if actual!=change['after']:
                    differences.append(dict(target=table,key=key,old_value=change['after'],new_value=actual,reason='independent_fixture_mismatch',source=change['source'],approval_status='unapproved'))
            return differences
        return compare

    def test_all_domains_new_versions_keep_prior_values_and_provenance(self):
        first=self.complete_request()
        sync.synchronize('acceptance-version-one',first,self.path,lambda r:None,self.independent_compare(first['changes']))
        second=copy.deepcopy(first);second['changes']=[]
        replacements={x['after']['id']:x['after']['id']+'-v2' for x in first['changes'] if 'id' in x['after'] and x['entity'] not in ('article','collection','talent','relationship')}
        for old in first['changes']:
            entity=old['entity'];new=copy.deepcopy(old);row=new['after']
            for field,value in list(row.items()):
                if (field=='id' or field.endswith('_id')) and value in replacements:row[field]=replacements[value]
            if entity in ('article','collection','talent'):
                new['before']=copy.deepcopy(old['after'])
                field={'article':'title','collection':'search_conditions_json','talent':'organization'}[entity]
                row[field]={'article':'Revised title','collection':'{"updated":true}','talent':'Revised organization'}[entity]
            elif entity=='relationship':
                new['before']=copy.deepcopy(old['after']);row['evidence']='Revised saved body'
            elif entity=='identifier':row['value']='legacy-two'
            elif entity=='alias':row['alias']='Second alias'
            elif entity=='occurrence':row['source_record']='two'
            elif entity=='body':
                row['text']='Revised saved body';row['text_hash']=db.checksum(row['text'].encode())
                row['payload_hash']=sync.digest({k:v for k,v in row.items() if k not in ('id','payload_hash','text_hash')})
            elif entity=='fetchAttempt':
                raw=dict(content_text='Revised saved body',content_hash=db.checksum(b'Revised saved body'),body_length=18,content_status='verified')
                row.update(raw_json=db.canonical(raw),stored_body_hash=raw['content_hash'],computed_body_hash=raw['content_hash'],stored_body_length=18)
            elif entity in ('summary','classification','feedback'):
                retire=copy.deepcopy(old);retire['before']=copy.deepcopy(retire['after']);retire['after']['is_current']=0
                retire['source']['raw']=copy.deepcopy(retire['after']);retire['source']['input_hash']=sync.digest(retire['source']['raw']);second['changes'].append(retire)
                row['version']=2
            with contextlib.closing(db.connect(self.path,readonly=True)) as c:
                new['key']={k:row[k] for k in sync.primary_key(c,sync.ENTITIES[entity])}
            raw=json.loads(row['raw_json']) if entity=='fetchAttempt' else copy.deepcopy(row)
            new['source'].update(position='2',raw=raw,input_hash=sync.digest(raw))
            second['changes'].append(new)
        result=sync.synchronize('acceptance-version-two',second,self.path,lambda r:None,self.independent_compare(second['changes']))
        self.assertEqual(result['added'],18);self.assertEqual(result['updated'],7)
        counts=self.counts();self.assertEqual(result,self.send('acceptance-version-two',second));self.assertEqual(counts,self.counts())
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM article_content_versions').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM review_records').fetchone()[0],2)
            for change in first['changes']:
                if change['entity'] in sync.IMMUTABLE:self.assertEqual(sync.read_row(c,change['entity'],change['key']),change['after'])
            for change in second['changes']:
                history=c.execute('SELECT * FROM sync_row_history WHERE operation_id=? AND entity=? AND row_key=?',('acceptance-version-two',change['entity'],db.canonical(change['key']))).fetchone()
                self.assertEqual(json.loads(history['source_json']),change['source'])
                self.assertEqual(json.loads(history['new_json']),change['after'])
                self.assertEqual(json.loads(history['old_json']) if history['old_json'] else None,change['before'])
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())

    def test_independent_difference_rolls_back_every_entity_and_same_id_resumes(self):
        request=self.complete_request();wrong=copy.deepcopy(request['changes']);wrong[0]['after']['title']='Expected other title'
        differences=[]
        def compare(c,r):
            found=self.independent_compare(wrong)(c,r);differences.extend(found);return found
        with self.assertRaisesRegex(sync.SyncStopped,'post_sync_comparison_difference'):
            sync.synchronize('acceptance-difference',request,self.path,lambda r:None,compare)
        self.assertEqual(len(differences),1)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            for table in set(sync.ENTITIES.values()):self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM sync_row_history').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM sync_source_receipts').fetchone()[0],0)
        self.assertEqual(sync.operation_status('acceptance-difference',self.path)['outcome'],'difference')
        result=sync.synchronize('acceptance-difference',request,self.path,lambda r:None,self.independent_compare(request['changes']))
        self.assertEqual(result['added'],22)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT outcome FROM sync_attempts ORDER BY rowid')],['difference','success'])

if __name__=='__main__':unittest.main()
