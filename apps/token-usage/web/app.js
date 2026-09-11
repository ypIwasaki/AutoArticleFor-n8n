import {labels,metrics,aggregate,total,delta,csvCell,comparison} from './model.mjs';
const $=id=>document.getElementById(id), fmt=n=>n==null?'—':new Intl.NumberFormat('ja-JP').format(n);
const compact=n=>n==null?'未計測':new Intl.NumberFormat('ja-JP',{notation:'compact',maximumFractionDigits:2}).format(n);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let reports=[], visible=[];
function options(id,values,selected){$(id).innerHTML=values.map(([v,t])=>`<option value="${esc(v)}">${esc(t)}</option>`).join('');if(values.some(([v])=>v===selected))$(id).value=selected;}
function difference(a,b){const d=delta(a,b);return d?`${d.amount>0?'+':''}${fmt(d.amount)} <small>${d.percent===null?'基準が0のため率なし':`${d.percent>0?'+':''}${d.percent.toFixed(1)}%`}</small>`:'— <small>比較できる観測値なし</small>';}
function number(item){if(!item)return '<span class="muted">記録なし</span>';return `${fmt(item.amount)}${item.amount===null?'<small>未計測</small>':item.missing?'<small>一部未計測</small>':'<small>観測値</small>'}${item.open?'<small>計測中</small>':''}`;}
function render(){
 const base=reports.find(r=>r.workDate===$('baseline').value), current=reports.find(r=>r.workDate===$('current').value), metric=$('metric').value, run=$('run-date').value;
 const a=aggregate(base,metric,run), b=aggregate(current,metric,run), ta=total(a),tb=total(b), unknown=b.get('unassigned')?.amount;
 const c=comparison(base,current,run), signed=n=>`${n>0?'+':''}${fmt(n)}`;
 const tone=d=>!d?'neutral':d.amount<0?'reduced':d.amount>0?'increased':'neutral';
 const descriptions={uncached_input_tokens:'入力からキャッシュ入力を引いた観測値',output_tokens:'生成された出力の観測値',cached_input_tokens:'入力の内数。合計に含まれます',total_tokens:'キャッシュを含む入力＋出力'};
 $('cards').innerHTML=Object.keys(descriptions).map(k=>{const d=c.deltas[k];return `<article class="${tone(d)}"><span>${esc(metrics[k])}</span><strong>${d?.percent!=null?`${d.percent>0?'+':''}${d.percent.toFixed(1)}%`:d?'率を計算できません':'比較できません'}</strong><p class="card-values">${fmt(c.a.values[k].amount)} → ${fmt(c.b.values[k].amount)}</p><b>${d?`${signed(d.amount)} トークン`:'—'}</b><small>${descriptions[k]}</small></article>`;}).join('');
 const input=c.deltas.uncached_input_tokens, output=c.deltas.output_tokens, all=c.deltas.total_tokens;
 let heading='比較できる観測値が不足しています';
 if(input&&output&&all) heading=`非キャッシュ入力は${input.amount<0?'減少':input.amount>0?'増加':'変化なし'}、出力は${output.amount<0?'減少':output.amount>0?'増加':'変化なし'}。合計は${all.amount<0?'減少':all.amount>0?'増加':'変化なし'}しています。`;
 const terms=[['uncached_input_tokens','非キャッシュ入力'],['output_tokens','出力'],['cached_input_tokens','キャッシュ入力']];
 const equation=c.decomposable?`<div class="equation">${terms.map(([k,label])=>`<span><small>${label}</small><b class="${tone(c.deltas[k])}">${signed(c.deltas[k].amount)}</b></span>`).join('<i>＋</i>')}<i>＝</i><span><small>合計の差分</small><b>${signed(all.amount)}</b></span></div>`:'<p>内訳が欠けているため、合計増減の分解はできません。</p>';
 $('effect').innerHTML=`<p class="eyebrow">CHANGE IN OBSERVED USAGE</p><h2>${heading}</h2>${equation}<p class="muted">緑は減少、茶色は増加。観測値の変化であり、対策の効果や費用削減の断定ではありません。</p>`;
 const condition=(label,x,y)=>`<div><span>${label}</span><b>${x} → ${y}</b></div>`;
 $('conditions').innerHTML=`<h2>比較条件も確認</h2><div class="condition-grid">${condition('未計測の行数',c.a.missing,c.b.missing)}${condition('未割当トークン',fmt(c.a.unassigned),fmt(c.b.unassigned))}${condition('記録された工程の試行回数',c.a.attempts,c.b.attempts)}</div><p>いずれも基準日 → 比較日。試行回数は記事数ではありません。${!c.a.rows||!c.b.rows?'選択した記事対象日の記録がない日があります。':'記事数・作業内容を揃えた比較ではありません。'} 未割当は合計に含まれますが、工程別には振り分けできません。</p>`;
 $('selected-metric').textContent=`${metrics[metric]} ｜ ${base.workDate}: ${fmt(ta.amount)} → ${current.workDate}: ${fmt(tb.amount)}（観測分）`;
 $('base-label').textContent=base.workDate; $('current-label').textContent=current.workDate;
 $('base-heading').textContent=base.workDate; $('current-heading').textContent=current.workDate;
 const query=$('search').value.trim().toLowerCase();
 visible=[...new Set([...a.keys(),...b.keys()])].map(step=>({step,a:a.get(step),b:b.get(step)})).filter(r=>(r.step+' '+(labels[r.step]||'')).toLowerCase().includes(query));
 const rank=r=>$('sort').value==='delta'?(delta(r.a?.amount,r.b?.amount)?Math.abs(delta(r.a?.amount,r.b?.amount).amount):-Infinity):r.b?.amount??-Infinity;
 visible.sort((x,y)=>$('sort').value==='name'?(labels[x.step]||x.step).localeCompare(labels[y.step]||y.step,'ja'):rank(y)-rank(x)||(labels[x.step]||x.step).localeCompare(labels[y.step]||y.step,'ja'));
 const max=Math.max(1,...visible.flatMap(r=>[r.a?.amount||0,r.b?.amount||0]));
 const bar=(item,color)=>`<div class="track"><div class="bar ${color}" style="width:${(item?.amount||0)/max*100}%"></div></div>`;
 $('rows').innerHTML=visible.map(r=>`<tr${r.step==='unassigned'?' class="unassigned"':''}><th scope="row">${esc(labels[r.step]||r.step)}<small>${esc(r.step)}</small></th><td class="bars" aria-label="基準 ${fmt(r.a?.amount)}、比較 ${fmt(r.b?.amount)}">${bar(r.a,'base')}${bar(r.b,'current')}</td><td class="numeric">${number(r.a)}</td><td class="numeric">${number(r.b)}</td><td class="numeric">${difference(r.a?.amount,r.b?.amount)}</td><td class="numeric">${r.a?.attempts??'—'} → ${r.b?.attempts??'—'}</td></tr>`).join('');
 $('empty').hidden=visible.length!==0;
 const history=reports.map(r=>({date:r.workDate,hasRows:aggregate(r,metric,run).size>0,...total(aggregate(r,metric,run))})),hmax=Math.max(1,...history.map(h=>h.amount||0));
 $('history').innerHTML=history.map(h=>`<button class="history-item ${h.date===current.workDate?'selected':''}" data-date="${esc(h.date)}" aria-label="${esc(h.date)}を比較日に選択、${fmt(h.amount)}トークン"><span>${esc(h.date)}${h.date===base.workDate?' · 基準':''}${h.date===current.workDate?' · 比較':''}</span><div class="history-track"><i style="width:${(h.amount||0)/hmax*100}%"></i></div><b>${h.hasRows?compact(h.amount):"記録なし"}</b><small>${h.hasRows?`未計測 ${h.missing} 行`:"対象記録なし"}</small></button>`).join('');
 $('history').querySelectorAll('button').forEach(el=>el.onclick=()=>{$('current').value=el.dataset.date;render();});
 $('metadata').textContent=`記録 ${reports.length} 日分 ｜ 基準の集計日時: ${base.generatedAt||'不明'} ｜ 比較の集計日時: ${current.generatedAt||'不明'}`;
 const params=new URLSearchParams({base:base.workDate,current:current.workDate,metric,run}); window.history.replaceState(null,'','?'+params);
}
async function load(){
 $('reload').disabled=true;$('status').hidden=false;$('status').textContent='記録を読み込んでいます…';
 try{
 const response=await fetch('/api/reports',{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);
 const data=await response.json();reports=data.reports.sort((a,b)=>a.workDate.localeCompare(b.workDate));
 if(!reports.length){$('content').hidden=true;$('status').textContent='比較できる記録がありません。content/operation-usage に作業日別JSONを保存して再読み込みしてください。'+(data.warnings||[]).join(' / ');return;}
 const p=new URLSearchParams(location.search), dates=reports.map(r=>[r.workDate,r.workDate]);
 options('baseline',dates,p.get('base')||reports.at(-2)?.workDate||reports.at(-1).workDate);options('current',dates,p.get('current')||reports.at(-1).workDate);
 options('metric',Object.entries(metrics),p.get('metric')||'uncached_input_tokens');
 const runs=[...new Set(reports.flatMap(r=>r.rows.map(row=>row.runDate)))].filter(Boolean).sort();
 options('run-date',[['all','すべての対象日'],...runs.map(d=>[d,d])],p.get('run')||'all');
 $('status').textContent=(data.warnings||[]).join(' / ');$('status').hidden=!data.warnings?.length;$('content').hidden=false;render();
 }catch(error){$('status').textContent=`記録を読み込めませんでした。サーバーの起動を確認し、再読み込みしてください。(${error.message})`;$('content').hidden=true;}
 finally{$('reload').disabled=false;}
}
['baseline','current','metric','run-date','sort'].forEach(id=>$(id).addEventListener('change',render));$('search').addEventListener('input',render);
$('swap').onclick=()=>{const x=$('baseline').value;$('baseline').value=$('current').value;$('current').value=x;render();};$('reload').onclick=load;
$('export').onclick=()=>{
 const rows=[['工程','工程ID','指標','基準作業日','比較作業日','記事対象日','基準値','比較値','差分','基準の未計測行数','比較の未計測行数'],...visible.map(r=>[labels[r.step]||r.step,r.step,metrics[$('metric').value],$('baseline').value,$('current').value,$('run-date').value,r.a?.amount,r.b?.amount,delta(r.a?.amount,r.b?.amount)?.amount,r.a?.missing??'',r.b?.missing??''])];
 const blob=new Blob(['\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8;'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`token-comparison-${$('baseline').value}-${$('current').value}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
load();

