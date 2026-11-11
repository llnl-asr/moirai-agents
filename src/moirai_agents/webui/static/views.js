// The Pipeline and History tabs. Loaded after moirai.js; uses its globals ($, esc, api, md, session, running, PIPES,
// shown, stateRender, STEP_WORD, when, DASH_VIEWS, dash, showTab, hms, dur).

// ---- Pipeline: the whole pipeline at a glance (every step, what it does, what happened), then one step in detail.
const PV_TONE={passed:'ok',failed:'bad',running:'run',needs_user:'run',skipped:'ok'};
let PV_PID=null;
function pipeRender(){const v=$('#pipeview');if(!v||v.hidden)return;
 if(!PIPES.length){v.innerHTML=`<div class="pv-empty"><h2>No pipeline in this chat</h2><p>Switch the chat to <b>Run</b> (annotate and run an application) or <b>Analysis</b> (explore traces you have) and ask for it; the pipeline and every step appear here.</p></div>`;return}
 const p=PIPES.find(x=>x.id===PV_PID)||PIPES[0];PV_PID=p.id;
 if(!shown||shown.pid!==p.id)shown={pid:p.id,stage:p.current||p.stages[0].name};
 const done=p.stages.filter(s=>s.status==='passed'||s.status==='skipped').length;
 const sel=PIPES.length>1?`<select id="pvsel" aria-label="pipeline">${PIPES.map(x=>`<option value="${esc(x.id)}" ${x.id===p.id?'selected':''}>${esc(x.name)} · ${esc(x.kind)}</option>`).join('')}</select>`:`<b>${esc(p.name)}</b> <span class="sub">${esc(p.kind)}</span>`;
 const rows=p.stages.map((s,i)=>{const tone=PV_TONE[s.status]||'',cur=s.name===p.current,on=s.name===shown.stage;
  const pg=s.progress&&s.progress.running&&s.progress.now?`<div class="pv-now">⏳ ${esc(s.progress.now.what||'')}</div>`:'';
  const dl=s.run?`<span class="pv-links">${DASH_VIEWS.map(([k,l])=>`<a href="#dashboard" data-run="${esc(s.run)}" data-view="${k}" data-label="${esc(s.name+' of '+p.name)}">${l}</a>`).join(' · ')}</span>`:'';
  return `<div role="button" tabindex="0" class="pv-step ${tone}${cur?' cur':''}${on?' sel':''}" data-stage="${esc(s.name)}">
   <span class="pv-n">${s.status==='passed'?'✓':s.status==='failed'?'✗':i+1}</span>
   <div class="pv-b"><div class="pv-h"><b>${esc(s.name)}</b><span class="pv-st">${esc(STEP_WORD[s.status]||s.status)}${s.verdict?' · checks '+esc(s.verdict):''}</span></div>
    ${s.plan?`<div class="pv-does">${esc(s.plan)}${s.plan_stale?' <b class="stale">plan out of date</b>':''}</div>`:''}${s.summary?`<div class="pv-sum">${esc(s.summary)}</div>`:''}${pg}
    ${(s.failed||[]).length?`<div class="pv-fail">${s.failed.length} check${s.failed.length>1?'s':''} to look at</div>`:''}${s.status==='needs_user'?'<div class="pv-wait">waiting for you</div>':''}${s.feedback_open?`<div class="pv-wait">${s.feedback_open} feedback open</div>`:''}${dl}</div></div>`}).join('');
 const head=`<div class="pv-head">${sel}<span class="sub">${done} of ${p.stages.length} steps done${p.updated?' · updated '+esc(when(p.updated)):''}</span></div>${p.goal?`<div class="pv-goal">${esc(p.goal)}</div>`:''}`;
 const html=`${head}<div class="pv-grid"><div class="pv-list">${rows}</div><div class="pv-detail" id="pvdetail"></div></div>`;
 if(v.dataset.h!==html){const keep=$('#pvdetail');v.dataset.h=html;v.innerHTML=html;if(keep)$('#pvdetail').replaceWith(keep)}
 const s=$('#pvsel');if(s)s.onchange=e=>{PV_PID=e.target.value;shown=null;pipeRender()};
 v.querySelectorAll('.pv-step').forEach(a=>a.onclick=e=>{if(e.target.closest('[data-run]'))return;e.preventDefault();shown={pid:p.id,stage:a.dataset.stage};delete v.dataset.h;pipeRender()});
 v.querySelectorAll('[data-run]').forEach(a=>a.onclick=e=>{e.preventDefault();e.stopPropagation();dash.run=a.dataset.run;dash.view=a.dataset.view;dash.label=a.dataset.label;showTab('dash')});
 stateRender()}

// ---- History: what exactly happened in this chat, from its provenance. Compact: one line per turn; open a turn for
// its tool calls (one line each), open a call for its full input and result.
let HX={sid:null,data:null,open:new Set(),calls:{},busy:false};
const hTime=t=>t?new Date(t*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'}):'';
const hDur=s=>s==null?'':(typeof dur==='function'?dur(s):s+'s');
async function histRender(force){const v=$('#histview');if(!v||v.hidden||HX.busy)return;
 if(!session){v.innerHTML='<div class="pv-empty"><h2>No chat open</h2><p>Open a chat from the left bar to see what happened in it.</p></div>';HX.sid=null;return}
 if(HX.sid!==session){HX={sid:session,data:null,open:new Set(),calls:{},busy:false};v.innerHTML='<div class="sub" style="padding:16px">loading…</div>'}
 HX.busy=true;let d;try{d=await api('api/history?session='+encodeURIComponent(session))}finally{HX.busy=false}
 if(!d||d.error){v.innerHTML=`<div class="pv-empty">${esc((d&&d.error)||'not available')}</div>`;return}
 if(HX.sid!==session)return;HX.data=d;histDraw()}
function histCall(c,depth){const st=c.end==null?'run':c.is_error?'bad':'ok';
 const full=HX.calls[c.id];const open=HX.open.has('c:'+c.id);
 return `<details class="hc" data-k="c:${esc(c.id)}" data-call="${esc(c.id)}" style="margin-left:${depth*16}px" ${open?'open':''}><summary><span class="dot ${st}"></span><span class="ht-t">${esc(hms(c.ts))}</span><b>${esc(c.name)}</b><span class="hc-d" title="${esc(c.desc)}">${esc(c.desc)}</span><span class="ht-t">${c.secs>=0.1?hDur(c.secs):''}</span></summary>
  <div class="hc-body">${full?(full.error?esc(full.error):`<div class="k">input</div><pre>${esc(full.input)}</pre>${full.input_cut?'<div class="sub">(cut)</div>':''}<div class="k">${full.is_error?'error':'result'}</div><pre class="${full.is_error?'del':''}">${esc(full.result||'(no output)')}</pre>${full.result_cut?'<div class="sub">(cut)</div>':''}`):`<div class="sub">loading…</div>`}</div></details>`}
function histDraw(){const v=$('#histview'),d=HX.data;if(!d)return;const T=d.turns||[];
 const ncalls=T.reduce((a,t)=>a+t.calls.length,0),secs=T.reduce((a,t)=>a+(t.duration_s||0),0),cost=T.reduce((a,t)=>a+(t.cost_usd||0),0),errs=T.reduce((a,t)=>a+t.calls.filter(c=>c.is_error).length,0);
 const turns=T.map((t,i)=>{const open=HX.open.has('t:'+t.turn_id),dot=t.status==='ok'?'ok':t.status==='running'?'run':'bad';
  const kids={};t.calls.forEach(c=>{if(c.parent)(kids[c.parent]=kids[c.parent]||[]).push(c)});
  const tree=(c,dp)=>histCall(c,dp)+(kids[c.id]||[]).map(k=>tree(k,dp+1)).join('');
  const calls=t.calls.filter(c=>!c.parent||!t.calls.some(x=>x.id===c.parent)).map(c=>tree(c,0)).join('');
  const ne=t.calls.filter(c=>c.is_error).length;
  return `<details class="ht" data-k="t:${esc(t.turn_id)}" ${open?'open':''}><summary><span class="dot ${dot}"></span><span class="ht-t">${esc(hTime(t.ts))}</span><span class="ht-m" title="${esc(t.message)}">${i+1}. ${esc((t.message||'').replace(/\s+/g,' ').slice(0,140))}</span>
   <span class="ht-t">${t.calls.length} call${t.calls.length===1?'':'s'}${ne?` · <span class="bad">${ne} failed</span>`:''}${t.agents?` · ${t.agents} agent${t.agents>1?'s':''}`:''}${t.duration_s!=null?' · '+hDur(t.duration_s):''}${t.cost_usd?' · $'+t.cost_usd.toFixed(2):''}</span></summary>
   <div class="ht-body"><div class="sub">${esc(t.mode||'')} · ${esc(t.model||'')} · ${esc(t.status||'')}${t.num_turns?` · ${t.num_turns} model turns`:''}</div>
   ${(t.steers||[]).map(x=>`<div class="ht-steer">↳ ${esc(hms(x.ts))} you: ${esc(x.text||'')}</div>`).join('')}
   ${(t.errors||[]).map(x=>`<div class="pv-fail">${esc(x.slice(0,400))}</div>`).join('')}
   ${calls||'<div class="sub">no tool calls</div>'}</div></details>`}).join('');
 const html=`<div class="ht-head"><h2>History</h2><span class="sub">${T.length} turn${T.length===1?'':'s'} · ${ncalls} tool calls${errs?` · ${errs} failed`:''}${secs?' · '+hDur(secs):''}${cost?' · $'+cost.toFixed(2):''}</span>
  <span class="ht-acts"><a href="#" data-h="open">open all turns</a> · <a href="#" data-h="close">collapse</a></span></div>
  <div class="sub" style="margin-bottom:8px">Every turn of this chat from its provenance record: open a turn for its tool calls, open a call for its exact input and result.</div>${turns||'<div class="sub">nothing recorded yet</div>'}`;
 if(v.dataset.h===html)return;const y=v.scrollTop;v.dataset.h=html;v.innerHTML=html;v.scrollTop=y;
 v.querySelectorAll('details[data-k]').forEach(e=>e.addEventListener('toggle',ev=>{if(ev.target!==e)return;const k=e.dataset.k;
  if(e.open){HX.open.add(k);if(e.dataset.call&&!HX.calls[e.dataset.call])histLoadCall(e.dataset.call)}else HX.open.delete(k)}));
 v.querySelectorAll('[data-h]').forEach(a=>a.onclick=ev=>{ev.preventDefault();
  if(a.dataset.h==='open')(HX.data.turns||[]).forEach(t=>HX.open.add('t:'+t.turn_id));else HX.open.clear();delete v.dataset.h;histDraw()})}
async function histLoadCall(id){const sid=HX.sid,d=await api(`api/history?session=${encodeURIComponent(sid)}&call=${encodeURIComponent(id)}`);
 if(HX.sid!==sid)return;HX.calls[id]=d||{error:'not available'};delete $('#histview').dataset.h;histDraw()}
setInterval(()=>{if(document.visibilityState!=='visible')return;const h=$('#histview');if(h&&!h.hidden&&(running||HX.sid!==session))histRender()},5000);
