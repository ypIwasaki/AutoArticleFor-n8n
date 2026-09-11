import assert from 'node:assert/strict';
import {aggregate,total,delta,value,csvCell} from './web/model.mjs';
const report={rows:[
 {runDate:'2026-09-11',step:'capture',attempts:1,tokens:{total_tokens:120,input_tokens:100,cached_input_tokens:80,output_tokens:20}},
 {runDate:'2026-09-11',step:'capture',attempts:1,tokens:{total_tokens:null}},
 {runDate:'2026-09-10',step:'capture',attempts:1,tokens:{total_tokens:30}},
 {runDate:'2026-09-11',step:'unassigned',tokens:{total_tokens:0}}
]};
const all=aggregate(report,'total_tokens');
assert.equal(all.get('capture').amount,150);assert.equal(all.get('capture').missing,1);
assert.deepEqual(total(all),{amount:150,missing:1});
assert.equal(aggregate(report,'total_tokens','2026-09-11').get('capture').amount,120);
assert.equal(aggregate(report,'output_tokens').get('unassigned').amount,null);
assert.equal(value(report.rows[0],'uncached_input_tokens'),20);
assert.equal(value({tokens:{input_tokens:2,cached_input_tokens:3}},'uncached_input_tokens'),null);
assert.equal(delta(null,3),null);assert.deepEqual(delta(0,3),{amount:3,percent:null});
assert.deepEqual(delta(100,80),{amount:-20,percent:-20});
assert.deepEqual(total(new Map()),{amount:null,missing:0});
assert.equal(csvCell('a"b'),'"a""b"');assert.equal(csvCell('=1+2'),'"\'=1+2"');
console.log('PASS: missing/zero, partial totals, date scope, nested counts, deltas, CSV');
const {comparison}=await import('./web/model.mjs');
const fixture=(input,cache,output)=>({rows:[{step:'summary',tokens:{input_tokens:input,cached_input_tokens:cache,output_tokens:output,total_tokens:input+output}}]});
const c=comparison(fixture(100,60,20),fixture(110,90,10));
assert.equal(c.deltas.uncached_input_tokens.amount,-20);
assert.equal(c.deltas.cached_input_tokens.amount,30);
assert.equal(c.deltas.output_tokens.amount,-10);
assert.equal(c.deltas.total_tokens.amount,0);
assert.equal(c.decomposable,true);
assert.equal(comparison(report,fixture(100,60,20)).decomposable,false);
assert.equal(comparison({rows:[]},fixture(0,0,0)).decomposable,false);
assert.equal(comparison(fixture(0,0,0),fixture(1,0,0)).deltas.total_tokens.percent,null);
const {readFileSync}=await import('node:fs');
const daily=d=>JSON.parse(readFileSync(new URL(`../../content/operation-usage/${d}.json`,import.meta.url),'utf8').replace(/^\uFEFF/,''));
const actual=comparison(daily('2026-09-10'),daily('2026-09-11'));
assert.equal(actual.deltas.uncached_input_tokens.amount,-295936);
assert.equal(actual.deltas.output_tokens.amount,-9284);
assert.equal(actual.deltas.cached_input_tokens.amount,449792);
assert.equal(actual.deltas.total_tokens.amount,144572);
assert.equal(actual.decomposable,true);
console.log('PASS: change decomposition, missing breakdowns, zero baseline, actual day comparison');
