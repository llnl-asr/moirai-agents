// The three views, for both roles: Annotate (annotate the app, diff what it promised against what the baseline recorded,
// publish the code), Live Run (a long run with its chat on the left and the provenance graph forming live on the right,
// then publish the data) and Query (lineage and downstream impact with evidence synthesis, while a run goes or after).
// Each view: its list on the far left, its chat, then what it is about. The domain scientist (Settings -> Role) sees the
// compact version; the provenance scientist also sees every stage's checks, jobs and the audit trail, and keeps the
// Chat, Pipeline, History and Dashboard tabs. Loaded after moirai.js; uses its globals ($, esc, api, md, session, running,
// openSession, sendText, setMode, showTab, dgNeedGraph, themeNow, dur).
const WB={role:'studio',view:'ann',items:{apps:[],lives:[],datasets:[]},sel:null,answers:[],pins:[],card:null,timer:null,lgmode:'agg',stage:null};
const WB_STATE={running:['run','running'],working:['run','agent working'],needs_you:['wait','needs you'],failed:['bad','failed'],done:['ok','done'],idle:['','idle']};
const STEP_L={plan:'Plan',explore:'Explore the app',smoke:'Test run',discover:'Find the data',annotate:'Annotate',integrate:'Hook up moirai',moirai_smoke:'Test with provenance',
 baseline:'Baseline run',graph:'Check the diagram',package:'Publish the code',live_run:'Live run',publish_data:'Publish the data',ingest:'Read the data',dashboard:'Dashboard',query:'Query'};
const VIEW_L={ann:'Annotate',live:'Live Run',query:'Query'};
const fmtBytes=b=>{if(b==null)return '-';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return (i?b.toFixed(b<10?1:0):b)+' '+u[i]};
const ago=t=>t?dur(Math.max(0,Date.now()/1000-t))+' ago':'';
const pro=()=>WB.role!=='domain';
try{WB.view=localStorage.getItem('moirai_view')||'ann';if(!VIEW_L[WB.view])WB.view='ann'}catch(_){}

// ---- role (Settings): the same views; the domain scientist's are compact
function setRole(r){WB.role=r==='domain'?'domain':'studio';try{localStorage.setItem('moirai_role',WB.role)}catch(_){}
 document.body.classList.toggle('wb',WB.role==='domain');
 document.querySelectorAll('#roleseg button').forEach(b=>b.classList.toggle('on',b.dataset.r===WB.role));
 const n=$('#rolenote');if(n)n.textContent=WB.role==='domain'?'the three views, compact':'the three views with every detail, plus chat, pipeline and history';
 if(WB.role==='domain'){setView('default');if(!document.body.classList.contains('views'))wbShow(WB.view)}
 else if(document.body.classList.contains('views')&&WB.sel){delete $('#wbview').dataset.h;wbTick(true)}
 if(typeof statusLabel==='function')statusLabel()}
document.querySelectorAll('#roleseg button').forEach(b=>b.onclick=()=>setRole(b.dataset.r));

// ---- the view tabs (they share #wbview and dock the chat on the left)
const _showTab0=showTab;
showTab=function(t){_showTab0(t);const on=t==='wb';document.body.classList.toggle('views',on);
 if(on){$('#chatview').hidden=false;$('#tabwb').classList.remove('on')}
 for(const k of Object.keys(VIEW_L))$('#tv'+k).classList.toggle('on',on&&WB.view===k)};
// One app is selected on the left and stays selected across the three views: Annotate shows the app, Live Run its live runs, Query its
// baseline or one of its runs (switchers in the view's header). A dataset is selected the same way and answers in Query.
try{WB.app=localStorage.getItem('moirai_app')||null;WB.pick=JSON.parse(localStorage.getItem('moirai_pick')||'null')||{live:{},query:{}}}catch(_){WB.pick={live:{},query:{}}}
const savePick=()=>{try{localStorage.setItem('moirai_app',WB.app||'');localStorage.setItem('moirai_pick',JSON.stringify(WB.pick))}catch(_){}};
function wbShow(v){v=VIEW_L[v]?v:'ann';WB.view=v;try{localStorage.setItem('moirai_view',v)}catch(_){}
 try{history.replaceState(null,'','#'+{ann:'annotate',live:'live',query:'query'}[v])}catch(_){}
 WB.sel=null;clearInterval(WB.timer);showTab('wb');wbLoad().then(wbOpenView)}
for(const k of Object.keys(VIEW_L)){const b=$('#tv'+k);if(b)b.onclick=()=>wbShow(k)}
function appOf(id){const x=wbItem(id);return x?(x.kind==='live'||x.published_run?x.parent:x.id):null}
function livesOf(app){return WB.items.lives.filter(l=>l.parent===app).sort((a,b)=>(b.created||0)-(a.created||0))}
// go to a view on a given target (an app, a live run or a dataset): its app becomes the selected one, the target that view's pick
function goView(view,target){const app=appOf(target);if(app){WB.app=app;if(view!=='ann'&&target)WB.pick[view][app]=target;savePick()}wbShow(view)}
function targetFor(v){const it=WB.app&&wbItem(WB.app);if(!it)return null;
 if(it.kind==='analysis')return v==='query'?it.id:null;
 if(v==='ann')return it.id;
 const lv=livesOf(it.id),p=WB.pick[v][it.id];
 if(v==='live')return p&&lv.some(x=>x.id===p)?p:(lv[0]||{}).id||null;
 return p&&(p===it.id||lv.some(x=>x.id===p)||(WB.items.published||[]).some(x=>x.id===p&&x.parent===it.id))?p:(lv[0]||{}).id||it.id}
async function wbOpenView(){if(!document.body.classList.contains('views'))return;
 document.querySelectorAll('#wblist .wbi').forEach(e=>e.classList.toggle('on',e.dataset.id===WB.app));
 const it=WB.app&&wbItem(WB.app);
 if(!it)return wbHome();
 const t=targetFor(WB.view);
 if(!t)return wbHome(it.kind==='analysis'?'dataset':'live');
 return wbOpen(t)}
// the switcher in a view's header: which run of the selected app
function targetBar(){const it=WB.app&&wbItem(WB.app);if(!it||it.kind==='analysis'||WB.view==='ann')return '';
 const lv=livesOf(it.id),cur=WB.sel&&WB.sel.id,chip=(x,label)=>{const [cls,word]=WB_STATE[x.state]||['',x.state];
  return `<button class="wb-tg${cur===x.id?' on':''}" data-target="${esc(x.id)}"><span class="wbd ${cls}"></span>${esc(label||x.name)} <span class="sub">${esc(word)}</span></button>`};
 const chips=(WB.view==='query'?chip(it,'current baseline'):'')+lv.map(x=>chip(x)).join('')+(WB.view==='live'?'<button class="wb-tg new" data-newlive="1">+ new live run</button>':'');
 const vs=WB.view==='query'?(it.versions||[]):[];
 const pub=vs.map(r=>{const day=r.ts?r.ts.replace(/^(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d).*/,'$1-$2-$3 $4:$5'):'';
  const tip=[`${r.name}: ${r.kind}`,day&&'published run of '+day,r.scale&&'scale '+r.scale,r.input&&'input '+r.input,r.commit&&'app commit '+r.commit,r.same_as&&`the same run as ${r.same_as}, published again`,r.note].filter(Boolean).join('\n');
  if(!r.path)return `<button class="wb-tg pub off" disabled title="${esc(tip)}">${esc(r.label)} <span class="sub">${esc(r.note||'')}</span></button>`;
  const on=r.pipeline&&cur===r.pipeline;
  return `<button class="wb-tg pub${on?' on':''}" ${r.pipeline?`data-target="${esc(r.pipeline)}"`:`data-pub="${esc(r.path)}" data-app="${esc(it.id)}"`} title="${esc(tip)}">${esc(r.label)} <span class="sub">${r.same_as?'= '+esc(r.same_as)+' ':''}${esc(day.slice(5,10))}</span></button>`}).join('');
 return `<div class="wb-targets"><span class="sub">${WB.view==='live'?'Live runs of':'Ask about'} <b>${esc(it.name)}</b>:</span>${chips}</div>`+
  (pub?`<div class="wb-targets wb-pubs"><span class="sub">Published baselines:</span>${pub}</div>`:'')}
$('#wbview').addEventListener('click',async e=>{const pb=e.target.closest('[data-pub]');
 if(pb){e.preventDefault();pb.disabled=true;pb.querySelector('.sub').textContent='opening…';
  const r=await api('api/workbench/published',{app:pb.dataset.app,path:pb.dataset.pub});
  if(!r||r.error){pb.disabled=false;pb.querySelector('.sub').textContent='';alert((r&&r.error)||'could not open it');return}
  await wbLoad();WB.pick.query[pb.dataset.app]=r.pipeline;savePick();wbOpen(r.pipeline);return}
 const t=e.target.closest('[data-target]');if(t){e.preventDefault();const app=appOf(t.dataset.target);if(app){WB.pick[WB.view][app]=t.dataset.target;savePick()}wbOpen(t.dataset.target);return}
 if(e.target.closest('[data-newlive]')){e.preventDefault();wbHome('live')}});

// ---- the list on the left: the apps (and datasets); the selected one stays selected across the views
async function wbLoad(){if(!me)return;const d=await api('api/workbench');if(!d||d.error)return;WB.items=d;renderNow(d);
 if(!document.body.classList.contains('views'))return;
 if(!WB.app&&session){const it=[...d.apps,...d.lives,...d.datasets,...(d.published||[])].find(x=>x.chat===session||x.query_chat===session);if(it){WB.app=appOf(it.id);savePick()}}
 if(WB.app&&!wbItem(WB.app)){WB.app=null;savePick()}
 const row=x=>{const [cls,word]=WB_STATE[x.state]||['',x.state],lv=livesOf(x.id),run=lv.filter(l=>l.state==='running').length,og=(x.gaps||[]).filter(g=>g.status==='open').length;
  return `<div class="wbi ${WB.app===x.id?'on':''}" data-id="${esc(x.id)}" title="${esc(x.id)}"><span class="wbd ${cls}"></span><span class="t">${esc(x.name)}</span><span class="s">${esc(word)}${lv.length?` · ${lv.length} run${lv.length>1?'s':''}${run?` (${run} live)`:''}`:''}${og?` · ${og} gap${og>1?'s':''}`:''}</span></div>`};
 $('#wblist').innerHTML=`<div class="shead">Apps<a data-new="app">+ annotate an app</a></div>${d.apps.map(row).join('')||'<div class="wbe">none yet</div>'}
  <div class="shead">Datasets<a data-new="data">+ open data</a></div>${d.datasets.map(row).join('')||'<div class="wbe">none yet</div>'}`;
 $('#wblist').querySelectorAll('.wbi').forEach(e=>e.onclick=()=>{WB.app=e.dataset.id;savePick();const it=wbItem(WB.app);if(it&&it.kind==='analysis'&&WB.view!=='query')return wbShow('query');wbOpenView()});
 $('#wblist').querySelectorAll('[data-new]').forEach(a=>a.onclick=e=>{e.preventDefault();WB.app=null;savePick();WB.sel=null;
  document.querySelectorAll('#wblist .wbi').forEach(x=>x.classList.remove('on'));if(a.dataset.new==='data'&&WB.view!=='query'){WB.view='query';showTab('wb')}wbHome(a.dataset.new)});
 if(WB.sel){const it=wbItem(WB.sel.id);if(it)WB.sel.item=it}}
function wbPoolOf(v){const d=WB.items;return v==='query'?[...d.apps,...d.datasets]:d.apps}
function wbItem(id){const d=WB.items;return [...d.apps,...d.lives,...d.datasets,...(d.published||[])].find(x=>x.id===id)}

// ---- the home of each view: what it is for, and how to start
function wbHome(what){WB.sel=null;clearInterval(WB.timer);const v=$('#wbview');v.dataset.home='1';delete v.dataset.h;
 const d=WB.items,sel=WB.app&&wbItem(WB.app),apps=d.apps.filter(a=>a.can_live&&(!sel||sel.kind!=='run'||a.id===sel.id));
 if(what==='dataset'){v.innerHTML=`<div class="wb-home"><h2>${esc(sel?sel.name:'Dataset')}</h2><p class="sub">This is a dataset: provenance someone else recorded. It has no annotation and no live runs of its own; ask about it in the Query view.</p><button class="btn primary" id="wbtoq">Query it</button></div>`;
  $('#wbtoq').onclick=()=>wbShow('query');return}
 if(sel&&sel.kind==='run'&&WB.view==='live'&&!sel.can_live){v.innerHTML=`<div class="wb-home"><h2>${esc(sel.name)}</h2><p class="sub">A live run uses the app's annotation: finish annotating it first (its integrate stage).</p><button class="btn primary" id="wbtoa">Annotate it</button></div>`;
  $('#wbtoa').onclick=()=>wbShow('ann');return}
 const forms={
  ann:`<form class="wb-start" id="wbapp"><h3>✎ Annotate an app</h3><p class="sub">The agent explores the app, proposes what to record, annotates it, runs a baseline and shows you what the annotation promised next to what was recorded. Then it publishes the code.</p>
    <label>Application<input name="src" placeholder="app directory or git URL" required></label>
    <label>Small test command<input name="cmd" placeholder="e.g. python run.py --config small.json" required></label>
    <label>Name<input name="name" placeholder="short name, e.g. biotox" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,40}"></label>
    <button class="btn primary">Start</button></form>`,
  live:`<form class="wb-start" id="wblive"><h3>▶ Start a live run</h3><p class="sub">Run an annotated app for real, for as long as it takes (hours are fine). Its provenance graph forms beside the chat while it runs; at the end the data is published.</p>
    ${apps.length?`<label>App<select name="parent" required>${apps.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}${a.published?' · code published':''}</option>`).join('')}</select></label>
    <div id="wbli" class="wb-li"></div>
    <label>Name<input name="name" placeholder="optional, e.g. biotox-nov8" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,40}"></label>
    <div class="wb-row"><label>Time limit (hours)<input name="hours" type="number" min="0.1" step="0.5" value="12"></label><label>Queue<input name="queue" placeholder="pbatch"></label></div>
    <div class="wb-sec">Command</div><code class="wb-cmd" id="wblcmd">-</code>
    <button class="btn primary">Start live run</button><div class="err" id="wblerr"></div>`:'<p class="err">No annotated app yet: annotate one in the Annotate view first.</p>'}</form>`,
  data:`<form class="wb-start" id="wbdata"><h3>🔎 Open provenance data</h3><p class="sub">Provenance traces anywhere you can read them: a folder, a .pfw.gz file or a glob. Each analysis can use a different place.</p>
    <label>Traces<input name="path" placeholder="/p/lustre…/my-run/traces or …/*.pfw.gz" required spellcheck="false"></label>
    <label>Name<input name="name" placeholder="optional"></label><button class="btn primary">Open</button><div class="err" id="wbderr"></div></form>`};
 const intro={ann:['Annotate','Annotate an app and check the annotation works: what it promised, next to what a baseline run recorded. Then publish the code.','ann'],
  live:['Live Run','Run an annotated app for hours and watch its provenance graph form. When the run ends, its data is published.','live'],
  query:['Query','Ask about the provenance of a live run (while it runs, or after), an app’s baseline or any dataset: where something came from, what it affected, and the evidence for it. Pick one on the left.','data']}[WB.view];
 const pool=wbPoolOf(WB.view).slice().sort((a,b)=>(b.updated||0)-(a.updated||0)).slice(0,6);
 v.innerHTML=`${what==='live'&&sel?targetBar():''}<div class="wb-home"><h2>${what==='live'&&sel?esc(sel.name)+': a new live run':intro[0]}</h2><p class="sub">${intro[1]}</p><div class="wb-starts">${forms[what==='data'?'data':what==='live'?'live':what==='app'?'ann':intro[2]]}</div>
  ${pool.length?`<h3 style="margin-top:22px">Recent</h3><div class="wb-recent">${pool.map(x=>{const [cls,word]=WB_STATE[x.state]||['',x.state];
   return `<div class="wb-rc" data-id="${esc(x.id)}"><span class="wbd ${cls}"></span><b>${esc(x.name)}</b><span class="sub">${esc(x.app?x.app+' · ':'')}${esc(word)} · ${ago(x.updated)}</span></div>`}).join('')}</div>`:''}</div>`;
 v.querySelectorAll('.wb-rc').forEach(e=>e.onclick=()=>{WB.app=appOf(e.dataset.id);savePick();wbLoad().then(wbOpenView)});
 const fa=$('#wbapp');if(fa)fa.onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),name=(f.get('name')||'').trim();
  const msg=`Annotate an app${name?` named ${name}`:''} for provenance.\nApplication: ${f.get('src')}\nSmall test command: ${f.get('cmd')}\n`+
   'Run the Run pipeline: explore, smoke, discover, annotate, integrate, moirai_smoke, baseline, then the graph stage (show me the diff of what the annotation promised against what the baseline recorded), then package (publish the code).';
  $('#newbtn').onclick();setMode('run');if(await sendText(msg,false,[]))setTimeout(()=>wbLoad(),4000)};
 const fl=$('#wblive');if(fl&&fl.querySelector('select'))fl.onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),m=$('#wblerr');m.textContent='creating…';
  const app=WB.items.apps.find(a=>a.id===f.get('parent')),vals=liValues(fl);
  const r=await api('api/workbench/live',{parent:f.get('parent'),name:(f.get('name')||'').trim(),...(app&&app.live_inputs?{values:vals}:{command:(f.get('cmd')||'').trim()})});if(!r||r.error){m.textContent=(r&&r.error)||'could not create';return}
  m.textContent='';WB.app=f.get('parent');WB.pick.live[WB.app]=r.pipeline;savePick();await wbLoad();await wbOpen(r.pipeline);
  const hrs=+f.get('hours')||12,q=(f.get('queue')||'').trim(),chosen=Object.entries(r.inputs||{}).map(([k,v])=>`- ${k}: ${v}`).join('\n');
  sendText(`Start live run ${r.pipeline} (a live pipeline of app ${f.get('parent')}).\n${chosen?`Chosen inputs:\n${chosen}\n`:''}Run command: ${r.command||'the app\'s run command'}\nTime limit: ${hrs} h${q?`, queue ${q}`:''}.\n`+
   'Show me the exact command, limit and queue and ask me before starting it with moirai_live_run. When it ends, validate it and publish its data (publish_data).',false,[])};
 if(fl&&fl.querySelector('select')){const ps=fl.querySelector('select[name=parent]');ps.onchange=()=>liForm(fl);liForm(fl)}
 const fd=$('#wbdata');if(fd)fd.onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),m=$('#wbderr');m.textContent='opening… (reading the traces once)';
  const r=await api('api/workbench/dataset',{path:f.get('path'),name:f.get('name')});if(!r||r.error){m.textContent=(r&&r.error)||'could not open';return}
  m.textContent='';WB.app=r.pipeline;savePick();await wbLoad();goView('query',r.pipeline)};
 const fi=v.querySelector('form input,form select');if(fi&&what)fi.focus()}

// ---- a live run's inputs, as the annotation phase described them: dropdowns and fields, and the command they make
function liForm(fl){const box=fl.querySelector('#wbli'),app=WB.items.apps.find(a=>a.id===fl.querySelector('select[name=parent]').value),sp=app&&app.live_inputs;
 fl.querySelectorAll('input[name=queue],input[name=hours]').forEach(x=>x.closest('label').hidden=false);
 if(!sp){box.innerHTML=`<p class="sub">The annotation phase has not described this app's run inputs yet (ask in its Annotate chat), so give the command.</p>
   <label>Run command<input name="cmd" placeholder="the real run, e.g. flux batch -N 4 run.sh (default: the app's)"></label>`;
  const c=box.querySelector('input');c.oninput=()=>{$('#wblcmd').textContent=c.value||'(the app’s run command)'};$('#wblcmd').textContent='(the app’s run command)';return}
 box.innerHTML=sp.inputs.map(i=>{const id='li_'+i.name,d=i.default??'',lab=`${esc(i.label||i.name)}${i.help?` <span class="sub">${esc(i.help)}</span>`:''}`;
  const ctl=i.kind==='choice'?`<select data-li="${esc(i.name)}" id="${id}">${(i.options||[]).map(o=>`<option ${String(o)===String(d)?'selected':''}>${esc(o)}</option>`).join('')}</select>`:
   i.kind==='bool'?`<select data-li="${esc(i.name)}" id="${id}"><option value="true" ${String(d)==='true'?'selected':''}>yes</option><option value="false" ${String(d)!=='true'?'selected':''}>no</option></select>`:
   `<input data-li="${esc(i.name)}" id="${id}" ${i.kind==='integer'||i.kind==='number'?`type="number" step="${i.kind==='integer'?1:'any'}"${i.min!=null?` min="${i.min}"`:''}${i.max!=null?` max="${i.max}"`:''}`:''} value="${esc(d)}" spellcheck="false">`;
  return `<label for="${id}">${lab}${ctl}</label>`}).join('');
 box.querySelectorAll('[data-li]').forEach(x=>x.oninput=x.onchange=()=>liPreview(fl,app));liPreview(fl,app);
 const has=n=>sp.inputs.some(i=>n.includes(i.name)),hide=(sel,on)=>{const l=fl.querySelector(sel);if(l)l.closest('label').hidden=on};   // already one of the app's inputs
 hide('input[name=queue]',has(['queue','partition']));hide('input[name=hours]',has(['hours','time_limit','limit','walltime']))}
function liValues(fl){const v={};fl.querySelectorAll('[data-li]').forEach(x=>v[x.dataset.li]=x.value);return v}
async function liPreview(fl,app){const r=await api(`api/workbench/render?pipeline=${encodeURIComponent(app.id)}&values=${encodeURIComponent(JSON.stringify(liValues(fl)))}`);
 const c=$('#wblcmd');if(!c)return;c.classList.toggle('bad',!r||r.status!=='ok');c.textContent=r&&r.status==='ok'?r.command:((r&&r.problems)||['?']).join('; ')}

// ---- open one: its chat on the left, the view on the right
async function wbOpen(id){const it=wbItem(id);if(!it)return;
 WB.sel={id,item:it,view:WB.view};WB.stage=null;delete $('#wbview').dataset.home;delete $('#wbview').dataset.h;clearInterval(WB.timer);
 WB.answers=[];WB.pins=[];WB.card=null;AGG.ann=AGG.live=AGG.q=null;Object.assign(Q,{sel:null,pick:null,g:null,cy:null,sig:'',seen:null,drawn:0});
 {const app=appOf(id);if(app){WB.app=app;if(WB.view!=='ann')WB.pick[WB.view][app]=id;savePick()}}
 document.querySelectorAll('#wblist .wbi').forEach(e=>e.classList.toggle('on',e.dataset.id===WB.app));
 $('#wbview').innerHTML='<div class="sub" style="padding:20px">loading…</div>';
 let chat=it.chat;
 if(WB.view==='query'){const r=await api('api/workbench/querychat',{pipeline:id});chat=r&&r.session;if(chat&&it.kind!=='analysis')it.query_chat=chat}
 if(chat&&chat!==session)openSession(chat);else if(chat)toBottom();else if(!chat&&session)$('#newbtn').onclick();
 setMode(WB.view==='query'?(it.kind==='analysis'?'analysis':'ask'):'run');
 await wbTick(true);WB.timer=setInterval(()=>{if(document.visibilityState==='visible'&&WB.sel)wbTick(false)},5000);
 if(WB.view==='query'&&Q.ask){const q=Q.ask;Q.ask=null;setTimeout(()=>sendText(q,running,[]),700)}}
async function wbTick(first){if(!WB.sel||!document.body.classList.contains('views'))return;
 if(WB.sel.view!==WB.view){WB.sel=null;return wbOpenView()}
 if(!first)await wbLoadQuiet();
 if(WB.view==='ann')return wbAnnotate(first);if(WB.view==='live')return wbLive(first);return wbQuery(!first)}
let WB_LAST=0;async function wbLoadQuiet(){if(Date.now()-WB_LAST<8000)return;WB_LAST=Date.now();await wbLoad()}

// ---- shared bits of the view pages
const strip=(it,names)=>(it.steps||[]).filter(s=>!names||names.includes(s.name)).map((s,i)=>`<span class="wbstep ${s.status}${WB.stage===s.name?' sel':''}" data-st="${esc(s.name)}" title="${esc(s.name)}: ${esc(s.status)}">${s.status==='passed'?'✓':s.status==='failed'?'✗':s.status==='needs_user'?'⏸':i+1} ${esc(STEP_L[s.name]||s.name)}</span>`).join('<span class="wbarr">›</span>');
function headOf(it,extra){const st=it.state==='needs_you'&&agentBusy(it)?'working':it.state,[cls,word]=WB_STATE[st]||['',st];
 return `<div class="wb-head"><h2>${esc(it.name)}</h2><span class="wbstate ${cls}">${esc(word)}</span>${it.app?`<span class="sub">run of ${esc(it.app)}</span>`:''}${it.jobs_running?`<span class="sub">${it.jobs_running} job${it.jobs_running>1?'s':''} running</span>`:''}${extra||''}
  <span class="wb-acts"><label title="Answer recommended options by itself (Settings → Auto-accept)"><input type="checkbox" class="wbauto" ${auto?'checked':''}> autopilot</label></span></div>`}
function wireHead(v){v.querySelectorAll('.wbauto').forEach(a=>a.onchange=()=>setAuto(a.checked));
 v.querySelectorAll('.wbstep[data-st]').forEach(s=>s.onclick=()=>{WB.stage=WB.stage===s.dataset.st?null:s.dataset.st;delete v.dataset.h;wbTick(true)});
 v.querySelectorAll('[data-goto]').forEach(b=>b.onclick=()=>goView(b.dataset.goto,b.dataset.id))}
// waiting for the user only while the agent is not working in that chat (the open one is known at once, others from the server)
const agentBusy=it=>it.busy||(it.chat&&it.chat===session&&running);
const needYou=it=>it.state==='needs_you'&&!agentBusy(it)?'<div class="wb-need">⏸ Waiting for your answer in the chat on the left.</div>':'';
// one stage's checks, for the provenance scientist (or anyone who clicks a step)
async function stageDetail(){const box=$('#wbstage');if(!box||!WB.sel)return;const it=WB.sel.item;
 const name=WB.stage&&(it.steps||[]).some(s=>s.name===WB.stage)?WB.stage:(it.current||((it.steps||[]).slice(-1)[0]||{}).name);if(!name)return;
 const s=await api(`api/pipeline/${encodeURIComponent(it.id)}/${encodeURIComponent(name)}`);if(!s||s.error||!$('#wbstage'))return;
 const c=s.checks||{},rows=[...(c.failed||[]).map(x=>['✗',x]),...(c.unknown||[]).map(x=>['?',x])];
 const html=`<div class="wb-sec">${esc(STEP_L[name]||name)} <span class="sub">${esc(s.status)}${c.status?` · checks ${esc(c.status)} ${c.passed??''}/${c.total??''}`:''}</span></div>
  ${s.summary?`<div class="wb-small">${esc(s.summary)}</div>`:''}${s.command?`<code class="wb-cmd">${esc(s.command)}</code>`:''}
  ${rows.length?`<ul class="wb-checks">${rows.map(([i,x])=>`<li><b>${i} ${esc(x.name)}</b> ${esc(x.detail||'')}${x.fix?`<div class="sub">fix: ${esc(x.fix)}</div>`:''}</li>`).join('')}</ul>`:''}
  ${s.gate?`<div class="sub">gate: ${s.gate.approved?'approved':'waiting'}${s.gate.user_answer?` · “${esc(String(s.gate.user_answer).slice(0,140))}”`:''}</div>`:''}
  ${(s.history||[]).slice(-4).reverse().map(h=>`<div class="sub">${h.ts?new Date(h.ts*1000).toLocaleTimeString():''} ${esc(h.note||h.status||'')}</div>`).join('')}`;
 if(box.dataset.h!==html){box.dataset.h=html;box.innerHTML=html}}

// what a node carries: an entity type's attribute names / an entity's attribute values; an activity's input (and output) names or values
const clip=(x,n)=>x.length>n?x.slice(0,n-1)+'…':x;
function propsOf(n){return n.kind==='activity'?[...(n.inputs||[]),...(n.outputs||[]).map(x=>'→ '+x)]:(n.attrs||[])}
function propLines(n,max){const p=propsOf(n);if(!p.length)return '';const head=n.kind==='activity'&&!String(p[0]).includes('=')?'in: ':'';
 return '\n'+head+p.slice(0,max).map(x=>clip(String(x),30)).join(n.kind==='type'||!String(p[0]).includes('=')?', ':'\n')+(p.length>max?` (+${p.length-max})`:'')}
function propList(d){const p=propsOf(d);return p.length?`<div class="wq-props">${p.slice(0,60).map(x=>`<code>${esc(x)}</code>`).join(' ')}${p.length>60?` <span class="sub">+${p.length-60} more</span>`:''}</div>`:''}

// the hover box of a node: what it is, and its inputs, outputs, parameters and attributes (names for a type or an activity kind, values for an instance)
function tipHtml(d){const name=String(d.name||d.label||d.id).split('\n')[0],k=d.kind==='type'?'entity type':d.kind;
 const p=d.kind==='activity'?[['inputs',d.inputs],['outputs',d.outputs]]:[[d.kind==='type'?'attributes':'attributes',d.attrs]];
 const rows=p.filter(([,v])=>v&&v.length).map(([l,v])=>`<div class="tip-h">${l}${d.kind==='type'||(d.kind==='activity'&&!String(v[0]).includes('='))?' (names)':''}</div><div class="wq-props">${v.slice(0,40).map(x=>`<code>${esc(x)}</code>`).join(' ')}${v.length>40?` <span class="sub">+${v.length-40}</span>`:''}</div>`).join('');
 return `<b>${esc(name)}</b> <span class="sub">${esc(k)}${d.type?' · '+esc(d.type):''}${d.role?' · '+esc(d.role):''}${d.count?' · ×'+d.count:''}</span>${d.uri?`<div class="sub tip-u">${esc(d.uri)}</div>`:''}${d.kind==='activity'&&d.pid!=null?`<div class="sub">pid ${esc(d.pid)}${d.ts?' · '+esc(isoUs(d.ts)):''}</div>`:''}${rows||'<div class="sub">nothing recorded about it beyond this</div>'}`}
function hoverTip(cy,box){const host=box.parentElement;let tip=host.querySelector(':scope > .cy-tip');if(!tip){tip=document.createElement('div');tip.className='cy-tip';tip.hidden=true;host.appendChild(tip)}
 cy.on('mouseover','node',e=>{const p=e.renderedPosition||e.target.renderedPosition(),bb=box.getBoundingClientRect(),hb=host.getBoundingClientRect();
  tip.innerHTML=tipHtml(e.target.data());tip.hidden=false;const x=bb.left-hb.left+p.x+14,y=bb.top-hb.top+p.y+14;
  tip.style.left=Math.max(4,Math.min(x,host.clientWidth-tip.offsetWidth-6))+'px';tip.style.top=Math.max(4,Math.min(y,host.clientHeight-tip.offsetHeight-6))+'px'});
 cy.on('mouseout','node',()=>{tip.hidden=true});cy.on('pan zoom',()=>{tip.hidden=true})}

// ---- the aggregated view: entity types and activity kinds, how they relate, with counts; in the Annotate view the diff
// of what the annotation promised against what was recorded. Kept across refreshes: counts and statuses update in place.
const AGG={};
const AGG_LEG=[['matched','recorded as promised'],['missing','promised, never recorded'],['conditional','promised if a condition holds, not recorded'],['unexpected','recorded, not in the annotation'],['captured','captured value: inputs, outputs, attributes']];
// the legend: what the shapes and edge colours mean, and on a diff what each status looks like
function aggLegend(mode){
 const parts=`<span class="ag-lg"><span><i class="ag-type"></i>entity type</span><span><i class="ag-in"></i>input type</span><span><i class="ag-act"></i>activity</span><span>★ scientific artifact</span><span>×n recorded</span></span>
  <span class="ag-lg"><span><i class="ag-gen"></i>generated</span><span><i class="ag-used"></i>used</span><span><i class="ag-upd"></i>updated / deleted</span><span><i class="ag-rel"></i>relation</span></span>
  <span class="ag-lg"><span>arrows: data → the activity that used it → the data it generated; a relation points from one entity type to another</span></span>`;
 const st=mode==='diff'?`<span class="ag-lg">${AGG_LEG.map(([k,l])=>`<span><i class="ag-${k}"></i>${l}</span>`).join('')}</span>`:'';
 return `<span class="ag-leg">${parts}${st}</span>`}
async function drawAgg(key,boxId,d,hl,onTap){const box=$('#'+boxId);if(!box)return null;
 if(!d||!d.nodes||!d.nodes.length){AGG[key]=null;box.innerHTML=`<div class="sub" style="padding:16px">${d&&d.reading?'reading the traces…':'Nothing to draw yet: the diagram appears once the annotation exists or the run records provenance.'}</div>`;return null}
 try{await dgNeedGraph()}catch(e){box.textContent=e.message;return null}
 const sig=d.mode+'#'+d.nodes.map(n=>n.id).sort().join()+'#'+d.edges.map(e=>e.id).sort().join();
 const nd=n=>({id:n.id,label:(n.artifact?'★ ':'')+n.label+(n.count?`  ×${n.count}`:''),kind:n.kind,status:n.status||'',role:n.role||'',name:n.label,count:n.count||0,
   attrs:n.attrs||[],inputs:n.inputs||[],outputs:n.outputs||[]});
 const ed=e=>({id:e.id,source:e.source,target:e.target,kind:e.kind,status:e.status||'',label:e.kind==='relation'?(e.label||''):(e.count&&pro()?'×'+e.count:''),count:e.count||0});
 const st=AGG[key];
 if(st&&st.sig===sig&&st.cy.container()===box){st.cy.batch(()=>{d.nodes.forEach(n=>st.cy.getElementById(n.id).data(nd(n)));d.edges.forEach(e=>st.cy.getElementById(e.id).data(ed(e)))});aggHl(st.cy,hl);return st.cy}
 box.innerHTML='';const dark=themeNow()==='dark',ink=dark?'#cbd5e1':'#334155';
 const cy=cytoscape({container:box,wheelSensitivity:.25,minZoom:.1,maxZoom:2.5,
  elements:[...d.nodes.map(n=>({data:nd(n)})),...d.edges.map(e=>({data:ed(e)}))],
  layout:{name:'dagre',rankDir:box.clientHeight>box.clientWidth*.7?'TB':'LR',nodeSep:26,rankSep:56,animate:false},
  style:[{selector:'node',style:{label:'data(label)','font-size':10,'text-valign':'bottom','text-margin-y':4,color:ink,'text-wrap':'wrap','text-max-width':150}},
   {selector:'node[kind="type"]',style:{shape:'round-rectangle',width:30,height:20,'background-color':'#bfdbfe','border-width':1.5,'border-color':'#1d4ed8'}},
   {selector:'node[role="input"],node[role="parameter"],node[role="reference"]',style:{'background-color':'#bbf7d0','border-color':'#15803d'}},
   {selector:'node[kind="activity"]',style:{shape:'ellipse',width:24,height:24,'background-color':'#fed7aa','border-width':1.5,'border-color':'#c2410c'}},
   {selector:'edge',style:{width:1.6,'curve-style':'bezier','target-arrow-shape':'triangle','arrow-scale':.8,'line-color':'#94a3b8','target-arrow-color':'#94a3b8',
     label:'data(label)','font-size':8,color:ink,'text-background-color':dark?'#0f172a':'#fff','text-background-opacity':.8,'text-background-padding':1}},
   {selector:'edge[kind="generated"]',style:{'line-color':'#15803d','target-arrow-color':'#15803d'}},
   {selector:'edge[kind="updated"],edge[kind="invalidated"]',style:{'line-color':'#a16207','target-arrow-color':'#a16207'}},
   {selector:'edge[kind="relation"]',style:{'line-style':'dashed','line-color':'#7c3aed','target-arrow-color':'#7c3aed'}},
   {selector:'node[status="missing"]',style:{'border-color':'#dc2626','border-style':'dashed','border-width':2.5,'background-opacity':.35}},
   {selector:'node[status="conditional"]',style:{'border-color':'#d97706','border-style':'dashed','border-width':2.5,'background-opacity':.45}},
   {selector:'node[status="unexpected"]',style:{'border-color':'#2563eb','border-width':4,'border-style':'double'}},
   {selector:'edge[status="missing"]',style:{'line-color':'#dc2626','target-arrow-color':'#dc2626','line-style':'dashed',width:2.4}},
   {selector:'edge[status="conditional"]',style:{'line-color':'#d97706','target-arrow-color':'#d97706','line-style':'dashed',width:2}},
   {selector:'edge[status="unexpected"]',style:{'line-color':'#2563eb','target-arrow-color':'#2563eb',width:2.8}},
   {selector:'node[status="captured"]',style:{width:16,height:12,'background-color':'#e2e8f0','border-color':'#64748b','border-width':1,'font-size':8,color:'#64748b'}},
   {selector:'edge[status="captured"]',style:{'line-color':'#cbd5e1','target-arrow-color':'#cbd5e1',width:1,'line-style':'dotted'}},
   {selector:'.dim',style:{opacity:.13}},
   {selector:'node.focus',style:{'border-width':3.5,'border-color':'#7c3aed','border-style':'solid'}},
   {selector:'edge.focus',style:{width:3,'line-color':'#7c3aed','target-arrow-color':'#7c3aed'}},
   {selector:'node.pick',style:{'background-color':'#f59e0b','border-color':'#b45309','border-width':4,'z-index':9}}]});
 AGG[key]={cy,sig};aggHl(cy,hl);cy.on('tap','node',e=>onTap&&onTap(e.target.data()));hoverTip(cy,box);return cy}
// highlight: {types:Set(type names), acts:Set(activity names), pick:Set(node ids)}; null: everything plain
function aggHl(cy,hl){if(!cy)return;cy.batch(()=>{cy.elements().removeClass('dim focus pick');if(!hl)return;
 const keep=new Set(cy.nodes().filter(n=>(n.data('kind')==='type'?hl.types:hl.acts).has(n.data('name'))).map(n=>n.id()));
 cy.elements().addClass('dim');cy.nodes().filter(n=>keep.has(n.id())).removeClass('dim').addClass('focus');
 cy.edges().filter(e=>keep.has(e.source().id())&&keep.has(e.target().id())).removeClass('dim').addClass('focus');
 if(hl.pick)cy.nodes().filter(n=>hl.pick.has(n.id())).removeClass('dim').addClass('pick')})}
function aggFit(cy){if(!cy)return;const s=cy.$('.pick').length?cy.$('.pick'):cy.$('.focus');cy.fit(s.length?s:cy.elements(),s.length&&s.length<4?70:24);if(cy.zoom()>1.4){cy.zoom(1.4);cy.center(s.length?s:undefined)}}
function aggInfo(boxId,d){const b=$('#'+boxId);if(!b)return;b.hidden=false;
 b.innerHTML=`<b>${d.kind==='type'?'entity type':'activity'}</b> ${esc(d.name)}${d.role?` <span class="sub">(${esc(d.role)})</span>`:''}${d.count?` · <b>${d.count}</b> recorded`:''}${d.status&&d.status!=='expected'?` · <span class="ag-st ${esc(d.status)}">${esc(d.status)}</span>`:''}
  ${propsOf(d).length?`<div class="sub">${d.kind==='type'?'attributes':'inputs and outputs'}:</div>${propList(d)}`:''}
  <div class="wq-ia"><button data-q="Where do the ${esc(d.name)} ${d.kind==='type'?'entities':'activities'} come from, and what did they affect?">ask about it</button><button data-x>✕</button></div>`;
 b.querySelectorAll('[data-q]').forEach(x=>x.onclick=()=>{if(WB.view==='query')sendText(x.dataset.q,running,[]);else{$('#input').value=x.dataset.q;$('#input').focus()}});b.querySelector('[data-x]').onclick=()=>b.hidden=true}

// ---- Annotate view: the steps, the diagram (promised, then promised vs recorded), the diff in words, publish the code
async function wbAnnotate(first){if(!WB.sel||!WB.sel.item)return;const it=WB.sel.item,pid=it.id,v=$('#wbview');
 const d=await api('api/workbench/aggregate?pipeline='+encodeURIComponent(pid));if(!WB.sel||WB.sel.id!==pid||WB.view!=='ann')return;
 const steps=it.steps||[],setup=steps.filter(s=>!['graph','package'].includes(s.name)).map(s=>s.name),gst=steps.find(s=>s.name==='graph')||{},pk=steps.find(s=>s.name==='package')||{};
 const mode=d&&d.mode,sum=(d&&d.summary)||{},reading=d&&d.reading,title=mode==='diff'?'Promised by the annotation vs recorded by the run':mode==='expected'?'What the annotation will record'+(reading?' · reading what the run recorded…':''):'The diagram';
 const lists=mode==='diff'?[['missing','Promised, never recorded',[...(d.missing_types||[]).map(t=>'type '+t),...(d.missing||[])]],['conditional','Promised under a condition, not recorded',d.conditional||[]],['unexpected','Recorded, not in the annotation',[...(d.unexpected_types||[]).map(t=>'type '+t),...(d.unexpected||[])]]]:[];
 const verdict=reading?'<div class="sub">Reading what the run recorded (a large run takes a minute the first time). The comparison appears here when it is read.</div>':mode!=='diff'?`<div class="sub">${mode==='expected'?'The run has not recorded anything yet: this is what the annotation promises. After the baseline, the recorded diagram is compared with it here.':'No annotation yet.'}</div>`:
  d.clean?`<div class="wb-ok">✓ Everything the annotation promised was recorded${sum.unexpected?`, plus ${sum.unexpected} relationship${sum.unexpected>1?'s':''} it did not promise`:''}.</div>`:`<div class="wb-bad">✗ ${sum.missing+((d.missing_types||[]).length)} promised relationship(s) or type(s) were never recorded.</div>`;
 const side=`<div class="wb-card"><div class="wb-sec">The check</div>${verdict}
   ${mode==='diff'?`<div class="ag-sum">${['matched','missing','conditional','unexpected'].map(k=>`<span class="ag-st ${k}"><b>${sum[k]||0}</b> ${k}</span>`).join('')}</div>`:''}
   ${lists.filter(l=>l[2].length).map(([k,t,xs])=>`<div class="wb-sec">${t}</div><ul class="ag-list ${k}">${xs.slice(0,pro()?50:8).map(x=>`<li>${esc(x)}</li>`).join('')}${!pro()&&xs.length>8?`<li class="sub">+${xs.length-8} more</li>`:''}</ul>`).join('')}
   <div class="sub" style="margin-top:6px">${gst.status==='needs_user'&&!agentBusy(it)?'The agent is asking you to approve this check in the chat.':gst.status==='needs_user'?'The agent is working on your answer.':gst.status==='passed'?'You approved this check.':''}</div></div>
  <div class="wb-card"><div class="wb-sec">Publish the code</div>${it.published?`<div class="wb-ok">✓ Published</div><code class="wb-cmd">${esc(it.published)}</code>`:
   `<div class="sub">${pk.status==='pending'?'After you approve the check, the annotated code and how to run it are packaged and published.':esc(STEP_L.package)+': '+esc(pk.status||'')}</div>`}
   ${it.can_live?`<button class="btn" data-goto="live" data-id="${esc(pid)}" style="margin-top:8px">▶ Live runs of this app</button>`:''}
   <button class="btn" data-goto="query" data-id="${esc(pid)}" style="margin-top:8px">⌕ Query the baseline</button></div>
  <div class="wb-card"><div class="wb-sec">Live run inputs</div>${it.live_inputs?`<ul class="wb-li-list">${it.live_inputs.inputs.map(i=>`<li><b>${esc(i.label||i.name)}</b> ${i.kind==='choice'?`<span class="sub">${(i.options||[]).length} choices</span>`:''} <span class="sub">default ${esc(i.default??'-')}</span></li>`).join('')}</ul>${pro()?`<code class="wb-cmd">${esc(it.live_inputs.command)}</code>`:''}`:
   `<div class="sub">What a scientist can tune when starting a live run. The agent writes them once it has explored and annotated the app.</div><button class="btn" id="wbdetect" style="margin-top:6px">Detect them now</button>`}</div>
  ${gapsCard(it)}${pro()||WB.stage?'<div class="wb-card" id="wbstage"></div>':''}`;
 const html=headOf(it)+`<div class="wb-strip"><span class="sub">annotate</span>${strip(it,setup)}<span class="sub" style="margin-left:12px">check &amp; publish</span>${strip(it,['graph','package'])}</div>${needYou(it)}${nowCard(it)}
  <div class="vw-grid"><div class="vw-main"><div class="wq-tools"><b class="vw-t">${title}</b>${aggLegend(mode)}<span class="wq-btns"><button id="agfit">fit</button></span></div>
   <div id="aggann" class="vw-graph"></div><div id="agginfo" class="wq-info" hidden></div></div><div class="vw-side">${side}</div></div>`;
 if(v.dataset.h!==html){v.dataset.h=html;const keepG=AGG.ann&&$('#aggann');const old=keepG?$('#aggann'):null;v.innerHTML=html;if(old)$('#aggann').replaceWith(old);wireHead(v);$('#agfit').onclick=()=>aggFit(AGG.ann&&AGG.ann.cy);
  const gg=$('#wbgapall');if(gg)gg.onclick=()=>workAllGaps(pid,gg);
  const dt=$('#wbdetect');if(dt)dt.onclick=()=>sendText('Describe this app\'s live-run inputs (what a scientist tunes when starting a long run): live_inputs_detect, curate them with what you know of the app, live_inputs_write and live_inputs_validate, then show them to me.',running,[])}
 await drawAgg('ann','aggann',d,null,x=>aggInfo('agginfo',x));if(first&&AGG.ann)aggFit(AGG.ann.cy);stageDetail()}

// ---- Live Run view: the run's chat on the left; here the graph forming live (aggregated, instances, temporal), then publish the data
async function wbLive(first){if(!WB.sel||!WB.sel.item)return;const it=WB.sel.item,pid=it.id,v=$('#wbview');
 const [L,d]=await Promise.all([api('api/workbench/live?pipeline='+encodeURIComponent(pid)),WB.lgmode==='agg'?api('api/workbench/aggregate?pipeline='+encodeURIComponent(pid)):null]);
 if(!WB.sel||WB.sel.id!==pid||WB.view!=='live')return;
 const n=(L&&L.counts)||{},r=(L&&L.rate)||{},lr=(it.steps||[]).find(s=>s.name==='live_run')||{},pd=(it.steps||[]).find(s=>s.name==='publish_data')||{};
 const el=it.started?dur(Math.max(0,Date.now()/1000-it.started)):'';
 const kpi=(k,val,sub)=>`<div class="wb-kpi"><div class="v">${esc(val??'-')}</div><div class="k">${k}${sub?`<span>${esc(sub)}</span>`:''}</div></div>`;
 const head=targetBar()+headOf(it,`${el&&lr.status==='running'?`<span class="sub">running for ${el}</span>`:''}<span class="sub">${L&&L.reading?'reading the traces…':L&&L.quiet_s!=null?'last provenance '+dur(L.quiet_s)+' ago':''}</span><button class="btn" data-goto="query" data-id="${esc(pid)}">⌕ Query this run</button>`)+
  `<div class="wb-strip">${strip(it)}</div>${needYou(it)}${nowCard(it)}${L&&L.stalled?`<div class="wb-need bad">⚠ No new provenance for ${dur(L.quiet_s)} while the run is going. Ask the chat to check the job.</div>`:''}`;
 const kpis=`<div class="wb-kpis">${kpi('entities',n.entities,r.entities_per_h!=null?`+${r.entities_per_h}/h`:'')}${kpi('activities',n.activities,r.activities_per_h!=null?`+${r.activities_per_h}/h`:'')}${kpi('links',n.edges)}${pro()?kpi('types',n.types)+kpi('records read',n.records):''}</div>`;
 const seg=`<div class="seg" id="lgseg">${[['agg','Aggregated'],['inst','Instances'],['temp','Temporal']].map(([k,l])=>`<button data-g="${k}" class="${WB.lgmode===k?'on':''}">${l}</button>`).join('')}</div>`;
 const feed=((L&&L.feed)||[]).slice(0,pro()?20:8).map(f=>`<li><b>${esc(f.type)}</b> <span title="${esc(f.uri)}">${esc((f.uri||f.id).split('/').pop().slice(0,48))}</span> <span class="sub">← ${esc(f.by)}</span></li>`).join('');
 const side=`<div class="wb-card"><div class="wb-sec">Newest artifacts</div><ul class="wb-feed">${feed||'<li class="sub">none yet</li>'}</ul></div>
  <div class="wb-card"><div class="wb-sec">Publish the data</div>${it.published?`<div class="wb-ok">✓ Published</div><code class="wb-cmd">${esc(it.published)}</code>`:
   `<div class="sub">${lr.status==='passed'?'The run is done: '+(pd.status==='needs_user'&&!agentBusy(it)?'the agent asks you to approve publishing.':'its traces are being published.'):'When the run ends, its traces and how it was run are published.'}</div>`}</div>
  ${pro()?`<div class="wb-card"><div class="wb-sec">Jobs</div>${((L&&L.jobs)||[]).map(j=>`<div class="wb-small">${esc(j.name)} · ${esc(j.status)}${j.rc!=null?' rc '+esc(j.rc):''} · ${dur(j.elapsed||0)}</div>`).join('')||'<div class="sub">none</div>'}</div><div class="wb-card" id="wbstage"></div>`:(WB.stage?'<div class="wb-card" id="wbstage"></div>':'')}`;
 const temp=it.dash_run?`dash/?view=prov-temporal&run=${encodeURIComponent(it.dash_run)}`:'';
 const main=`<div class="wq-tools">${seg}${WB.lgmode==='agg'?(d&&d.reading?'<span class="sub">reading the traces… showing what the annotation promises</span>':aggLegend(d&&d.mode)):''}<span class="wq-btns">${WB.lgmode!=='temp'?'<button id="lgfit">fit</button>':temp?`<a href="${temp}" target="_blank" class="sub">open ↗</a>`:''}</span></div>
  ${WB.lgmode==='temp'?(temp?`<iframe class="vw-frame" id="lgframe" title="temporal view"></iframe>`:'<div class="sub" style="padding:16px">The temporal view opens once the run writes traces.</div>'):'<div id="lggraph" class="vw-graph"></div>'}<div id="lginfo" class="wq-info" hidden></div>`;
 const html=head+kpis+`<div class="vw-grid"><div class="vw-main">${main}</div><div class="vw-side">${side}</div></div>`;
 if(v.dataset.h!==html){const g=$('#lggraph'),fr=$('#lgframe');v.dataset.h=html;v.innerHTML=html;
  if(g&&$('#lggraph')&&g.dataset.mode===WB.lgmode)$('#lggraph').replaceWith(g);if(fr&&$('#lgframe'))$('#lgframe').replaceWith(fr);
  wireHead(v);v.querySelectorAll('#lgseg button').forEach(b=>b.onclick=()=>{WB.lgmode=b.dataset.g;AGG.live=null;LIVEG.cy=null;delete v.dataset.h;wbLive(true)});
  const f=$('#lgfit');if(f)f.onclick=()=>{const c=WB.lgmode==='agg'?AGG.live&&AGG.live.cy:LIVEG.cy;if(c)c.fit(undefined,24)}}
 const fr=$('#lgframe');if(fr&&fr.dataset.src!==temp){fr.dataset.src=temp;fr.src=temp}
 const box=$('#lggraph');if(box)box.dataset.mode=WB.lgmode;
 if(WB.lgmode==='agg'){const cy=await drawAgg('live','lggraph',d,null,x=>aggInfo('lginfo',x));if(first&&cy)cy.fit(undefined,24)}
 else if(WB.lgmode==='inst')await liveInst(first);
 stageDetail()}
// the instance graph of a live run: redrawn when it grew, at most every 20 s
const LIVEG={cy:null,sig:'',at:0};
async function liveInst(first){if(!WB.sel)return;const pid=WB.sel.id,g=await api(`api/workbench/graph?pipeline=${encodeURIComponent(pid)}&since=${encodeURIComponent(first?'':LIVEG.sig)}`);
 const box=$('#lggraph');if(!box||!WB.sel||WB.sel.id!==pid)return;
 if(!g||g.error||g.same)return;if(g.reading){if(!LIVEG.cy)box.innerHTML='<div class="sub" style="padding:16px">reading the traces…</div>';return}
 if(g.truncated){LIVEG.cy=null;box.innerHTML=`<div class="sub" style="padding:16px">${g.counts.entities+g.counts.activities} nodes: too many to draw one by one. The Aggregated view shows all of it; the Query view draws the part a question is about.</div>`;return}
 if(!first&&LIVEG.cy&&Date.now()-LIVEG.at<20000)return;
 LIVEG.sig=g.sig;LIVEG.at=Date.now();LIVEG.cy=await drawInst(box,g.nodes,g.edges,x=>wqInfoAt('lginfo',x))}

// ---- Query view: the query chat on the left; here the graph and the evidence of the selected query. Select a query to see
// the part of the graph it used; select one piece of its plan or evidence to see just that. Instances or aggregated.
const Q={sel:null,pick:null,sig:'',g:null,cy:null,adj:null,drawn:0,seen:null,part:false,gmode:'inst',want:null,reach:false};
const PLAN_K=[['intent','Intent'],['targets','Targets'],['entities','Entities'],['edges','Edges'],['time','Time / version'],['filters','Filters'],['output_schema','Output schema']];
const EV_I={event:'◷',artifact:'▣',source:'⛁',query:'⌕'};
const WQ_ASK={live:['What has the run produced in the last hour?','Where did the newest artifact come from? Show its lineage.','Which inputs fed the most artifacts?','What is downstream of the input data: what would a bad input affect?','What is sensitive for publishing? Mark everything derived from sensitive inputs.'],
 run:['Where did the final outputs of the baseline come from?','Which inputs fed the most artifacts?','What is downstream of each input?'],
 analysis:['Where did the final outputs come from?','Which inputs fed the most artifacts?','What is sensitive for publishing? Mark everything derived from sensitive inputs.']};
function wbShowAnswer(id){Q.want=id;if(WB.view!=='query'||!WB.sel){if(WB.sel)goView('query',WB.sel.id);else wbShow('query');return}Q.sel=id;Q.pick=null;wbQStrip();wbQPanel();wqHighlight(true)}

async function wbQuery(quiet){if(!WB.sel||!WB.sel.item)return;const it=WB.sel.item,pid=it.id,v=$('#wbview');
 if(!quiet||!$('#wqgraph')){Q.cy=null;Q.drawn=0;Q.sig='';Q.seen=null;AGG.q=null;
  v.innerHTML=targetBar()+`<div class="wb-head"><h2>${esc(it.published_run?`${it.published_run.name} · ${it.published_run.kind}`:it.name)}</h2><span class="sub">${it.published_run?`${esc(it.published_run.label)} of ${esc(it.published_run.name)}, published run of ${esc(it.published_run.ts)}`+(it.published_run.scale?` (scale ${esc(it.published_run.scale)})`:''):({live:'live run',run:'current baseline of the app',analysis:'dataset'}[it.kind]||'')+(it.app?' of '+esc(it.app):'')}</span>
    <div class="seg" id="wqseg"><button data-g="inst" class="${Q.gmode==='inst'?'on':''}">Instances</button><button data-g="agg" class="${Q.gmode==='agg'?'on':''}">Aggregated</button></div>
    <span class="sub" id="wqstat"></span><span class="wb-acts"><a href="#" id="wbreport">report of pinned answers</a></span></div>
   <div class="wq-strip" id="wqstrip"></div>
   <div class="wq-grid2"><div class="wq-viz"><div class="wq-tools"><span class="wq-leg" id="wqleg"></span>
      <span class="wq-btns"><button id="wqreach" title="also draw everything upstream (lineage) and downstream (impact) of the answer's data">${Q.reach?'answer only':'+ upstream & downstream'}</button><button id="wqfit" title="zoom to the selected part">fit selection</button><button id="wqall" title="show the whole graph">whole graph</button></span></div>
     <div id="wqgraph" class="wq-graph"><div class="sub" style="padding:16px">reading the graph…</div></div><div id="wqinfo" class="wq-info" hidden></div></div>
    <div class="wq-panel" id="wqpanel"></div></div>`;
  $('#wbreport').onclick=e=>{e.preventDefault();wbReport()};
  $('#wqfit').onclick=()=>wqHighlight(true);$('#wqreach').onclick=e=>{Q.reach=!Q.reach;e.target.textContent=Q.reach?'answer only':'+ upstream & downstream';Q.cy=null;AGG.q=null;wqDraw()};$('#wqall').onclick=()=>{const c=Q.gmode==='agg'?AGG.q&&AGG.q.cy:Q.cy;c&&c.animate({fit:{padding:20}},{duration:250})};
  v.querySelectorAll('#wqseg button').forEach(b=>b.onclick=()=>{Q.gmode=b.dataset.g;v.querySelectorAll('#wqseg button').forEach(x=>x.classList.toggle('on',x===b));Q.cy=null;AGG.q=null;wqDraw()});
  if(it.kind==='analysis'&&!WB.card)api('api/workbench/card?pipeline='+encodeURIComponent(pid)).then(c=>{if(c&&!c.error){WB.card=c;if(!Q.sel)wbQPanel()}})}
 const [d,gr]=await Promise.all([api('api/workbench/answers?pipeline='+encodeURIComponent(pid)),api(`api/workbench/graph?pipeline=${encodeURIComponent(pid)}&since=${encodeURIComponent(Q.sig)}`)]);
 if(!WB.sel||WB.sel.id!==pid||WB.view!=='query')return;
 if(d&&!d.error){const ids=d.answers.map(a=>a.id);
  if(Q.want&&ids.includes(Q.want)){Q.sel=Q.want;Q.pick=null;Q.want=null}
  else if(Q.seen){const fresh=ids.filter(i=>!Q.seen.has(i));if(fresh.length){Q.sel=fresh[fresh.length-1];Q.pick=null}}   // the agent just answered: show it
  Q.seen=new Set(ids);const sig=JSON.stringify([ids,d.pins,Q.sel,Q.pick]);WB.answers=d.answers;WB.pins=d.pins;
  if(Q.sel&&!ids.includes(Q.sel))Q.sel=null;if($('#wqstrip').dataset.sig!==sig){$('#wqstrip').dataset.sig=sig;wbQStrip();wbQPanel();if(Q.cy||AGG.q)wqHighlight(true)}}
 const st=$('#wqstat');
 if(gr&&gr.reading){st.textContent='reading the traces…';return}
 if(gr&&!gr.error&&!gr.same){const firstG=!Q.g;Q.g=gr;Q.sig=gr.sig;
  st.textContent=`${gr.counts.entities} entities · ${gr.counts.activities} activities${it.state==='running'?' · live':''}`;
  if(gr.truncated&&firstG&&!Q.sel)Q.gmode='agg';
  if(firstG||Date.now()-Q.drawn>30000)await wqDraw();else st.textContent+=' · grew, redraws shortly'}
 else if(gr&&gr.same&&Q.g&&Q.g.sig!==Q.sig)await wqDraw()}

function wbQStrip(){const s=$('#wqstrip');if(!s)return;const as=WB.answers.slice().reverse();
 s.innerHTML=`<button class="wq-chip ${Q.sel?'':'on'}" data-a="">◎ whole graph</button>`+as.map(a=>`<button class="wq-chip ${a.id===Q.sel?'on':''} ${a.verified?'':'unv'}" data-a="${esc(a.id)}" title="${esc(a.question)}">${a.verified?'✓':'⚠'} ${esc((a.question||'').slice(0,60))}${WB.pins.includes(a.id)?' 📌':''}</button>`).join('');
 s.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>{Q.sel=b.dataset.a||null;Q.pick=null;wbQStrip();wbQPanel();wqHighlight(true)})}

function wbQPanel(){const p=$('#wqpanel');if(!p||!WB.sel||!WB.sel.item)return;const a=WB.answers.find(x=>x.id===Q.sel),it=WB.sel.item;
 if(a&&a.engine==='traceviewer')return wbQPanelTV(p,a);
 if(!a){const qs=(it.kind==='analysis'&&WB.card&&WB.card.questions)||WQ_ASK[it.kind]||[];
  p.innerHTML=`<div class="wq-intro"><b>Ask in the chat on the left.</b> Each answer becomes a query above: its plan, the evidence it collected and its answer show here, and the part of the graph it used is highlighted.</div>
   ${WB.answers.length?`<div class="wb-sec">Queries</div><div class="wq-list">${WB.answers.slice().reverse().map(x=>`<button class="wq-li" data-a="${esc(x.id)}"><span class="ac-badge ${x.verified?'ok':'bad'}">${x.verified?'✓':'⚠'}</span> ${esc(x.question||'')}<span class="sub">${ago(x.ts)}</span></button>`).join('')}</div>`:''}
   <div class="wb-sec">Try</div><div class="wb-qs">${qs.map(q=>`<button class="wb-q">${esc(q)}</button>`).join('')}</div>`;
  p.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>{Q.sel=b.dataset.a;Q.pick=null;wbQStrip();wbQPanel();wqHighlight(true)});
  p.querySelectorAll('.wb-q').forEach(b=>b.onclick=()=>sendText(b.textContent,running,[]));return}
 const pl=a.plan||{},val=v=>Array.isArray(v)?v.join(', '):typeof v==='object'&&v?JSON.stringify(v):String(v??''),st=a.stats||{},conf=Math.round((a.confidence||0)*100);
 const on=k=>Q.pick===k?' on':'';
 const planRows=PLAN_K.filter(([k])=>pl[k]!=null&&val(pl[k])!=='').filter(([k])=>pro()||['intent','targets'].includes(k));
 const plan=planRows.map(([k,l])=>`<div class="ap-k">${l}</div><div class="ap-v${k==='targets'?' wq-pk'+on('t'):''}"${k==='targets'?' data-pk="t"':''}>${esc(val(pl[k]))}</div>`).join('')||'<div class="sub">no plan given</div>';
 const ev=(a.evidence||[]).map((e,i)=>{const can=e.entity||e.activity;return `<li class="${can?'wq-pk':''}${on('e'+i)}" ${can?`data-pk="e${i}"`:''} title="${can?'show it on the graph':'not a graph record'}"><span class="ev-i">${EV_I[e.kind]||'•'}</span><span>${esc(e.text||'')}${pro()&&e.entity?` <code>${esc(String(e.entity).slice(0,40))}</code>`:''}${e.activity?` <code>${esc(e.activity)}</code>`:''}${pro()&&e.tool?` <span class="sub">via ${esc(e.tool)}</span>`:''}</span></li>`}).join('');
 const body=a.verified?`<div class="ap-ans">${md(a.answer||'')}</div>`:`<div class="ap-unv"><b>Unverified: evidence and lineage only.</b><ul>${(a.problems||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul><details><summary>the unverified summary</summary>${md(a.answer||'')}</details></div>`;
 const pinned=WB.pins.includes(a.id);
 p.innerHTML=cfHtml(cfOf(a))+`<div class="wq-q"><span class="ac-badge ${a.verified?'ok':'bad'}">${a.verified?'✓ verified against the traces':'⚠ unverified'}</span><b>${esc(a.question||'')}</b><span class="sub">${a.ts?new Date(a.ts*1000).toLocaleString():''} · ${a.direction==='forward'?'downstream impact':a.direction==='both'?'lineage both ways':'lineage'}</span></div>
  <section class="wq-sec"><h4 class="wq-pk${on('t')}" data-pk="t"><span class="n">1</span>Query plan <span class="sub">targets</span></h4><div class="ap-plan">${plan}</div></section>
  <section class="wq-sec"><h4><span class="n">2</span>Evidence <span class="sub">select one to see it</span></h4><ul class="ap-ev">${ev||'<li class="sub">none</li>'}</ul>
   ${(a.gaps||[]).length?`<div class="ap-gaps tv-gaps"><b>Gaps</b><ul>${a.gaps.map((g,i)=>`<li>${esc(typeof g==='string'?g:(g.detail||g.subject||''))}${it.kind!=='analysis'?`<div><button class="btn" data-ogap="${i}" title="Open the app's Annotate chat: explore the code for this gap, fix the annotation, learn it in the capabilities">Improve coverage</button></div>`:''}</li>`).join('')}</ul>
    ${it.kind!=='analysis'&&a.gaps.length>1?`<button class="btn primary" data-ogap="*">Improve coverage of all ${a.gaps.length} gaps</button>`:''}</div>`:''}
   </section>
  <section class="wq-sec"><h4 class="wq-pk${Q.pick?'':' on'}" data-pk=""><span class="n">3</span>Answer <span class="sub">its whole part</span></h4>${body}
   ${a.sensitive?`<div class="ap-sens wq-pk${on('s')}" data-pk="s">⛔ <b>${a.sensitive.count}</b> entities derive from sensitive inputs: restrict or review before sharing. <span class="sub">show them</span></div>`:''}
   <div class="ap-stats"><span><b>${st.sources??'-'}</b> sources</span><span><b>${st.artifacts??'-'}</b> artifacts</span>${pro()?`<span><b>${st.records??'-'}</b> records read</span>`:''}</div></section>
  <div class="ac-del"><button id="wqpin" class="${pinned?'on':''}">${pinned?'📌 pinned':'📌 pin to report'}</button><button id="wqcite">⧉ copy citations</button>${pro()?'<button id="wqaudit">audit trail</button>':''}<button id="wqwrong" class="warn">this looks wrong</button></div>
  ${pro()?`<pre class="ac-audit" id="wqaud" hidden>${esc(JSON.stringify({id:a.id,question:a.question,targets:a.targets,direction:a.direction,problems:a.problems,records_read:st.records,plan:a.plan,evidence:a.evidence},null,1))}</pre>`:''}`;
 p.querySelectorAll('[data-ogap]').forEach(b=>b.onclick=()=>improveGaps(a,b.dataset.ogap==='*'?a.gaps.map((_,k)=>'t'+k):['t'+b.dataset.ogap],b));
 p.querySelectorAll('[data-pk]').forEach(x=>x.onclick=()=>{Q.pick=x.dataset.pk||null;wbQPanel();wqHighlight(true)});
 $('#wqpin').onclick=async()=>{const r=await api('api/workbench/pin',{pipeline:WB.sel.id,id:a.id,on:!pinned});if(r&&!r.error){WB.pins=r.pins;wbQStrip();wbQPanel()}};
 $('#wqcite').onclick=()=>{const t=`${a.question}\n`+(a.evidence||[]).map((e,i)=>`[${i+1}] ${e.text||''}${e.entity?' entity='+e.entity:''}${e.activity?' activity='+e.activity:''}`).join('\n')+`\nmoirai answer ${a.id}, pipeline ${a.pipeline}`;
  navigator.clipboard&&navigator.clipboard.writeText(t);$('#wqcite').textContent='✓ copied'};
 const au=$('#wqaudit');if(au)au.onclick=()=>{const x=$('#wqaud');x.hidden=!x.hidden};
 $('#wqwrong').onclick=async()=>{const why=prompt('What is wrong with this answer? The agent checks it against the traces and learns from it.');if(!why)return;
  await sendText(`[Feedback on answer ${a.id}] This answer looks wrong: ${why}. Check it against the traces, publish a corrected answer, and record the lesson.`,running,[])}}

// the graph: instances (when small enough, else the selected query's lineage) or the aggregated view
const INST_STYLE=dark=>[{selector:'node',style:{label:'data(label)','font-size':8,'text-valign':'bottom','text-margin-y':2,color:dark?'#cbd5e1':'#334155',width:12,height:12,'min-zoomed-font-size':7,'text-wrap':'wrap','text-max-width':170}},
 {selector:'node[kind="entity"]',style:{shape:'round-rectangle','background-color':'#bfdbfe','border-width':1,'border-color':'#1d4ed8'}},
 {selector:'node[kind="activity"]',style:{shape:'ellipse','background-color':'#fed7aa','border-width':1,'border-color':'#c2410c'}},
 {selector:'edge',style:{width:1,'curve-style':'bezier','target-arrow-shape':'triangle','arrow-scale':.6,'line-color':'#94a3b8','target-arrow-color':'#94a3b8'}},
 {selector:'edge[kind="generated"]',style:{'line-color':'#15803d','target-arrow-color':'#15803d'}},{selector:'edge[rel=1]',style:{'line-style':'dashed'}},
 {selector:'.dim',style:{opacity:.12}},{selector:'node.focus',style:{'border-width':3,'border-color':'#7c3aed',width:16,height:16,'font-size':9,'font-weight':600}},
 {selector:'edge.focus',style:{width:2.2,'line-color':'#7c3aed','target-arrow-color':'#7c3aed'}},{selector:'node.sens',style:{'background-color':'#fecaca'}},
 {selector:'node.pick',style:{'background-color':'#f59e0b','border-color':'#b45309','border-width':4,width:22,height:22,'z-index':9}}];
async function drawInst(box,nodes,edges,onTap){try{await dgNeedGraph()}catch(e){box.textContent=e.message;return null}
 const ids=new Set(nodes.map(n=>n.id));box.innerHTML='';
 const cy=cytoscape({container:box,wheelSensitivity:.25,minZoom:.05,maxZoom:3,
  elements:[...nodes.map(n=>({data:{...n,id:n.id,label:n.label||n.id}})),...edges.filter(e=>ids.has(e.source)&&ids.has(e.target)).map((e,i)=>({data:{id:'e'+i,source:e.source,target:e.target,kind:e.kind,rel:e.relation?1:0}}))],
  layout:{name:'dagre',rankDir:(box.clientHeight>box.clientWidth*.8?'TB':'LR'),nodeSep:10,rankSep:40,animate:false},style:INST_STYLE(themeNow()==='dark')});
 cy.on('tap','node',e=>onTap&&onTap(e.target.data()));cy.on('tap',e=>{if(e.target===cy)document.querySelectorAll('.wq-info').forEach(b=>b.hidden=true)});hoverTip(cy,box);return cy}
function wqStarts(a){const g=Q.g,ns=(g&&g.nodes)||[],ids=new Set(ns.map(n=>n.id)),out=new Set;
 const ln=(a.lineage&&a.lineage.nodes)||[];
 (a.targets||[]).forEach(t=>{if(ids.has(t))out.add(t);else ns.filter(n=>n.kind==='entity'&&((n.uri||'').includes(t)||n.type===t)).slice(0,40).forEach(n=>out.add(n.id))});
 if(!out.size)(a.steps||[]).forEach(s=>(s.cites||[]).forEach(c=>{const i=recId(c);if(ids.has(i))out.add(i)}));   // targets that are captured values (parameters): what they describe
 if(!out.size)ln.filter(n=>n.start).forEach(n=>ids.has(n.id)&&out.add(n.id));
 if(!out.size)ln.filter(n=>n.kind==='entity').forEach(n=>ids.has(n.id)&&out.add(n.id));
 return out}
// the subgraph that answers the query: its targets, its lineage and every record it cites, with what each of them directly used and generated
function wqAnswer(a){const g=Q.g;if(!g||g.truncated||!g.nodes)return null;const ids=new Set(g.nodes.map(n=>n.id)),core=new Set(wqStarts(a));
 ((a.lineage||{}).nodes||[]).forEach(n=>ids.has(n.id)&&core.add(n.id));(a.steps||[]).forEach(s=>(s.cites||[]).forEach(c=>{const i=recId(c);if(ids.has(i))core.add(i)}));
 if(!core.size)return null;const out=new Set(core);g.edges.forEach(e=>{if(e.relation)return;if(core.has(e.source))out.add(e.target);if(core.has(e.target))out.add(e.source)});return out}
function wqPart(a){return Q.reach?wqScope(a):(wqAnswer(a)||wqScope(a))}
// lay a graph out to fill its box: each connected piece by dagre in the direction that fits the box best, the pieces packed in rows
function packLayout(cy,box){const W=box.clientWidth||800,H=box.clientHeight||600,gap=30,comps=cy.elements().components();if(!comps.length)return;
 const run=(c,dir)=>{c.layout({name:'dagre',rankDir:dir,nodeSep:10,rankSep:40,animate:false,fit:false}).run();return c.boundingBox({includeLabels:true})};
 const parts=comps.map(c=>{const t=run(c,'TB'),zt=Math.min(W/Math.max(t.w,1),H/Math.max(t.h,1));let bb=run(c,'LR');
  if(zt>Math.min(W/Math.max(bb.w,1),H/Math.max(bb.h,1)))bb=run(c,'TB');return {c,bb}});
 if(parts.length>1){const area=parts.reduce((s,p)=>s+(p.bb.w+gap)*(p.bb.h+gap),0),rowW=Math.max(Math.max(...parts.map(p=>p.bb.w)),Math.sqrt(area*W/H));
  parts.sort((x,y)=>y.bb.h-x.bb.h);let x=0,y=0,rh=0;
  cy.batch(()=>parts.forEach(p=>{if(x>0&&x+p.bb.w>rowW){x=0;y+=rh+gap;rh=0}const dx=x-p.bb.x1,dy=y-p.bb.y1;
   p.c.nodes().positions(n=>({x:n.position('x')+dx,y:n.position('y')+dy}));x+=p.bb.w+gap;rh=Math.max(rh,p.bb.h)}))}}
// the selected query's data: from its start entities, everything upstream (lineage) and downstream (impact) through used/generated edges
function wqScope(a){const g=Q.g;if(!g||g.truncated||!g.nodes)return null;const start=wqStarts(a);if(!start.size)return null;
 const fwd={},back={};g.edges.forEach(e=>{if(e.relation)return;(fwd[e.source]=fwd[e.source]||[]).push(e.target);(back[e.target]=back[e.target]||[]).push(e.source)});
 const seen=new Set(start);for(const adj of [fwd,back]){const q=[...start];while(q.length&&seen.size<1500){const x=q.shift();for(const y of adj[x]||[])if(!seen.has(y)){seen.add(y);q.push(y)}}}
 return seen}
async function wqDraw(){const box=$('#wqgraph');if(!box||!WB.sel)return;Q.drawn=Date.now();Q.drawnFor=Q.sel||null;const leg=$('#wqleg');
 if(Q.gmode==='agg'){Q.cy=null;if(leg)leg.innerHTML=aggLegend('runtime').replace('class="ag-leg"','class="ag-leg"')+' <span><i class="f"></i>the query’s part</span>';
  let d=await api('api/workbench/aggregate?pipeline='+encodeURIComponent(WB.sel.id));if(!$('#wqgraph'))return;
  if(d&&d.mode==='diff'){d.nodes.forEach(n=>{if(n.status==='matched')n.status=''});d.edges.forEach(e=>{if(e.status==='matched')e.status=''})}
  const a=WB.answers.find(x=>x.id===Q.sel);
  if(a&&d&&d.nodes){const sc=wqPart(a),hl=wqAggHl(a),types=new Set(hl.types),acts=new Set(hl.acts);   // only the types and activities of the query's data
   if(sc)(Q.g.nodes||[]).forEach(n=>{if(sc.has(n.id))(n.kind==='entity'?types:acts).add(n.kind==='entity'?n.type:n.label)});
   const keep=new Set(d.nodes.filter(n=>(n.kind==='type'?types:acts).has(n.label)).map(n=>n.id));
   if(keep.size){d={...d,nodes:d.nodes.filter(n=>keep.has(n.id)),edges:d.edges.filter(e=>keep.has(e.source)&&keep.has(e.target))}}}
  Q.filtered=!!a;AGG.q=null;await drawAgg('q','wqgraph',d,null,x=>aggInfo('wqinfo',x));wqHighlight(true);return}
 if(leg)leg.innerHTML=Q.sel?'<i class="e"></i>entity <i class="a"></i>activity <i class="f"></i>the answer’s records <i class="p"></i>selected step · '+(Q.reach?'the answer’s data with everything upstream and downstream of it':'only the records the answer used, with their direct inputs and outputs'):'<i class="e"></i>entity <i class="a"></i>activity · the whole graph: select a query to draw only its data';
 const g=Q.g,a=WB.answers.find(x=>x.id===Q.sel),whole=g&&!g.truncated&&g.nodes;let nodes,edges;Q.part=!whole;Q.filtered=false;
 const sc=a&&whole?wqPart(a):null;
 if(sc){nodes=g.nodes.filter(n=>sc.has(n.id));edges=g.edges.filter(e=>sc.has(e.source)&&sc.has(e.target));Q.filtered=true}   // the query's data only, laid out afresh
 else if(a&&a.lineage&&a.lineage.nodes.length){nodes=a.lineage.nodes;edges=a.lineage.edges;Q.filtered=true}   // its records are not in this graph (other traces): its own subgraph
 else if(whole){nodes=g.nodes;edges=g.edges}
 else{Q.cy=null;box.innerHTML=`<div class="sub" style="padding:16px">${g?`The graph has ${g.counts.entities+g.counts.activities} nodes, too many to draw one by one: select a query to draw its part, or switch to Aggregated.`:'No provenance yet.'}</div>`;return}
 const adj={};edges.forEach(e=>{if(e.relation)return;if(e.kind==='used')(adj[e.target]=adj[e.target]||{gen:[],used:[]}).used.push(e.source);else(adj[e.source]=adj[e.source]||{gen:[],used:[]}).gen.push(e.target)});Q.adj=adj;
 Q.cy=await drawInst(box,nodes,edges,x=>wqInfoAt('wqinfo',x));if(Q.cy)packLayout(Q.cy,box);
 if(a&&!Q.filtered&&leg)leg.insertAdjacentHTML('beforeend',`<div class="xp-note warn">⚠ This answer's records are not in this graph: it was computed on other traces (${esc([...new Set((a.reads||[]).map(r=>r.file))].join(', '))}). Ask it again here to compute it on these traces and draw its part.</div>`);
 wqHighlight(true)}

function wqResolve(ref){if(!Q.cy||!ref)return[];ref=String(ref);const n=Q.cy.getElementById(ref);if(n&&n.length)return[ref];
 return Q.cy.nodes('[kind="entity"]').filter(x=>(x.data('uri')||'').includes(ref)||x.data('type')===ref||x.data('label')===ref).map(x=>x.id()).slice(0,80)}
function wqActs(name,E){const all=Q.cy.nodes('[kind="activity"]').filter(x=>x.data('label')===name).map(x=>x.id());
 const made=all.filter(id=>(Q.adj[id]||{gen:[]}).gen.some(x=>E.has(x)));if(made.length)return made.slice(0,80);
 const near=all.filter(id=>(Q.adj[id]||{used:[]}).used.some(x=>E.has(x)));return (near.length?near:all).slice(0,80)}
function wqFocus(a){const E=new Set;if(Q.part){Q.cy.nodes().forEach(n=>E.add(n.id()));return {E,A:new Set}}
 if(a.engine==='traceviewer'){const A=new Set;((a.lineage||{}).nodes||[]).forEach(n=>(n.kind==='entity'?E:A).add(n.id));(a.targets||[]).forEach(t=>E.add(t));
  (a.steps||[]).forEach(s=>(s.cites||[]).forEach(c=>(c.id?E:A).add(recId(c))));return {E,A}}
 (a.lineage&&a.lineage.nodes||[]).filter(n=>n.kind==='entity').forEach(n=>E.add(n.id));
 (a.targets||[]).forEach(t=>wqResolve(t).forEach(x=>E.add(x)));(a.evidence||[]).forEach(e=>e.entity&&wqResolve(e.entity).forEach(x=>E.add(x)));
 const A=new Set;Object.entries(Q.adj).forEach(([id,j])=>{if(j.gen.some(x=>E.has(x))&&(j.used.some(x=>E.has(x))||!j.used.length))A.add(id)});
 (a.evidence||[]).forEach(e=>e.activity&&wqActs(e.activity,E).forEach(x=>A.add(x)));return {E,A}}
// the aggregated view's highlight: the types and activity kinds the query's part is made of
function wqAggHl(a){const ln=(a.lineage&&a.lineage.nodes)||[],types=new Set,acts=new Set,pick=new Set;
 if(a.engine==='traceviewer'){const tOf={};ln.forEach(n=>{if(n.kind==='entity')tOf[n.id]=n.type});((Q.g&&Q.g.nodes)||[]).forEach(n=>{if(n.kind==='entity')tOf[n.id]=n.type});
  ln.forEach(n=>n.kind==='entity'?n.type&&types.add(n.type):acts.add(n.label));
  const actName={};((Q.g&&Q.g.nodes)||[]).forEach(n=>{if(n.kind==='activity')actName[n.id]=n.label});ln.forEach(n=>{if(n.kind==='activity')actName[n.id]=n.label});
  (a.steps||[]).forEach((s,i)=>(s.cites||[]).forEach(c=>{const ty=c.id?tOf[c.id]:null,an=c.id?null:(c.rec||actName[recId(c)]);if(ty)types.add(ty);if(an&&c.rec!=='EH')acts.add(an);
   if(Q.pick==='s'+(i+1)){if(ty)pick.add('t:'+ty);if(an&&c.rec!=='EH')pick.add('a:'+an)}}));return {types,acts,pick}}
 ln.forEach(n=>n.kind==='entity'?n.type&&types.add(n.type):acts.add(n.label));
 const tOf=ref=>{const hit=ln.filter(n=>n.kind==='entity'&&(n.id===ref||n.type===ref||(n.label||'').includes(ref)||String(ref).includes(n.label||'\0')));return hit.length?hit.map(n=>n.type):[ref]};
 (a.evidence||[]).forEach(e=>{if(e.activity)acts.add(e.activity);if(e.entity)tOf(e.entity).forEach(t=>t&&types.add(t))});
 if(Q.pick==='t')(a.targets||[]).forEach(t=>tOf(t).forEach(x=>pick.add('t:'+x)));
 else if(Q.pick&&Q.pick[0]==='e'){const e=(a.evidence||[])[+Q.pick.slice(1)]||{};if(e.entity)tOf(e.entity).forEach(x=>pick.add('t:'+x));if(e.activity)pick.add('a:'+e.activity)}
 return {types,acts,pick}}
function wqHighlight(fit){const a=WB.answers.find(x=>x.id===Q.sel);
 if((Q.drawnFor||null)!==(Q.sel||null)&&(Q.cy||AGG.q))return wqDraw();          // another query (or the whole graph): draw its data
 if(Q.gmode==='agg'){const c=AGG.q&&AGG.q.cy;if(!c)return;aggHl(c,a?wqAggHl(a):null);if(Q.filtered)c.elements().removeClass('dim');if(fit)aggFit(c);return}
 if(Q.g&&Q.g.truncated&&a){const n0=((a.lineage||{}).nodes||[])[0];if(n0&&(!Q.cy||!Q.cy.getElementById(n0.id).length))return wqDraw()}
 const cy=Q.cy;if(!cy)return;
 cy.batch(()=>{cy.elements().removeClass('dim focus pick sens');if(!a)return;
  const {E,A}=wqFocus(a),keep=new Set([...E,...A]);
  if(!Q.filtered)cy.elements().addClass('dim');cy.nodes().filter(n=>keep.has(n.id())).removeClass('dim').addClass('focus');
  cy.edges().filter(e=>keep.has(e.source().id())&&keep.has(e.target().id())).removeClass('dim').addClass('focus');
  if(a.sensitive){const s=new Set(a.sensitive.lineage||[]);cy.nodes().filter(n=>s.has(n.id())).removeClass('dim').addClass('sens')}
  let pk=[];if(Q.pick&&Q.pick[0]==='s'&&Q.pick!=='s'&&a.engine==='traceviewer'){const s=(a.steps||[])[+Q.pick.slice(1)-1];if(s)pk=(s.cites||[]).map(recId)}
  else if(Q.pick==='t')(a.targets||[]).forEach(t=>pk.push(...wqResolve(t)));
  else if(Q.pick==='s')pk=(a.sensitive&&a.sensitive.lineage)||[];
  else if(Q.pick&&Q.pick[0]==='e'){const e=(a.evidence||[])[+Q.pick.slice(1)]||{};if(e.entity)pk.push(...wqResolve(e.entity));if(e.activity)pk.push(...wqActs(e.activity,E))}
  const ps=new Set(pk);cy.nodes().filter(n=>ps.has(n.id())).removeClass('dim').addClass('pick')});
 if(!fit)return;const sel=cy.$('.pick').length?cy.$('.pick'):Q.filtered&&!Q.reach?cy.elements():cy.$('.focus');
 cy.stop();cy.fit(sel.length?sel:cy.elements(),sel.length&&sel.length<4?60:24);if(cy.zoom()>1.3){cy.zoom({level:1.3,renderedPosition:{x:cy.width()/2,y:cy.height()/2}});cy.center(sel.length?sel:undefined)}}
function wqInfoAt(boxId,d){const b=$('#'+boxId);if(!b)return;const ref=d.uri||d.label;b.hidden=false;
 b.innerHTML=`<b>${esc(d.kind)}</b> ${esc(String(d.label).split('\n')[0])}${d.type?` <span class="sub">(${esc(d.type)})</span>`:''}${d.uri?`<div class="sub" title="${esc(d.uri)}">${esc(d.uri)}</div>`:''}${propList(d)}${d.kind==='activity'&&d.dur!=null?`<div class="sub">pid ${esc(d.pid)} · ${dur((d.dur||0)/1e6)}</div>`:''}
  <div class="wq-ia">${d.kind==='entity'?`<button data-dir="back" title="Computed now with TraceViewer queries: no agent">where did it come from?</button><button data-dir="forward" title="Computed now with TraceViewer queries: no agent">what did it affect?</button>`:''}<button data-x>✕</button></div>`;
 b.querySelectorAll('[data-dir]').forEach(x=>x.onclick=()=>{const back=x.dataset.dir==='back',nm=String(d.label||d.id).split('\n')[0];
  askPlan(back?`Where did ${nm} come from?`:`What did ${nm} affect downstream?`,back?[{op:'entities',id:d.id},{op:'lineage',from:0,direction:'back'},{op:'ends',from:1}]:
   [{op:'entities',id:d.id},{op:'lineage',from:0,direction:'forward'},{op:'group',from:1,by:'type'}])});b.querySelector('[data-x]').onclick=()=>b.hidden=true}

function wbReport(){const pins=WB.answers.filter(a=>WB.pins.includes(a.id));if(!pins.length){alert('Pin answers first (📌 on an answer).');return}
 const w=window.open('','_blank');if(!w)return;const name=(WB.sel&&WB.sel.item&&WB.sel.item.name)||'';
 w.document.write(`<!doctype html><meta charset="utf-8"><title>${esc(name)} provenance report</title><style>body{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:30px auto;color:#1d232b}h1{font-size:22px}h2{font-size:16px;margin-top:28px;border-top:1px solid #ddd;padding-top:14px}.v{color:#15803d}.u{color:#b91c1c}li{margin:2px 0}code{background:#f1f5f9;padding:0 3px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:3px 6px}</style>
  <h1>Provenance report: ${esc(name)}</h1><p>${new Date().toLocaleString()} · ${pins.length} answers</p>`+
  pins.map((a,i)=>`<h2>${i+1}. ${esc(a.question)}</h2><p class="${a.verified?'v':'u'}">${a.verified?'Verified against the traces':'Unverified'} · confidence ${Math.round((a.confidence||0)*100)}% · ${a.stats.sources} sources, ${a.stats.artifacts} artifacts</p>
   ${a.verified?md(a.answer||''):'<p class="u">'+(a.problems||[]).map(esc).join('<br>')+'</p>'}${a.sensitive?`<p class="u">${a.sensitive.count} entities derive from sensitive inputs: restrict or review before sharing.</p>`:''}
   <p><b>Evidence</b></p><ol>${(a.evidence||[]).map(e=>`<li>${esc(e.text||'')}${e.entity?` <code>${esc(e.entity)}</code>`:''}${e.activity?` <code>${esc(e.activity)}</code>`:''}</li>`).join('')}</ol>${(a.gaps||[]).length?`<p><b>Gaps</b>: ${a.gaps.map(esc).join('; ')}</p>`:''}<p style="color:#64748b;font-size:12px">answer ${esc(a.id)}</p>`).join('')+'<script>setTimeout(()=>print(),300)<\/script>');
 w.document.close()}

// ---- answer cards in the chat (compact), from the answer_publish call: click to see it in the Query view
const _askCard0=Renderer.prototype.ev;
Renderer.prototype.ev=function(e){if(e.type==='tool_result'&&this.answerIds&&this.answerIds[e.id]&&this.answerIds[e.id]._tv){let r=null;try{r=JSON.parse(e.text)}catch(_){}
  const q=this.answerIds[e.id];if(r&&r.id&&(r.steps||r.synthesis_check)){const c=document.createElement('div'),last=(r.steps||[]).slice(-1)[0],sc=r.synthesis_check;c.className='acmini ok';
   c.innerHTML=r.steps?`<span class="ac-badge ok">✓ computed from the traces</span> <b>${esc(q.question||'')}</b><div>${esc(last?last.result:'')}</div><div class="sub">${r.stats?`${r.stats.queries} TraceViewer queries · ${r.stats.rows_read} records read · ${r.stats.cited} cited`:''} · click to see the plan, the evidence and the graph</div>`:
    `<span class="ac-badge ${sc.ok?'ok':'bad'}">${sc.ok?'✓ write-up checked':'⚠ write-up flagged'}</span> ${sc.ok?'every claim cites a step; every number is in the result':esc((sc.problems||[]).join(' · '))}`;
   c.style.cursor='pointer';c.onclick=()=>wbShowAnswer(r.id);this.b.appendChild(c);if(WB.sel&&WB.view==='query')setTimeout(()=>wbQuery(true),500)}}
 else if(e.type==='tool_result'&&this.answerIds&&this.answerIds[e.id]){let r=null;try{r=JSON.parse(e.text)}catch(_){}
  if(r&&r.id){const q=this.answerIds[e.id],c=document.createElement('div');c.className='acmini '+(r.verified?'ok':'unv');
   c.innerHTML=`<span class="ac-badge ${r.verified?'ok':'bad'}">${r.verified?'✓ verified':'⚠ unverified'}</span> <b>${esc(q.question||'')}</b>${r.verified?`<div>${md(q.answer||'')}</div>`:`<div class="sub">${(r.problems||[]).map(esc).join(' · ')}</div>`}<div class="sub">${r.stats?`${r.stats.sources} sources · ${r.stats.artifacts} artifacts`:''}${r.sensitive?` · ⛔ ${r.sensitive.count} sensitive-derived`:''} · click to see it on the graph with its evidence</div>`;
   c.title='show it in the Query view';c.style.cursor='pointer';c.onclick=()=>wbShowAnswer(r.id);
   this.b.appendChild(c);if(WB.sel&&WB.view==='query')setTimeout(()=>wbQuery(true),500)}}
 if(e.type==='tool_use'&&/(answer_publish|evidence_answer|evidence_synthesis)$/.test(e.name||'')){let q={};try{q=JSON.parse(e.input)}catch(_){}q._tv=/evidence_/.test(e.name);(this.answerIds=this.answerIds||{})[e.id]=q}
 return _askCard0.call(this,e)};
setInterval(()=>{if(document.body.classList.contains('views')&&document.visibilityState==='visible'&&!WB.sel)wbLoad()},15000);
{let r='studio';try{r=localStorage.getItem('moirai_role')||'studio'}catch(_){}WB.role=r;document.body.classList.toggle('wb',r==='domain');
 document.querySelectorAll('#roleseg button').forEach(b=>b.classList.toggle('on',b.dataset.r===r))}
// the empty chat of a view says what that chat is for
const _renderEmpty0=renderEmpty;
renderEmpty=function(){if(!document.body.classList.contains('views'))return _renderEmpty0();lastDay=null;$('#pdfbtn').disabled=true;
 const it=WB.sel&&WB.sel.item,H={ann:['Annotate chat',it?`The chat of ${it.name}: the agent asks here before each step.`:'Start on the right, or tell the agent your app here: the directory and a small test command.',
   [['Annotate my app','Annotate my application for provenance. App directory: <path>. Small test command: <command>.'],['Continue','Where is my pipeline, and what is the next step?']]],
  live:['Live run chat',it?`The chat of ${it.name}: it starts the run, watches it and publishes its data.`:'Pick a live run on the left, or start one on the right.',
   [['How is it going?','How is the run going? Anything stalled or failing?'],['Publish','The run ended: validate it and publish its data.']]],
  query:['Query chat',it?`Ask about ${it.name}: where something came from, what it affected. Each answer shows on the right with its evidence.`:'Pick a run or a dataset on the left.',
   ((it&&WQ_ASK[it.kind])||WQ_ASK.live).slice(0,3).map(q=>[q.split(/[?:]/)[0]+'?',q])]}[WB.view];
 $('#log').innerHTML=`<div class="empty"><h2>${H[0]}</h2><div>${esc(H[1])}</div><div class="starters">${H[2].map(s=>`<div class="starter" data-q="${esc(s[1])}"><b>${esc(s[0])}</b>${esc(s[1])}</div>`).join('')}</div></div>`;
 document.querySelectorAll('.starter').forEach(e=>e.onclick=()=>{$('#input').value=e.dataset.q;$('#input').focus()})};

// a turn starting or ending changes whether a view waits for the user: redraw at once, not at the next poll
{const _setRunning0=setRunning;setRunning=function(v){_setRunning0(v);if(WB.sel&&document.body.classList.contains('views')){delete $('#wbview').dataset.h;wbTick(false)}}}

// ---- a computed answer (core.evidence): the plan, the result computed from TraceViewer queries, the write-up that cites it, and the trail
const recId=c=>c.id||`${c.pid}:${c.ts}`;
const isoUs=us=>us?new Date(us/1000).toLocaleString():'-';
const OPWORD={entities:'entities',activities:'activities',lineage:'lineage',group:'count',ends:'ends',inputs:'parameters',outputs:'outputs',attributes:'attributes'};
function stepArgs(s){const a=s.args||{},r=s.resolved||{},bits=[];
 if(a.all)bits.push('all');if(a.type)bits.push(`type ${a.type}`);if(a.uri_contains)bits.push(`uri contains “${a.uri_contains}”`);if(a.id)bits.push(`id ${a.id}`);if(a.ids)bits.push(`${a.ids.length} ids`);
 if(a.name)bits.push(a.name);if(a.activity)bits.push(`of ${a.activity}`);if(a.pid!=null)bits.push(`pid ${a.pid}`);if(a.from!=null)bits.push(`from [S${a.from+1}]`);if(a.direction)bits.push(a.direction==='back'?'backward (where it came from)':'forward (what it affected)');if(a.by)bits.push(`by ${a.by}`);
 const tm=r.time||r.produced;if(tm)bits.push(`time ${tm[0]} → ${tm[1]}${a.since||a.until||a.produced_since||a.produced_until?` (from “${[a.since||a.produced_since,a.until||a.produced_until].filter(Boolean).join(' / ')}”)`:''}`);
 return bits.join(' · ')}
function synthHtml(t){return md(t||'').replace(/\[S(\d+)\]/g,(m,n)=>`<a href="#" class="scite" data-pk="s${n}" title="step ${n}: select it and its records on the graph">S${n}</a>`).replace(/\[R(\d+)\]/g,(m,n)=>`<a href="#" class="rcite" data-row="${n}" title="open row ${n} of the result table">R${n}</a>`)}
// [R n]: open the result's details and show that row
document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('.rcite');if(!a)return;e.preventDefault();e.stopPropagation();
 const d=document.querySelector('#wqpanel details[data-xp=result]');if(!d)return;d.open=true;XPOPEN.result=true;
 const tr=d.querySelector(`table[data-anchor=result] tr[data-row="${a.dataset.row}"]`);if(!tr)return;tr.scrollIntoView({block:'center',behavior:'smooth'});
 tr.classList.remove('flash');void tr.offsetWidth;tr.classList.add('flash')},true);
// ---- an answer explained in four parts (the query_explain capability): summary first, details folded, one look per kind of block
const XP={},XPOPEN={},CF={};
const CF_HOW={query:'TraceViewer query',derived:'derived from a step',narrative:'from the result',llm_verified:'query values, LLM wording',wording:'wording only (not counted)',llm_query:'agent query, LLM summary',llm:'value derived by the LLM',gap:'gap'};
// the confidence capability's score: how much of the answer was calculated vs inferred, every LLM-inferred part flagged
function cfHtml(c){if(!c||c.loading)return '<div class="cf sub">confidence: computing…</div>';if(c.error)return `<div class="cf sub">confidence: ${esc(c.error)}</div>`;
 const tone=c.score>=90?'hi':c.score>=70?'mid':c.score>=40?'lo':'bad';
 const rows=(c.items||[]).map(i=>`<tr class="${i.llm?'llm':''}"><td><span class="cf-how ${esc(i.how)}">${esc(CF_HOW[i.how]||i.how)}</span></td><td>${synthHtml(i.what||'')}${i.why?`<div class="sub">${esc(i.why)}</div>`:''}</td><td class="n">${i.weight??'–'}</td></tr>`).join('');
 return `<details class="cf cf-${tone}" ontoggle="XPOPEN.conf=this.open" ${XPOPEN.conf?'open':''}><summary><span class="cf-score">${c.score}</span><span class="cf-main"><b>Confidence: ${esc(c.label)}</b>${c.llm_inferred?` <span class="cf-llm">${c.llm_inferred} with values derived by the LLM</span>`:''}<span class="cf-bar"><i style="width:${c.score}%"></i></span><span class="sub">${esc(c.summary)} ${esc(c.data||'')}</span></span></summary>
  <div class="xp-tw"><table class="xp-t"><thead><tr><th>how it was obtained</th><th>part of the answer</th><th class="n">weight</th></tr></thead><tbody>${rows}</tbody></table></div><div class="sub">score = the weighted share of the parts × 100; halved when the traces no longer give the result</div></details>`}
function cfOf(a){const k=a.id+':'+(a.result_hash||'')+':'+(a.synthesis||'').length+':'+(a.ts||'')+':'+(a.answer||'').length;if(!CF[k]){CF[k]={loading:1};api('api/workbench/confidence?pipeline='+encodeURIComponent(WB.sel.id)+'&id='+encodeURIComponent(a.id)).then(r=>{CF[k]=r||{error:'failed'};if(Q.sel===a.id)wbQPanel()})}return CF[k]}
function xpVal(v){if(Array.isArray(v))return `<span class="xp-chips">${v.map(x=>`<span class="xp-chip${x==='…'?' more':''}">${esc(x)}</span>`).join('')}</span>`;
 const t=String(v??'');if(!t)return '<span class="sub">–</span>';
 const shown=/^\/|^[a-z]+:\/\//.test(t)?`<code>${esc(t)}</code>`:esc(t);
 return t.length>90?`<span class="xp-cell" title="click to show all">${shown}</span>`:shown}
function xpBlock(b){const T=b.type;
 if(T==='text')return `<div class="xp-text">${synthHtml(b.text)}</div>`;
 if(T==='kv')return `<dl class="xp-kv">${(b.rows||[]).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${xpVal(v)}</dd>`).join('')}</dl>`;
 if(T==='table'){const num=new Set(b.numeric||[]);
  const an=b.anchor;
  return `<div class="xp-tw"><table class="xp-t"${an?` data-anchor="${esc(an)}"`:''}><thead><tr>${an?'<th class="rn">#</th>':''}${b.columns.map((c,i)=>`<th class="${num.has(i)?'n':''}">${esc(c)}</th>`).join('')}</tr></thead><tbody>${(b.rows||[]).map((r,ri)=>`<tr${an?` data-row="${ri+1}"`:''}>${an?`<td class="rn">R${ri+1}</td>`:''}${r.map((c,i)=>`<td class="${num.has(i)?'n':''}">${xpVal(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${b.total>(b.rows||[]).length?`<div class="sub">showing ${b.rows.length} of ${b.total}</div>`:''}`}
 if(T==='query')return `<div class="xp-q"><div class="xp-qh"><b>${esc(b.label||'')}</b> <span>${esc(b.why||'')}</span><span class="xp-meta">${esc(b.meta||'')}</span></div><pre><code>${esc(b.code||'')}</code></pre></div>`;
 if(T==='stats')return `<div class="xp-facts">${(b.items||[]).map(x=>`<span><b>${esc(x.value)}</b> ${esc(x.label)}</span>`).join('')}</div>`;
 if(T==='list')return `<ul class="xp-list">${(b.items||[]).map(x=>`<li>${synthHtml(x)}</li>`).join('')}</ul>`;
 if(T==='note')return `<div class="xp-note ${b.tone==='ok'?'ok':'warn'}">${b.tone==='ok'?'✓':'⚠'} ${esc(b.text||'')}</div>`;
 return `<pre>${esc(JSON.stringify(b))}</pre>`}
const fmtFact=v=>typeof v==='number'?v.toLocaleString():esc(String(v));
function xpPart(x,n,a,chk){const groups=(x.details||[]).filter(g=>!(x.part==='answer'&&g.title==='Gaps'&&(a.gaps||[]).length)),k=x.part,open=XPOPEN[k];
 const steps=k==='plan'?`<div class="xp-steps">${(a.steps||[]).map(s=>`<a href="#" class="xp-step${Q.pick==='s'+s.step?' on':''}" data-pk="s${s.step}" title="show S${s.step}'s records on the graph">S${s.step} ${esc(OPWORD[s.op]||s.op)}</a>`).join('')}</div>`:'';
 const head=k==='result'?`<h4 class="wq-pk${Q.pick?'':' on'}" data-pk="">`:'<h4>';
 return `<section class="wq-sec xp" data-part="${k}">${head}<span class="n">${n}</span>${esc(x.title||k)}</h4>
  ${chk?(()=>{const pr=(chk.problems||[]).filter(x=>x!==chk.shared);return `<span class="xp-chk ${chk.status==='pass'?'ok':'bad'}" title="${esc(chk.data||'')}">${chk.status==='pass'?'✓ checked against the traces':pr.length?'⚠ '+pr.length+' problem'+(pr.length===1?'':'s')+' against the traces':'⚠ result changed: see above'}</span>`})():''}
  <p class="xp-sum">${synthHtml(x.summary||'')}</p>${(x.body||[]).map(xpBlock).join('')}${chk&&(chk.problems||[]).filter(x=>x!==chk.shared).length?`<div class="xp-note warn">⚠ ${(chk.problems||[]).filter(x=>x!==chk.shared).slice(0,5).map(esc).join('<br>')}</div>`:''}${(x.facts||[]).length?`<div class="xp-facts">${x.facts.map(f=>`<span><b>${fmtFact(f.value)}</b> ${esc(f.label)}</span>`).join('')}</div>`:''}${steps}
  ${groups.length?`<details class="xp-more" data-xp="${k}" ${open?'open':''}><summary>details</summary>${groups.map(g=>`<div class="xp-g${g.pick&&Q.pick===g.pick?' on':''}"><div class="xp-gt${g.pick?' wq-pk':''}"${g.pick?` data-pk="${esc(g.pick)}" title="show these records on the graph"`:''}>${esc(g.title||'')}</div>${(g.blocks||[]).map(xpBlock).join('')}</div>`).join('')}</details>`:''}</section>`}
function wbQPanelTV(p,a){const on=k=>Q.pick===k?' on':'',st=a.stats||{},sc=a.synthesis_check||{},pinned=WB.pins.includes(a.id),steps=a.steps||[],fixable=WB.sel.item.kind!=='analysis'||!!WB.sel.item.parent;
 const plan=steps.map(s=>`<li class="wq-pk${on('s'+s.step)}" data-pk="s${s.step}"><b>S${s.step}</b> ${esc(OPWORD[s.op]||s.op)} <span class="sub">${esc(stepArgs(s))}</span><div class="tv-hl">${esc(s.headline)}</div>
   <div class="sub">${s.queries?`TraceViewer ${s.queries.map(n=>'Q'+n).join(', ')}`:`computed from [S${(s.computed_from??0)+1}]`} · ${s.cited} record${s.cited===1?'':'s'} cited</div></li>`).join('');
 const res=a.result||{};
 const syn=a.synthesis?`<div class="ap-ans">${synthHtml(a.synthesis)}</div><div class="${sc.ok?'tv-ok':'tv-bad'}">${sc.ok?'✓ every claim cites a step, and every number is in the computed result':'⚠ '+(sc.problems||[]).map(esc).join(' · ')}</div>`:
  '<div class="sub">Not written up yet: the computed result above is the answer.</div>';
 const reads=(a.reads||[]).map(r=>`<tr><td>${esc(r.file)}</td><td>${fmtBytes(r.bytes)}</td><td><code>${esc(r.sha256_ends)}</code></td><td>${new Date(r.mtime*1000).toLocaleString()}</td></tr>`).join('');
 const qs=(a.queries||[]).map(q=>`<div class="tv-q"><b>Q${q.n}</b> <span class="sub">${esc(q.why)} · ${q.rows} row${q.rows===1?'':'s'} · ${q.secs}s</span><pre>${esc(q.query)}</pre></div>`).join('');
 const cites=steps.map(s=>`<details><summary>S${s.step}: ${s.cited} record${s.cited===1?'':'s'}</summary><table class="tv-t"><tr><th>record</th><th>file</th><th>pid</th><th>time</th><th>id</th></tr>${(s.cites||[]).slice(0,pro()?300:40).map(c=>`<tr><td>${esc(c.rec)}</td><td>${esc(c.file)}</td><td>${esc(c.pid)}</td><td>${c.ts?esc(isoUs(c.ts)):'-'}</td><td><code>${esc(c.id||'')}</code></td></tr>`).join('')}</table></details>`).join('');
 const key=a.id+':'+(a.result_hash||'')+':'+(a.synthesis||'').length+':'+(a.ts||''),xp=XP[key];      // a write-up added later is a new explanation
 if(!xp){XP[key]={loading:true};api('api/workbench/explain?pipeline='+encodeURIComponent(WB.sel.id)+'&id='+encodeURIComponent(a.id)).then(r=>{XP[key]=r&&!r.error&&r.parts?r:{error:(r&&r.error)||'failed'};if(Q.sel===a.id)wbQPanel()})}
 const gapsHtml=`${(a.gaps||[]).length?`<section class="wq-sec tv-gaps"><h4><span class="n">!</span>Gaps <span class="sub">what the provenance could not show</span></h4>
   <ul>${a.gaps.map(g=>`<li><b>${esc(GAP_L[g.kind]||g.kind)}</b> <code>${esc(g.subject)}</code> <a href="#" class="scite" data-pk="s${g.step}">S${g.step}</a><div class="sub">${esc(g.detail)}</div>
    ${fixable?`<button class="btn" data-gap="${esc(g.id)}" title="Open the app's Annotate chat: explore the code for this gap, fix the annotation, learn it in the capabilities">Improve coverage</button>`:''}</li>`).join('')}</ul>
   ${fixable&&a.gaps.length>1?`<button class="btn primary" data-gap="*">Improve coverage of all ${a.gaps.length} gaps</button>`:''}
   ${fixable?'':'<div class="sub">A dataset has no annotation here: improve it on the app that produced it.</div>'}</section>`:''}
`;
 const ck=(xp&&xp.checks)||{},fails=Object.values(ck).filter(c=>c.status!=='pass'),common=fails.length===4&&fails.every(c=>(c.problems||[])[0]===(fails[0].problems||[])[0])?fails[0].problems[0]:'',
  notes=[...new Set(Object.values(ck).map(c=>c.note).filter(Boolean))];
 if(common)Object.values(ck).forEach(c=>c.shared=common);
 const banner=(common?`<div class="xp-note warn">⚠ ${esc(common)}</div>`:'')+notes.map(n=>`<div class="xp-note info">ℹ ${esc(n)}</div>`).join('');
 const secs=xp&&xp.parts?cfHtml(xp.confidence)+banner+xp.parts.map((x,i)=>xpPart(x,i+1,a,ck[x.part])+(x.part==='answer'?gapsHtml:'')).join(''):`<div class="cf sub">checking the answer against the traces and scoring its confidence…</div>  <section class="wq-sec"><h4><span class="n">1</span>Plan <span class="sub">select a step to see its records on the graph</span></h4><ol class="tv-plan">${plan}</ol></section>
  <section class="wq-sec"><h4 class="wq-pk${Q.pick?'':' on'}" data-pk=""><span class="n">2</span>Result <span class="sub">computed, its whole part</span></h4><div class="tv-hl big">${esc(res.headline||'')}</div>${res.table?`<div class="ap-ans">${md(res.table)}</div>`:''}</section>
  <section class="wq-sec"><h4><span class="n">3</span>Answer <span class="sub">${a.synthesis?'written by the agent from the result; [S] cites a step':''}</span></h4>${syn}</section>
  ${(a.gaps||[]).length?`<section class="wq-sec tv-gaps"><h4><span class="n">!</span>Gaps <span class="sub">what the provenance could not show</span></h4>
   <ul>${a.gaps.map(g=>`<li><b>${esc(GAP_L[g.kind]||g.kind)}</b> <code>${esc(g.subject)}</code> <a href="#" class="scite" data-pk="s${g.step}">S${g.step}</a><div class="sub">${esc(g.detail)}</div>
    ${fixable?`<button class="btn" data-gap="${esc(g.id)}" title="Open the app's Annotate chat: explore the code for this gap, fix the annotation, learn it in the capabilities">Improve coverage</button>`:''}</li>`).join('')}</ul>
   ${fixable&&a.gaps.length>1?`<button class="btn primary" data-gap="*">Improve coverage of all ${a.gaps.length} gaps</button>`:''}
   ${fixable?'':'<div class="sub">A dataset has no annotation here: improve it on the app that produced it.</div>'}</section>`:''}
  <section class="wq-sec"><h4><span class="n">4</span>Evidence</h4>
   <div class="ap-stats"><span><b>${st.queries??'-'}</b> TraceViewer queries</span><span><b>${st.rows_read??'-'}</b> records read</span><span><b>${st.cited??'-'}</b> cited</span><span>result <code>${esc(a.result_hash||'')}</code></span></div>
   <details ${pro()?'open':''}><summary>Files read (${(a.reads||[]).length})</summary><table class="tv-t"><tr><th>file</th><th>size</th><th>sha256 of its ends</th><th>modified</th></tr>${reads}</table></details>
   <details><summary>Queries (${(a.queries||[]).length})</summary>${qs}</details>
   <details><summary>Cited records</summary>${cites}</details></section>
`;
 p.innerHTML=`<div class="wq-q"><span class="ac-badge ok" title="Every step is a TraceViewer query on the traces; the result is computed from the records">✓ computed from the traces</span><b>${esc(a.question||'')}</b>
   <span class="sub">${a.ts?new Date(a.ts*1000).toLocaleString():''} · plan chosen by ${a.plan&&a.plan.chosen_by==='you'?'you':'the agent'} · ${esc((a.engine_info||{}).name||'TraceViewer')} ${esc((a.engine_info||{}).version||'')} · ${a.secs}s</span></div>
  ${secs}
  <div class="ac-del"><button id="wqrerun" title="Run the same plan on the traces as they are now and compare">↻ re-run</button><button id="wqpin" class="${pinned?'on':''}">${pinned?'📌 pinned':'📌 pin to report'}</button><button id="wqcite">⧉ copy citations</button><button id="wqwrong" class="warn">this looks wrong</button></div><div id="wqrr" class="sub"></div>`;
 p.querySelectorAll('[data-pk]').forEach(x=>x.onclick=e=>{e.preventDefault();e.stopPropagation();Q.pick=Q.pick===(x.dataset.pk||null)&&x.dataset.pk?null:(x.dataset.pk||null);wbQPanel();wqHighlight(true)});
 p.querySelectorAll('details[data-xp]').forEach(d=>d.ontoggle=()=>{XPOPEN[d.dataset.xp]=d.open});
 p.querySelectorAll('.xp-cell').forEach(c=>c.onclick=e=>{e.stopPropagation();c.classList.toggle('open')});
 p.querySelectorAll('[data-gap]').forEach(b=>b.onclick=()=>improveGaps(a,b.dataset.gap==='*'?a.gaps.map(g=>g.id):[b.dataset.gap],b));
 $('#wqrerun').onclick=async()=>{const b=$('#wqrerun'),m=$('#wqrr');b.disabled=true;m.textContent='running the plan again…';
  const r=await api('api/workbench/rerun',{pipeline:WB.sel.id,id:a.id});b.disabled=false;
  m.innerHTML=!r||r.error?esc((r&&r.error)||'failed'):r.same?`✓ same result (${esc(r.result_hash)}), ${r.secs}s`:`⚠ the result changed: ${esc(r.was)} → ${esc(r.result_hash)}${r.files_changed.length?` · files changed: ${esc(r.files_changed.join(', '))}`:''} · now: ${esc(r.headline)}`};
 $('#wqpin').onclick=async()=>{const r=await api('api/workbench/pin',{pipeline:WB.sel.id,id:a.id,on:!pinned});if(r&&!r.error){WB.pins=r.pins;wbQStrip();wbQPanel()}};
 $('#wqcite').onclick=()=>{const t=`${a.question}\n${res.headline}\n`+steps.map(s=>`[S${s.step}] ${s.op} ${stepArgs(s)}: ${s.headline}\n`+(s.cites||[]).slice(0,20).map(c=>`    ${c.rec} ${c.file} pid=${c.pid} ts=${c.ts}${c.id?' id='+c.id:''}`).join('\n')).join('\n')+
  `\nqueries:\n`+(a.queries||[]).map(q=>`  Q${q.n} ${q.query}`).join('\n')+`\nresult ${a.result_hash} (moirai answer ${a.id}, pipeline ${a.pipeline})`;
  navigator.clipboard&&navigator.clipboard.writeText(t);$('#wqcite').textContent='✓ copied'};
 $('#wqwrong').onclick=async()=>{const why=prompt('What is wrong with this answer?');if(!why)return;
  await sendText(`[Feedback on answer ${a.id}] This looks wrong: ${why}. Check the plan (did it resolve the right entities and times?), run a corrected evidence_answer, and record the lesson.`,running,[])}}
// one-click questions from the graph: the page builds the plan itself, the server runs it - no agent, no model
async function askPlan(question,steps){if(!WB.sel)return;const m=document.querySelector('.wq-info');if(m)m.insertAdjacentHTML('beforeend','<div class="sub">running TraceViewer queries…</div>');
 const r=await api('api/workbench/ask',{pipeline:WB.sel.id,question,steps,session});if(!r||r.error){alert((r&&r.error)||'the query failed');return}
 Q.want=r.id;if(WB.view!=='query')goView('query',WB.sel.id);else wbQuery(true)}

// ---- gaps: improve the annotation's coverage of exactly what a query could not show, in the app's annotation phase
const GAP_L={reported:'reported by the answer',undeclared_entity:'entity referenced, never declared',missing_producer:'nothing recorded producing it',no_inputs:'inputs not recorded',no_outputs:'output not recorded',
 no_attributes:'attributes not recorded',not_recorded:'not recorded at all'};
async function improveGaps(a,ids,btn){if(btn)btn.disabled=true;
 const r=await api('api/workbench/gaps',{pipeline:WB.sel.id,answer:a.id,gaps:ids});if(btn)btn.disabled=false;if(!r||r.error){alert((r&&r.error)||'could not open the gap');return}
 const lines=r.gaps.map(g=>`- ${g.id} (${g.kind}) ${g.subject}: ${g.detail}`).join('\n');
 const msg=`Improve the provenance coverage for ${r.gaps.length===1?'this gap':'these gaps'}, which a query exposed (question: “${a.question}”, answer ${a.id} on pipeline ${a.pipeline}):\n${lines}\n\n`+
  GAP_HOW+'ask me before re-running the smoke or baseline, then re-run the question and gap_close.';
 toAppChat(r.app,r.chat,msg)}
const GAP_HOW='Load skill prov_gaps and follow it: explore the app\'s code for each, find why it was missed and name the cause, fix the annotation (capture inputs, outputs, '+
 'variables or attributes, or annotate the missed function or entity), learn it generically in prov_annotate and for this workload in its workload capability, ';
// the request goes to the app's own chat (its annotation phase), never to the query chat it came from
async function toAppChat(app,chat,msg){const querySession=session,sleep=ms=>new Promise(z=>setTimeout(z,ms));
 if(!(WB.view==='ann'&&WB.sel&&WB.sel.id===app))goView('ann',app);
 for(let i=0;i<30&&!(WB.sel&&WB.sel.item&&WB.sel.id===app);i++)await sleep(300);
 if(!WB.sel||WB.sel.id!==app){alert('Could not open the app in the Annotate view.');return}
 if(chat){if(session!==chat)await openSession(chat);for(let i=0;i<25&&session!==chat;i++)await sleep(200)}
 else if(session===querySession){$('#newbtn').onclick();setMode('run')}
 if(chat?session!==chat:session===querySession&&querySession){alert('Could not open the app\'s chat: the request is in the box.');$('#input').value=msg;return}
 if(running){alert('The app\'s chat is busy: your gap request is in the box; send it when the agent is done.');$('#input').value=msg;return}
 setMode('run');sendText(msg,false,[])}
// every gap of every query on this app (its runs, live runs, published datasets), collected onto the app and worked on in one request
async function workAllGaps(pid,btn){if(btn){btn.disabled=true;btn.textContent='collecting the gaps of every query…'}
 const r=await api('api/workbench/gaps/collect',{pipeline:pid});if(btn)btn.disabled=false;
 if(!r||r.error){alert((r&&r.error)||'could not collect the gaps');if(btn)btn.textContent='Collect gaps from all queries';return}
 if(!r.open.length){if(btn)btn.textContent=`No open gaps in ${r.answers} quer${r.answers===1?'y':'ies'}`;return}
 const lines=r.open.map(g=>{const f=g.from||{},n=1+(g.seen_in||[]).length;return `- ${g.id} (${g.kind}) ${g.subject}: ${g.detail}\n  shown by “${f.question||'?'}” (answer ${f.answer||'?'} on ${f.pipeline||'?'})${n>1?`, and ${n-1} more answer${n>2?'s':''}`:''}`}).join('\n');
 const msg=`Improve the provenance coverage for all ${r.open.length} open gap${r.open.length>1?'s':''} of this app, collected from every query asked about it (${r.answers} answer${r.answers===1?'':'s'}; gap_list has each one's question and plan):\n${lines}\n\n`+
  GAP_HOW+'and work them as ONE batch: group the gaps by function or entity, fix each group in one pass, ask me ONCE before a single re-run of the smoke or baseline, '+
  'then re-run every question that showed a gap and gap_close each.';
 toAppChat(r.app,r.chat,msg)}
function gapsCard(it){if(!Array.isArray(it.gaps))return '';const gs=it.gaps,open=gs.filter(g=>g.status==='open');
 return `<div class="wb-card"><div class="wb-sec">Provenance gaps <span class="sub">${open.length} open · ${gs.length-open.length} fixed</span></div><ul class="wb-gaps">${gs.slice().reverse().slice(0,pro()?30:8).map(g=>
  `<li class="${esc(g.status)}"><b>${esc(g.status==='open'?'open':g.status==='closed'?'✓ closed':'fixed, run pending')}</b> ${esc(GAP_L[g.kind]||g.kind)} <code>${esc(g.subject)}</code>${g.cause?` <span class="sub">· ${esc(g.cause.replace(/_/g,' '))}</span>`:''}${pro()&&g.how?`<div class="sub">${esc(g.how)}</div>`:''}</li>`).join('')}</ul>
  ${gs.length?'':'<div class="sub">What the queries on this app could not show. Collect them from every query asked so far, and fix them together.</div>'}
  <button class="btn primary" id="wbgapall" title="Collect the gaps of every query on this app, its live runs and its published datasets, and send them to the app's chat as one request">Collect gaps from all queries &amp; fix them together</button></div>`}

// ---- what is running now, from any tab: every app or run with a running job, a working agent, or a question for you
function activeOf(d){return [...d.apps,...d.lives,...d.datasets].filter(x=>(x.jobs||[]).length||x.busy||['running','working','needs_you'].includes(x.state))}
function nowText(x){const st=STEP_L[x.current]||x.current||'',j=x.jobs||[],agent=x.busy?'agent working':x.state==='needs_you'?'waiting for your answer':'agent idle';
 return `${st?esc(st)+' · ':''}${j.length?j.map(y=>`${esc(y.name)} ${dur(y.elapsed||0)}`).join(', ')+' · ':''}${agent}`}
function renderNow(d){let el=$('#wbnow');if(!el){const nav=document.querySelector('header nav.tabs');if(!nav)return;nav.insertAdjacentHTML('afterend','<div id="wbnow"></div>');el=$('#wbnow')}
 const act=activeOf(d),need=act.filter(x=>x.state==='needs_you'&&!x.busy).length,run=act.length-need;
 if(!act.length){el.innerHTML='';return}
 const open=el.classList.contains('open');
 el.innerHTML=`<button class="wbnow-b" title="What is running now, and where">${run?`<span class="wbd run"></span>${run} running`:''}${need?`${run?' · ':''}<span class="wbd wait"></span>${need} need${need>1?'':'s'} you`:''}</button>
  <div class="wbnow-p">${act.map(x=>{const v=x.kind==='live'?'live':x.kind==='analysis'?'query':'ann',[cls]=WB_STATE[x.busy?'working':x.state]||[''];
   return `<a href="#" data-now="${esc(x.id)}" data-v="${v}"><span class="wbd ${cls}"></span><b>${esc(x.name)}</b> <span class="sub">${esc(VIEW_L[v])}${x.app?' · run of '+esc(x.app):''}</span><div class="sub">${nowText(x)}</div>${(x.jobs||[]).map(y=>y.last?`<div class="wbnow-l">${esc(y.last)}</div>`:'').join('')}</a>`}).join('')}</div>`;
 el.classList.toggle('open',open);
 el.querySelector('.wbnow-b').onclick=e=>{e.stopPropagation();el.classList.toggle('open')};
 el.querySelectorAll('[data-now]').forEach(a=>a.onclick=e=>{e.preventDefault();el.classList.remove('open');goView(a.dataset.v,a.dataset.now)})}
document.addEventListener('click',e=>{const el=$('#wbnow');if(el&&!el.contains(e.target))el.classList.remove('open')});
setInterval(async()=>{if(!me||document.visibilityState!=='visible')return;if(document.body.classList.contains('views'))return wbLoadQuiet();
 const d=await api('api/workbench');if(d&&!d.error){WB.items=d;renderNow(d)}},10000);
// the Now card of a view: the step that is running, its jobs and what they print, and whether the agent is on it
function nowCard(it){const j=it.jobs||[],cur=STEP_L[it.current]||it.current;if(!j.length&&!agentBusy(it))return '';
 return `<div class="wb-now"><b>▶ Now:</b> ${cur?esc(cur):''}${j.map(y=>`<div>job <code>${esc(y.name)}</code> running for ${dur(y.elapsed||0)}${y.last?` <span class="sub">· ${esc(y.last)}</span>`:''}</div>`).join('')}
  <div class="sub">${agentBusy(it)?'The agent is working on it in the chat on the left.':'The agent is not watching the job right now: ask in the chat on the left (“how is it going?”) and it checks it.'}</div></div>`}
