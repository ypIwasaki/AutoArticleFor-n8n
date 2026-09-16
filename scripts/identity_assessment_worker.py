"""Run only a saved importer's decision path in an isolated Python process.
No destination DB is opened and no saved input/implementation is changed.
"""
from pathlib import Path
import sys,json,argparse

def main():
 p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--implementation',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 sys.dont_write_bytecode=True;sys.path.insert(0,str(a.implementation/'scripts'))
 import import_legacy_database as imp
 if Path(imp.__file__).resolve()!=(a.implementation/'scripts/import_legacy_database.py').resolve():raise RuntimeError('unexpected_identity_runtime')
 class Recording(imp.Importer):
  def __init__(self):
   super().__init__(None,a.snapshot,'reproduction-only',None,[]);self.assessments={}
  def put(self,table,**row):
   if table=='article_identity_assessments':self.assessments[row['source_record_id']]=row
  def paths(self,directory,suffix):return []
  def article(self,aid,raw,held=False):return aid
  def conflict(self,*args,**kwargs):pass
  def fetch(self,*args,**kwargs):pass
 engine=Recording();engine.preload();engine.contents()
 with a.output.open('w') as f:
  for sid,row in sorted(engine.assessments.items()):f.write(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n')
 print(json.dumps({'assessments':len(engine.assessments),'implementation_version':imp.VERSION}))
if __name__=='__main__':main()
