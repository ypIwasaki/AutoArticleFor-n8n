const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const workflow = JSON.parse(fs.readFileSync(path.join(root, 'n8n/workflows/daily-keyword-news-summary.workflow.json')));
const nodes = Object.fromEntries(workflow.nodes.map(n => [n.name, n]));
const installed = process.env.N8N_INSTALL_ROOT || path.join(require('os').homedir(), '.local/lib/node_modules/n8n');
const {SplitInBatchesV3} = require(path.join(installed, 'node_modules/n8n-nodes-base/dist/nodes/SplitInBatches/v3/SplitInBatchesV3.node.js'));
const {RssFeedRead} = require(path.join(installed, 'node_modules/n8n-nodes-base/dist/nodes/RssFeedRead/RssFeedRead.node.js'));
const Parser = require(path.join(installed, 'node_modules/rss-parser'));
const queries = ['alpha','beta','empty'].map((keyword, i) => ({json:{keyword,query:keyword,sourceName:'Google News',rssUrl:'https://example.test/'+i,keywords:['alpha','beta'],since:'2026-09-17T00:00:00Z',until:'2026-09-17T01:00:00Z'}}));
const attempts = [0,0,0];
Parser.prototype.parseURL = async function(url) {
  const i=Number(url.split('/').pop()); attempts[i]++;
  if(i===1 && attempts[i]===1) {const e=new Error('read ETIMEDOUT');e.code='ETIMEDOUT';throw e;}
  return {items:i===2?[]:[{title:queries[i].json.keyword,link:'https://article.test/'+i,isoDate:'2026-09-17T00:30:00Z',contentSnippet:'sample'}]};
};
const attach = new Function('$input','$','$runIndex',nodes['Attach RSS Search Provenance'].parameters.jsCode);
const normalizeCode = nodes['Normalize and Deduplicate Articles'].parameters.jsCode;
const normalize = new Function('$input','$','Date',normalizeCode);
const oldNormalize = new Function('$input','$','Date',normalizeCode.replace('  if (item.json?._rssQuery) return item.json._rssQuery;\n',''));
class FixedDate extends Date {constructor(v){super(v===undefined?'2026-09-17T01:00:00Z':v);} static now(){return new Date('2026-09-17T01:00:00Z').getTime();}}
(async()=>{
 const state={};let input=queries;let run=0;let all;
 while(true){
  const [done,batch]=await new SplitInBatchesV3().execute.call({getInputData:()=>input,getContext:()=>state,getNodeParameter:n=>n==='batchSize'?1:{},getInputSourceData:()=>({previousNode:'fixture'})});
  if(!batch.length){all=done;break;}
  let output;
  for(let attempt=0;attempt<nodes['Fetch One RSS Feed'].maxTries;attempt++){
   try{[output]=await new RssFeedRead().execute.call({getNode:()=>nodes['Fetch One RSS Feed'],getInputData:()=>batch,getNodeParameter:(n,i)=>n==='url'?batch[i].json.rssUrl:{},continueOnFail:()=>false,helpers:{constructExecutionMetaData:(items,{itemData})=>items.map(item=>({...item,pairedItem:itemData}))}});break;}
   catch(e){if(attempt===2)throw e;}
  }
  if(!output.length)output=[{json:{}}];
  input=attach({all:()=>output},name=>({first:(branch,index)=>{assert.equal(name,'Read RSS Search Results');assert.equal(branch,1);assert.equal(index,run);return batch[0];}}),run);run++;
 }
 assert.deepEqual(attempts,[1,2,1]);assert.equal(all.length,3);
 const oldItems=all.map((item,i)=>({json:Object.fromEntries(Object.entries(item.json).filter(([k])=>k!=='_rssQuery')),pairedItem:{item:i}}));
 const dollar=()=>({all:()=>queries});
 const expected=oldNormalize({all:()=>oldItems},dollar,FixedDate);
 const actual=normalize({all:()=>all},dollar,FixedDate);
 assert.deepEqual(actual,expected);assert.equal(actual[0].json.articles.length,2);
 assert.deepEqual(actual[0].json.articles[1].matchedSearchKeywords,['beta']);
 console.log('RSS retry, empty feed and search provenance passed');
})().catch(e=>{console.error(e);process.exit(1);});
