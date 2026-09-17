"""One synthetic article; isolated DB, no external network or production writes."""
import json
from pathlib import Path
import sys
import shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project_database as db
import project_business_writes as business
import project_readers as project
import article_review_facts as shared
DAY='2026-09-17'
URL='https://example.test/news/synthetic-event?source=ai-minimization-verification'
BODY='星野アキは青空社の所属で、9月20日に東京で音楽イベントへ出演する。参加費は3000円。'+('背景資料として会場の設備を説明する。'*360)+'追加根拠：出演契約の期間は1年間である。'
def setup(root):
    root=Path(root); path=root/'data/autoarticle.sqlite'
    if path.exists():raise ValueError('Fixture must use a fresh directory')
    db.migrate(path)
    with db.connect(path) as c,db.transaction(c):
        c.execute("UPDATE cutover_state SET read_source='project-db',write_target='project-db'")
        c.execute("UPDATE compatibility_policy SET mode='on-demand'")
    for relative in shared.POLICY_FILES+('config/article-classification-taxonomy.json',):
        target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(db.ROOT/relative,target)
    records=[dict(recordType='run',runDate=DAY,generatedAt=DAY+'T01:00:00Z',capturedArticleCount=1,workflowExecutionId='fixture'),
        dict(recordType='article',runDate=DAY,articleIndex=1,article=dict(url=URL,title='星野アキの音楽イベント',excerpt='出演情報',publishedAt=DAY+'T00:00:00Z',source='検証用配信元'))]
    business.submit_collection('fixture-collection',records,path,root)
    capture=dict(recordType='article-content',originalUrl=URL,resolvedUrl=URL,contentStatus='verified',contentText=BODY,contentMarkdown=BODY,contentLength=len(BODY),fetchedAt=DAY+'T01:01:00Z',contentCompleteness='full',contentType='article')
    business.submit('fixture-capture','capture',dict(day=DAY,record=capture),path,root)
    return root
def review(root,**overrides):
    with project.reader(root) as r:_,rows,caps=r.load_day(DAY,[])
    a=rows[0]['article'];cap=caps[URL];first=BODY.split('背景')[0]
    value=dict(reviewVersion=1,url=URL,inputHash=shared.input_hash(a,cap),policyHash=shared.policy_hash(root),basis='body',taskStatus={t:'ready' for t in shared.TASKS},
        facts=[dict(id='f1',text='星野アキは青空社所属。9月20日、東京の音楽イベントに出演。参加費3000円。',evidenceIds=['e1'])],
        entities=[dict(name='星野アキ',kind='person',factIds=['f1']),dict(name='青空社',kind='organization',factIds=['f1'])],
        evidence=[dict(id='e1',field='contentText',start=0,end=len(first),quote=first)],unresolved=[],reviewedBy='codex-synthetic-fixture',reviewedAt=DAY+'T01:05:00Z',sourceDate=DAY)
    value.update(overrides)
    return value
def save_review(root,value=None,op='fixture-review'):
    business.submit(op,'reviews',dict(day=DAY,records=[value or review(root)]),project.path_for(root),root)
def baseline(out):
    import read_ai_inputs as inputs
    root=setup(out/'fixture-before')
    packets={}
    def pair(task):
        return dict(initial=inputs.build_payload(root,DAY,task,limit=1),
          additional=inputs.build_payload(root,DAY,task,article_url=URL,limit=1,content_offset=6000,max_content_chars=1000,include_body=True))
    packets['common-review']=pair('article-summary')
    save_review(root)
    for task in shared.TASKS:packets[task]=pair(task)
    (out/'before-inputs.json').write_text(json.dumps(packets,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:{part:len(json.dumps(v,ensure_ascii=False,separators=(',',':'))) for part,v in pair.items()} for k,pair in packets.items()}))
if __name__=='__main__':
    baseline(db.ROOT/'.operation-state/ai-input-minimization')
