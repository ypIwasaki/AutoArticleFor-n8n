export const labels = {
 prepare:'準備',n8n:'n8n起動',collect:'収集',capture:'本文取得','shared-review':'共通本文確認',
 summary:'記事要約','talent-review':'人材索引','classification-review':'記事分類',keywords:'キーワード',
 weekly:'週次レポート',apply:'DB反映','apply-talent':'人材DB反映','apply-classification':'分類DB反映',
 dashboard:'確認アプリ起動',page:'画面確認',preflight:'実行前チェック',investigation:'障害調査',report:'報告',common:'共通作業',unassigned:'未割当'
};
export const metrics = {total_tokens:'合計トークン',input_tokens:'入力',output_tokens:'出力',uncached_input_tokens:'非キャッシュ入力',cached_input_tokens:'キャッシュ入力（内数）',reasoning_output_tokens:'推論出力（内数）'};
const valid = v => typeof v === 'number' && Number.isFinite(v) && v >= 0;
export function value(row, metric) {
 const t = row.tokens || {};
 if(metric === 'uncached_input_tokens') return valid(t.input_tokens) && valid(t.cached_input_tokens) && t.input_tokens >= t.cached_input_tokens ? t.input_tokens-t.cached_input_tokens : null;
 return valid(t[metric]) ? t[metric] : null;
}
export function aggregate(report, metric, runDate='all') {
 const result = new Map();
 for(const row of report?.rows || []) {
  if(runDate !== 'all' && row.runDate !== runDate) continue;
  const item = result.get(row.step) || {step:row.step,amount:null,missing:0,attempts:0,open:false,rows:0};
  const amount=value(row,metric);
  if(amount === null) item.missing++; else item.amount=(item.amount??0)+amount;
  item.attempts+=row.attempts||0; item.open ||= !!row.open; item.rows++;
  result.set(row.step,item);
 }
 return result;
}
export function total(items) {
 let amount=null, missing=0;
 for(const item of items.values()) { if(item.amount !== null) amount=(amount??0)+item.amount; missing+=item.missing; }
 return {amount,missing};
}
export function delta(a,b) {
 if(a == null || b == null) return null;
 return {amount:b-a,percent:a===0?null:(b-a)/a*100};
}
export function csvCell(value) {
 let text=String(value ?? '');
 if(/^[=+@\-]/.test(text)) text="'"+text;
 return '"'+text.replaceAll('"','""')+'"';
}
export function comparison(base, current, runDate='all') {
 const keys=['uncached_input_tokens','output_tokens','cached_input_tokens','total_tokens'];
 const stats=report=>{
  const rows=(report?.rows||[]).filter(r=>runDate==='all'||r.runDate===runDate);
  const values=Object.fromEntries(keys.map(k=>[k,total(aggregate(report,k,runDate))]));
  const complete=rows.filter(r=>keys.every(k=>value(r,k)!==null));
  const observed=rows.filter(r=>keys.some(k=>value(r,k)!==null));
  const unassigned=aggregate(report,'total_tokens',runDate).get('unassigned')?.amount??null;
  return {values,rows:rows.length,missing:values.total_tokens.missing,unassigned,
   attempts:rows.reduce((n,r)=>n+(r.attempts||0),0),
   decomposable:complete.length>0 && complete.length===observed.length && complete.every(r=>value(r,'total_tokens')===value(r,'uncached_input_tokens')+value(r,'cached_input_tokens')+value(r,'output_tokens'))};
 };
 const a=stats(base),b=stats(current);
 return {a,b,deltas:Object.fromEntries(keys.map(k=>[k,delta(a.values[k].amount,b.values[k].amount)])),decomposable:a.decomposable&&b.decomposable};
}
