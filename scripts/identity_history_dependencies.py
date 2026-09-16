"""Registered, closed historical inputs; never resolve a live fallback."""
from pathlib import Path
import json,shutil,hashlib,ast,re
import project_database as db

VERSION='identity-history-registry-v1'
REGISTRY='docs/database-identity-history.json'

def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def load_registry(path):
 value=json.loads(Path(path).read_text())
 if value.get('format')!=VERSION or set(value)!={'format','origins'} or not value['origins']:raise RuntimeError('identity_history_registry_version_or_shape')
 ids=[x['id'] for x in value['origins']]
 if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',x) for x in ids):raise RuntimeError('invalid_identity_origin_id')
 if len(ids)!=len(set(ids)):raise RuntimeError('duplicate_identity_origin')
 return value

def file_check(root,item):
 from sync_snapshot_dependencies import check_file
 check_file(root,item)

def verify_origin(folder,registration):
 folder=Path(folder)
 if sha(folder/'origin.json')!=registration['manifest_sha256']:raise RuntimeError('identity_origin_manifest_changed')
 origin=json.loads((folder/'origin.json').read_text())
 if origin.get('format')!='identity-origin-v1' or origin['id']!=registration['id']:raise RuntimeError('identity_origin_version_or_registration')
 if origin.get('capture_scope_complete') is not True:raise RuntimeError('historical_capture_scope_not_registered')
 files=origin['files'];names=[x['path'] for x in files]
 if len(names)!=len(set(names)) or not {'n8n.sqlite','files/content/article-body-captures/backfill-state.json','implementation/scripts/import_legacy_database.py'}<=set(names):raise RuntimeError('identity_origin_required_files_missing')
 for item in files:file_check(folder,item)
 proc=origin['processor'];implementation=folder/'implementation'/proc['path']
 if sha(implementation)!=proc['sha256'] or proc['version']!=registration['processor_version']:raise RuntimeError('identity_processor_unregistered_version')
 import import_legacy_database as current
 versions=[ast.literal_eval(n.value) for n in ast.parse(implementation.read_text()).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in n.targets)]
 if versions!=[proc['version']] or proc['version'] not in ('phase4-length-alias-import-v3',current.VERSION):raise RuntimeError('identity_processor_unregistered_version')
 return origin

def capture_registry(root):
 root=Path(root);registry=load_registry(root/REGISTRY)
 for item in registry['origins']:
  expected='.operation-state/database/identity-origins/'+item['id']
  if item['root']!=expected:raise RuntimeError('identity_origin_path_unregistered')
  verify_origin(root/item['root'],item)
 return {'path':REGISTRY,'size':(root/REGISTRY).stat().st_size,'sha256':sha(root/REGISTRY),'format_version':VERSION,'origins':registry['origins']}

def copy(root,snapshot,item):
 root=Path(root);snapshot=Path(snapshot);out=snapshot/'files'/REGISTRY;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/REGISTRY,out)
 for registration in item['origins']:
  source=root/registration['root'];origin=verify_origin(source,registration);dest=snapshot/'identity-history'/registration['id'];dest.mkdir(parents=True)
  for entry in origin['files']:
   output=dest/entry['path'];output.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/entry['path'],output);file_check(dest,entry)
  shutil.copyfile(source/'origin.json',dest/'origin.json')
 verify(snapshot,item)

def verify(snapshot,item):
 snapshot=Path(snapshot)
 if item.get('path')!=REGISTRY or item.get('format_version')!=VERSION:raise RuntimeError('identity_history_dependency_missing_or_version')
 file_check(snapshot/'files',item);registry=load_registry(snapshot/'files'/REGISTRY)
 if registry['origins']!=item.get('origins'):raise RuntimeError('identity_history_unregistered_origin')
 return [(snapshot/'identity-history'/x['id'],verify_origin(snapshot/'identity-history'/x['id'],x)) for x in registry['origins']]

def assert_live(root,item):
 file_check(root,item)
 for registration in item['origins']:verify_origin(Path(root)/registration['root'],registration)
