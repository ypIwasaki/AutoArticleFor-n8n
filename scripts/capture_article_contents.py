#!/usr/bin/env python3
"""Persist captured article text and public media metadata."""
from __future__ import annotations
import argparse,hashlib,json,os,re,sys,time
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from urllib import error,request
from urllib.parse import urlencode,urlparse
import backfill_article_summaries as old
from sync_workflow_to_n8n import api_request,load_env_file,normalize_api_base_url
ROOT=Path(__file__).resolve().parents[1]; DIR=ROOT/"content/article-body-captures"; STATE=DIR/"backfill-state.json"; NAME="article_contents"; MAX=100000
RECORDS=ROOT/"content/structured-records"
COLS=[{"name":n,"type":t} for n,t in [("article_key","string"),("original_url","string"),("resolved_url","string"),("source_domain","string"),("content_type","string"),("content_status","string"),("content_text","string"),("content_length","number"),("content_hash","string"),("extraction_method","string"),("failure_reason","string"),("content_path","string"),("fetched_at","date")]]
SHORT={"t.co","bit.ly","tinyurl.com","ow.ly","buff.ly","is.gd"}; VIDEO={"youtube.com","youtu.be","tiktok.com","vimeo.com","twitch.tv"}; SOCIAL={"x.com","twitter.com","instagram.com","facebook.com","threads.net","bsky.app"}
def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def host(u):
 h=urlparse(u).netloc.casefold().split(":")[0]; return h[4:] if h.startswith("www.") else h
def isin(h,s): return any(h==x or h.endswith("."+x) for x in s)
class Fetch:
 def __init__(self,g,p,r): self.d={"g":g,"p":p};self.next=defaultdict(float);self.r=r
 def __call__(self,u,*,data=None,content_type=None):
  k="g" if host(u)=="news.google.com" else "p"; hd={"User-Agent":old.USER_AGENT,"Accept-Language":"ja,en-US;q=0.8,en;q=0.6"}
  if content_type: hd["Content-Type"]=content_type
  for a in range(self.r):
   time.sleep(max(0,self.next[k]-time.monotonic()));self.next[k]=time.monotonic()+self.d[k]
   try:
    with request.urlopen(request.Request(u,data=data,headers=hd),timeout=20) as x:return x.read(2500000),x.geturl(),x.headers.get("Content-Type","")
   except error.HTTPError as e:
    if e.code not in {429,500,502,503,504} or a+1==self.r: raise old.CaptureError(f"HTTP {e.code}")
    self.next[k]=time.monotonic()+max(2**(a+1)*2,float(e.headers.get("Retry-After","0") or 0))
   except error.URLError as e:
    if a+1==self.r: raise old.CaptureError(f"接続失敗: {e.reason}")
    self.next[k]=time.monotonic()+2**a
def load():
 try:d=json.loads(STATE.read_text(encoding="utf8"))
 except (OSError,json.JSONDecodeError):return {},{}
 return d.get("entries",{}),d.get("resolvedUrls",{})

def load_record_articles(run_date=None):
 paths=[RECORDS/f"{run_date}.jsonl"] if run_date else sorted(RECORDS.glob("????-??-??.jsonl"))
 result={}
 for path in paths:
  if not path.exists(): continue
  for raw in path.read_text(encoding="utf8").splitlines():
   try: record=json.loads(raw)
   except json.JSONDecodeError: continue
   if record.get("recordType")!="article" or not isinstance(record.get("article"),dict): continue
   article=record["article"]; url=str(article.get("url","")).strip()
   if not url: continue
   result[url]=old.Article(url=url,title=str(article.get("title","")).strip(),excerpt=str(article.get("excerpt","")).strip(),source=str(article.get("source","")).strip(),published_at=str(article.get("publishedAt","")).strip(),last_seen_at=str(article.get("lastSeenAt","")).strip(),run_date=str(record.get("runDate") or path.stem))
 return result

def save(e,c):
 DIR.mkdir(parents=True,exist_ok=True);STATE.write_text(json.dumps({"schemaVersion":2,"generatedAt":now(),"entries":e,"resolvedUrls":c},ensure_ascii=False,indent=2)+"\n",encoding="utf8")
def make(a,key,status,kind,url,reason="",text="",method="",summary=""):
 text="\n".join(old.clean_text(x) for x in text.splitlines() if old.clean_text(x))[:MAX];return {"article_key":key or hashlib.sha256(a.url.encode()).hexdigest(),"original_url":a.url,"run_date":a.run_date,"title":a.title,"published_at":a.published_at,"status":status,"content_type":kind,"resolved_url":url,"source_host":host(url or a.url),"reason":reason,"content_text":text,"body_length":len(text),"content_hash":hashlib.sha256(text.encode()).hexdigest() if text else "","extraction_method":method,"summary":summary,"processed_at":now()}
def media(a,key,u,kind,f):
 try:
  ep="https://www.youtube.com/oembed" if isin(host(u),{"youtube.com","youtu.be"}) else "https://www.tiktok.com/oembed" if isin(host(u),{"tiktok.com"}) else ""
  if ep:
   b,_,_=f(ep+"?"+urlencode({"url":u,"format":"json"}));d=json.loads(b.decode("utf8","replace"))
   text="\n".join(x+": "+str(d.get(y,"")) for x,y in [("タイトル","title"),("投稿者・チャンネル","author_name"),("配信元","provider_name")] if d.get(y))
  else:
   b,u,t=f(u);_,desc=old.meaningful_blocks(b.decode("utf8","replace"),a.title);text="タイトル: "+a.title+("\n概要: "+desc if desc else "")
  return make(a,key,"metadata_only",kind,u,"公開メタデータを保存しました。動画本編・非公開投稿は保存していません。",text,"oembed-or-page-metadata")
 except Exception as x:return make(a,key,"unavailable",kind,u,"公開メタデータを取得できませんでした: "+str(x),method="public-metadata")
def capture(a,key,f,cache,kw):
 try:
  u=cache.get(a.url,a.url)
  if host(u)=="news.google.com":u=old.resolve_google_url(u)
  if host(u) in SHORT:_,u,_=f(u)
  cache[a.url]=u
  if isin(host(u),VIDEO):return media(a,key,u,"video_metadata",f)
  if isin(host(u),SOCIAL):return media(a,key,u,"social_metadata",f)
  b,u,t=f(u)
  if "html" not in t.casefold():return make(a,key,"unavailable","article",u,"HTML記事ページではありませんでした",method="publisher-html")
  blocks,desc=old.meaningful_blocks(b.decode("utf8","replace"),a.title);text="\n".join(blocks);summary=old.extract_summary(blocks,kw)
  if len(text)>=300 and len(old.split_sentences(summary))>=2 and len(summary)>=90:return make(a,key,"verified","article",u,text=text,method="publisher-html",summary=summary)
  if len(text or desc)>=80:return make(a,key,"partial","article",u,"本文または要点の文章量が不足するため部分取得として保存しました",text or desc,"publisher-html",summary)
  return make(a,key,"unavailable","article",u,"本文として十分なテキストを取得できませんでした",text or desc,"publisher-html")
 except old.CaptureError as x:return make(a,key,"unavailable","unknown",cache.get(a.url),"{}".format(x),method="url-resolution")
def archive(e):
 groups=defaultdict(list)
 for x in e.values():groups[x.get("run_date","")[:10]].append(x)
 for day,vs in groups.items():
  if len(day)!=10:continue
  p=DIR/(day+".jsonl");rel=str(p.relative_to(ROOT)).replace("\\","/")
  for x in vs:x["content_path"]=rel
  p.write_text("\n".join(json.dumps({"recordType":"article-content","articleKey":x.get("article_key",""),"originalUrl":x.get("original_url",""),"resolvedUrl":x.get("resolved_url"),"contentStatus":x.get("status","unavailable"),"contentType":x.get("content_type","unknown"),"contentText":x.get("content_text",""),"contentLength":x.get("body_length",0),"failureReason":x.get("reason"),"fetchedAt":x.get("processed_at","")},ensure_ascii=False) for x in vs)+"\n",encoding="utf8")

def archive_run(articles,e):
 by_date=defaultdict(list)
 for article in articles:
  entry=e.get(article.url)
  if entry: by_date[article.run_date].append((article,entry))
 for day,items in by_date.items():
  p=DIR/(day+".jsonl")
  p.write_text("\n".join(json.dumps({"recordType":"article-content","articleKey":entry.get("article_key",hashlib.sha256(article.url.encode()).hexdigest()),"originalUrl":article.url,"resolvedUrl":entry.get("resolved_url"),"contentStatus":entry.get("status","unavailable"),"contentType":entry.get("content_type","unknown"),"contentText":entry.get("content_text",""),"contentLength":entry.get("body_length",0),"failureReason":entry.get("reason"),"fetchedAt":entry.get("processed_at","")},ensure_ascii=False) for article,entry in items)+"\n",encoding="utf8")

def conn(env):
 load_env_file(env);b=normalize_api_base_url(os.environ.get("N8N_API_BASE_URL") or os.environ.get("N8N_BASE_URL") or "");k=os.environ["N8N_API_KEY"];ts=api_request("GET",b,"/data-tables",k).get("data",[]);x=next((x for x in ts if x.get("name")==NAME),None)
 if not x:x=api_request("POST",b,"/data-tables",k,{"name":NAME,"columns":COLS})
 return b,k,x["id"]
def upsert(c,x):
 b,k,t=c;d={n:x.get(m,"") for n,m in [("article_key","article_key"),("original_url","original_url"),("resolved_url","resolved_url"),("source_domain","source_host"),("content_type","content_type"),("content_status","status"),("content_text","content_text"),("content_hash","content_hash"),("extraction_method","extraction_method"),("failure_reason","reason"),("content_path","content_path"),("fetched_at","processed_at")]};d["content_length"]=x["body_length"];api_request("POST",b,f"/data-tables/{t}/rows/upsert",k,{"filter":{"type":"and","filters":[{"columnName":"article_key","condition":"eq","value":d["article_key"]}]},"data":d,"returnData":False})
def main():
 p=argparse.ArgumentParser();p.add_argument("--database",type=Path,default=old.DEFAULT_DATABASE_PATH);p.add_argument("--run-date",help="Capture all articles from one structured-record date, including rows not yet in the articles table.");p.add_argument("--limit",type=int);p.add_argument("--retry-unverified",action="store_true");p.add_argument("--refresh",action="store_true");p.add_argument("--title-pattern",help="Process only articles whose title or excerpt matches this regular expression.");p.add_argument("--exclude-pattern",help="Skip articles whose title or excerpt matches this regular expression.");p.add_argument("--google-delay",type=float,default=2.5);p.add_argument("--publisher-delay",type=float,default=.75);p.add_argument("--max-retries",type=int,default=4);p.add_argument("--env-file",type=Path,default=ROOT/".env");p.add_argument("--no-sync-contents",action="store_true");p.add_argument("--write",action="store_true");a=p.parse_args();db=a.database.expanduser();f=Fetch(a.google_delay,a.publisher_delay,a.max_retries);old.http_bytes=f
 db_arts=old.load_articles(db); record_arts=load_record_articles(a.run_date)
 if a.title_pattern:
  include=re.compile(a.title_pattern,re.IGNORECASE);record_arts={url:article for url,article in record_arts.items() if include.search(article.title+chr(10)+article.excerpt)}
 if a.exclude_pattern:
  exclude=re.compile(a.exclude_pattern,re.IGNORECASE);record_arts={url:article for url,article in record_arts.items() if not exclude.search(article.title+chr(10)+article.excerpt)}
 if a.run_date: arts=sorted(record_arts.values(),key=lambda x:(x.published_at,x.url),reverse=True)
 else:
  arts_by_url={x.url:x for x in db_arts};arts_by_url.update(record_arts);arts=sorted(arts_by_url.values(),key=lambda x:(x.run_date,x.published_at,x.url),reverse=True)
 keys={str(x.get("url","")):str(x.get("article_key","")) for x in old.database_rows(db)};e,ca=load();todo=[x for x in arts if a.refresh or x.url not in e or(a.retry_unverified and e[x.url].get("status")!="verified")][:a.limit];c=None
 if not a.no_sync_contents:
  try:c=conn(a.env_file)
  except Exception as x:print("warning: Data Table sync disabled: "+str(x),file=sys.stderr)
 print(f"articles={len(arts)} cached={len(e)} pending={len(todo)}")
 for i,x in enumerate(todo,1):
  e[x.url]=capture(x,keys.get(x.url,""),f,ca,old.load_keywords());archive_run(record_arts.values(),e);save(e,ca)
  if c:
   try:upsert(c,e[x.url])
   except Exception as z:print("warning: Data Table sync failed: "+str(z),file=sys.stderr)
  print(f"[{i}/{len(todo)}] {e[x.url]['status']}: {x.title[:90]}")
 archive_run(record_arts.values(),e) if a.run_date else archive(e);save(e,ca)
 if a.write:old.write_summaries(arts,e)
if __name__=="__main__":main()
