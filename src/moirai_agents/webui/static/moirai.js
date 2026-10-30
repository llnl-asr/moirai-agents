// The right panel, the stage gates, the Dashboard and self-refresh. Loaded after the chat code in index.html.
const PANEL_S=4000;
let PIPES=[],curPipe=null,dash={pid:null,traces:null,es:null,g:null,dirty:false,sel:null};
function act(path,body){return api(path,body)}
function showTab(t){const c=t==='chat';try{history.replaceState(null,'',c?location.pathname:'#dashboard')}catch(_){}$('#chatview').hidden=!c;$('#dashview').hidden=c;$('#tabchat').classList.toggle('on',c);$('#tabdash').classList.toggle('on',!c);if(!c)dashRender(true)}
$('#tabchat').onclick=()=>showTab('chat');$('#tabdash').onclick=()=>showTab('dash');

// ---- Steps + Jobs
async function pollPipes(){if(!me)return;const d=await api('api/pipelines'+(session?'?session='+session:''));if(!d||!d.pipelines)return;
 PIPES=d.pipelines;curPipe=PIPES[0]||null;
 $('#scount').textContent=curPipe?`(${curPipe.stages.filter(s=>s.status==='passed').length}/${curPipe.stages.length})`:'';
 $('#steps').innerHTML=PIPES.length?PIPES.slice(0,3).map(p=>`<div class="phead">${esc(p.name)} <span>${esc(p.kind)}</span></div>`+p.stages.map(s=>{
   const st=s.status==='needs_user'?'waiting for you':s.status;
   const line=s.failed&&s.failed.length?s.failed[0]:s.summary||'';
   return `<div class="step ${p.current===s.name?'cur':''}"><span class="sdot ${esc(s.status)}"></span><div><div class="nm">${esc(s.name)}</div><div class="sub">${esc(st)} · ${esc(s.owner)}${s.feedback_open?` · ${s.feedback_open} feedback open`:''}</div>${line?`<div class="sub">${esc(line)}</div>`:''}</div></div>`}).join('')).join(''):'<div class="lesson st">no pipeline in this chat</div>';
 const J=PIPES.flatMap(p=>(p.jobs||[]).map(j=>({...j,p:p.id})));const run=J.filter(j=>j.status==='running').length;
 $('#jcount').textContent=J.length?`(${run} running)`:'';
 $('#jobs').innerHTML=J.length?J.slice(0,12).map(j=>{const c=j.status==='running'?'run':j.rc===0?'ok':'bad';
   return `<details class="jb"><summary><span class="jdot ${c}"></span><span class="n">${esc(j.name)}</span><span class="s">${j.status==='running'?Math.round(j.elapsed)+' s':(j.timed_out?'timed out':'rc '+j.rc)}</span></summary><div class="body"><span class="k">stage</span><span class="v">${esc(j.stage)}</span><span class="k">limit</span><span class="v">${j.limit_s||'?'} s</span><span class="k">log</span><span class="v">${esc(j.log)}</span>${j.status==='running'?`<button class="btn danger" data-jp="${esc(j.p)}" data-jid="${esc(j.id)}">Stop</button>`:''}</div></details>`}).join(''):'<div class="job">none</div>';
 $('#jobs').querySelectorAll('[data-jid]').forEach(b=>b.onclick=async()=>{await act('api/job/stop',{pipeline:b.dataset.jp,job:b.dataset.jid});pollPipes()});
 const lv=PIPES.find(p=>p.dashboard);$('#tablive').hidden=!(lv&&lv.dashboard.live&&(lv.jobs||[]).some(j=>j.status==='running'));
 if(lv&&(lv.id!==dash.pid||lv.dashboard.traces!==dash.traces)){dashOpen(lv);dashRender(true)}}
const pollJobs=pollPipes;

// ---- Lessons
async function pollLessons(){if(!me)return;const d=await api('api/lessons');if(!d||!d.lessons)return;const L=d.lessons,np=L.filter(x=>x.status==='pending').length;
 $('#lcount').textContent=L.length?`(${np?np+' pending · ':''}${L.length})`:'';
 $('#lessons').innerHTML=L.length?L.slice(0,40).map(x=>`<details class="lesson"><summary><span class="ldot ${esc(x.status)}"></span><span class="t">${esc(x.text)}</span></summary><div class="body"><div class="tg">${esc(x.capability)} · ${esc(x.source)}</div><div>${esc(x.text)}</div>${x.evidence?`<div class="st">${esc(x.evidence)}</div>`:''}${x.status==='pending'?`<div class="acts"><button data-l="${x.id}" data-a="1">Apply</button><button data-l="${x.id}" data-a="">Reject</button></div>`:''}</div></details>`).join(''):'<div class="lesson st">no lessons yet</div>';
 $('#lessons').querySelectorAll('[data-l]').forEach(b=>b.onclick=async()=>{await act('api/lesson/decide',{id:b.dataset.l,approve:!!b.dataset.a});pollLessons()})}

// ---- Capabilities
async function pollCaps(){if(!me)return;const d=await api('api/capabilities');if(!d||!d.active)return;
 $('#ccount').textContent=`(${d.active.length}${d.pending.length?' · '+d.pending.length+' to approve':''})`;
 const P=d.pending.map(p=>`<div class="capp"><b>${esc(p.kind)}: ${esc(p.name)}</b><div class="why">${esc(p.why)}</div>${(p.review||[]).map(r=>`<div class="flag">⚠ ${esc(r)}</div>`).join('')}${p.diff?`<details><summary>diff</summary><pre>${esc(p.diff)}</pre></details>`:''}<div class="acts"><button class="btn primary" data-c="${p.id}" data-a="1">Approve</button><button class="btn" data-c="${p.id}" data-a="">Reject</button></div></div>`).join('');
 const A=d.active.map(c=>`<div class="cap ${c.enabled?'':'off'}"><div class="n">${esc(c.name)} <span class="s">v${c.version} · ${esc(c.layer)} · ${c.tools.length} tools${c.lessons?' · '+c.lessons+' lessons':''}</span></div>${c.error?`<div class="bad">${esc(c.error)}</div>`:''}</div>`).join('');
 $('#capabilities').innerHTML=P+A;
 if(d.pending.length){const s=document.querySelector('details[data-sec=capabilities]');if(s)s.open=true}
 $('#capabilities').querySelectorAll('[data-c]').forEach(b=>b.onclick=async()=>{b.disabled=true;const r=await act('api/capability/decide',{id:b.dataset.c,approve:!!b.dataset.a});if(r&&r.status==='error')alert(r.error);pollCaps()})}

// ---- gates: an ask_user that names a pipeline and a stage
const askCard0=Renderer.prototype.askCard;
Renderer.prototype.askCard=function(e){askCard0.call(this,e);let q={};try{q=JSON.parse(e.input)}catch(_){}
 if(!q.pipeline||!q.stage||this.answered)return;
 const cards=this.b.querySelectorAll('.ask'),c=cards[cards.length-1];if(!c)return;
 const ta=c.querySelector('textarea');ta.placeholder='What should change, or what should be remembered?';
 const g=document.createElement('div');g.className='gate';
 g.innerHTML=`<button class="btn primary" data-k="approve">Approve ${esc(q.stage)}</button><button class="btn" data-k="process">Change the process</button><button class="btn" data-k="remember">Remember this</button><div class="hint">Change the process becomes a new or updated capability, which you approve. Remember this becomes a lesson.</div>`;
 c.appendChild(g);const head=`[Gate: pipeline ${q.pipeline}, stage ${q.stage}]`;
 g.querySelectorAll('button').forEach(b=>b.onclick=()=>{const t=ta.value.trim(),k=b.dataset.k;if(k!=='approve'&&!t){ta.focus();return}
  const msg=k==='approve'?`${head} Approved.${t?' Comment: '+t:''} Record it with pipeline_decide, then pipeline_advance.`:k==='process'?`${head} Process feedback, pipeline_feedback kind=process: ${t}`:`${head} Remember, pipeline_feedback kind=remember: ${t}`;
  c.classList.add('done');sendText(msg)})};

// ---- Dashboard: the provenance graph of dftracer-utils' viewer (web/src/prov/ProvenanceView.tsx), simplified, fed by one stream from the web server:
// a snapshot, then deltas as the trace grows. Controls: pick entities, grouped by type; show or hide activities, entity types and I/O.
const PALETTE=['#4e9fd6','#e6a13c','#5fb87a','#d9675f','#a07bd1','#3fb5a8','#d4c04a','#e07fb3','#7f9cc2','#b8865a','#8fbf4d','#c26d9e'];
const MAX_ACTIVITIES=400,FILE_COLOR='#7f9cc2',IO_SHOWN=new Set(['read','write','delete']);   // open and stat are noise, as in the viewer's default
const view={activities:true,io:true,hidden:new Set(),picked:new Set()};
function dashOpen(p){if(dash.es)dash.es.close();if(dash.cy)dash.cy.destroy();$('#dashview').innerHTML='';
 dash={pid:p.id,traces:p.dashboard.traces,live:p.dashboard.live,es:null,g:null,dirty:true,cy:null,counts:null};view.picked.clear();view.hidden.clear();
 const es=dash.es=new EventSource(`api/dash/${encodeURIComponent(p.id)}/stream${TOK?'?token='+encodeURIComponent(TOK):''}`);
 es.addEventListener('snapshot',ev=>{const s=JSON.parse(ev.data);dash.g={types:{},entities:{},activities:s.activities,edges:s.edges,relations:s.relations,accesses:s.accesses||[]};
  s.types.forEach(t=>dash.g.types[t.type]=t);s.entities.forEach(e=>dash.g.entities[e.id]=e);dash.counts=s.counts;dash.live=s.live;dash.dirty=true;dashRender(false)});
 es.onmessage=ev=>{const d=JSON.parse(ev.data);if(!dash.g)return;dash.counts=d.counts;for(const i of d.items){
   if(i.kind==='type')dash.g.types[i.type]=i;else if(i.kind==='entity')dash.g.entities[i.id]=i;else if(i.kind==='relation')dash.g.relations.push(i);else if(i.kind==='access')dash.g.accesses.push(i);
   else if(i.kind==='activity'){dash.g.activities.push(i);for(const[k,ids]of Object.entries(i.relations||{}))for(const e of [].concat(ids))dash.g.edges.push({activity:i.id,kind:k,entity:e})}}
  if(d.items.length)dash.dirty=true;else dashTiles()}}
const leaf=s=>{s=String(s||'').replace(/^file:\/\//,'').replace(/\/+$/,'');return s.split('/').pop()||s};
function entName(e){return leaf(e.uri)||String(e.id).slice(0,10)+'…'}
function typeColors(){const names=Object.keys(dash.g.types).concat(Object.values(dash.g.entities).map(e=>e.type)).filter((x,i,a)=>x&&a.indexOf(x)===i).sort();return Object.fromEntries(names.map((n,i)=>[n,PALETTE[i%PALETTE.length]]))}
function dashTiles(){const n=dash.counts||{},el=$('#dtiles');if(!el)return;
 el.innerHTML=[['Activities',n.activities],['Entities',n.entities],['Files',n.files],['Records / s',n.rate]].map(([k,v])=>`<div class="tile"><b>${(v??0).toLocaleString()}</b><span>${k}</span></div>`).join('')}
// The graph to draw: picked entities plus everything upstream and downstream of them; contracted when activities are hidden.
function dashElements(){const g=dash.g,acts=g.activities.slice(-MAX_ACTIVITIES),ids=new Set(acts.map(a=>a.id)),edges=g.edges.filter(e=>ids.has(e.activity));
 const inp={},out={};for(const e of edges){const m=e.kind==='used'?inp:e.kind==='generated'||e.kind==='updated'?out:null;if(m)(m[e.activity]=m[e.activity]||[]).push(e.entity)}
 let keepA=ids,keepE=new Set(edges.map(e=>e.entity));
 if(view.picked.size){keepA=new Set();keepE=new Set(view.picked);
  const walk=(from,to)=>{const seen=new Set(view.picked),todo=[...seen];while(todo.length){const x=todo.pop();for(const a of acts)if((from[a.id]||[]).includes(x)){keepA.add(a.id);for(const y of to[a.id]||[])if(!seen.has(y)){seen.add(y);keepE.add(y);todo.push(y)}}}};
  walk(out,inp);walk(inp,out)}
 const shown=id=>keepE.has(id)&&!view.hidden.has((g.entities[id]||{}).type),els=[],colors=typeColors();
 for(const id of keepE)if(shown(id)){const e=g.entities[id]||{id,type:'?'};els.push({data:{id:'E:'+id,label:entName(e),color:colors[e.type]||'#888',kind:'entity',eid:id}})}
 const seen=new Set(),edge=(s,t,kind)=>{const k=s+'>'+t;if(!seen.has(k)){seen.add(k);els.push({data:{id:k,source:s,target:t,kind}})}};
 if(view.activities){for(const a of acts)if(keepA.has(a.id)){els.push({data:{id:'A:'+a.id,label:a.name,kind:'activity'}});
   for(const x of inp[a.id]||[])if(shown(x))edge('E:'+x,'A:'+a.id,'lineage');for(const x of out[a.id]||[])if(shown(x))edge('A:'+a.id,'E:'+x,'lineage')}}
 else{for(const a of acts)if(keepA.has(a.id))for(const x of inp[a.id]||[])for(const y of out[a.id]||[])if(shown(x)&&shown(y)&&x!==y)edge('E:'+x,'E:'+y,'lineage')}
 for(const r of g.relations)if(/derived_from|revision_of/.test(r.relation)&&shown(r.object)&&shown(r.subject)&&(!view.activities))edge('E:'+r.object,'E:'+r.subject,'lineage');
 if(view.io){const files=new Set(),stored=new Set(),link=p=>{if(!files.has(p)){files.add(p);els.push({data:{id:'F:'+p,label:leaf(p),kind:'file',path:p}})}};
  if(view.activities)for(const x of g.accesses)if(keepA.has(x.aid)&&IO_SHOWN.has(x.op)){link(x.path);const w=x.op==='write'||x.op==='delete';edge(w?'A:'+x.aid:'F:'+x.path,w?'F:'+x.path:'A:'+x.aid,'io')}
  for(const id of keepE)if(shown(id)){const e=g.entities[id]||{},p=String(e.uri||'').replace(/^file:\/\//,'');if(p&&(files.has(p)||g.accesses.some(x=>x.path===p&&IO_SHOWN.has(x.op)))){link(p);edge('E:'+id,'F:'+p,'stored');stored.add(p)}}
  for(const x of els)if(x.data.kind==='file'&&stored.has(x.data.path))x.data.label=''}
 return els}
function cyStyle(){const c=getComputedStyle(document.documentElement),ink=c.getPropertyValue('--ink').trim(),line=c.getPropertyValue('--muted').trim(),panel=c.getPropertyValue('--panel').trim();
 const label={label:'data(label)','font-size':10,color:ink,'text-wrap':'ellipsis','text-max-width':110,'text-valign':'bottom','text-margin-y':3};
 return [{selector:'node[kind="entity"]',style:{...label,shape:'ellipse',width:18,height:18,'background-color':'data(color)'}},
  {selector:'node[kind="activity"]',style:{...label,shape:'round-rectangle',width:30,height:16,'background-color':panel,'border-width':1.5,'border-color':ink}},
  {selector:'node[kind="file"]',style:{...label,shape:'rectangle',width:14,height:18,'background-color':FILE_COLOR,opacity:.85}},
  {selector:'edge',style:{width:1.2,'line-color':line,'target-arrow-color':line,'target-arrow-shape':'triangle','arrow-scale':.8,'curve-style':'bezier',opacity:.7}},
  {selector:'edge[kind="io"]',style:{'line-style':'dashed','line-color':FILE_COLOR,'target-arrow-color':FILE_COLOR}},
  {selector:'edge[kind="stored"]',style:{'line-style':'dotted','target-arrow-shape':'none'}},
  {selector:'node:selected',style:{'border-width':3,'border-color':c.getPropertyValue('--accent').trim()}}]}
function dashGraph(){const host=$('#dcy');if(!host||!dash.g)return;
 if(!dash.cy){dash.cy=cytoscape({container:host,style:cyStyle(),minZoom:.05,maxZoom:1.4,wheelSensitivity:.3});
  dash.cy.on('tap','node',ev=>{const d=ev.target.data();$('#dinfo').textContent=d.kind==='entity'?`${(dash.g.entities[d.eid]||{}).type}: ${(dash.g.entities[d.eid]||{}).uri||d.eid}`:d.kind==='file'?d.path:`activity ${d.label}`})}
 const cy=dash.cy,els=dashElements(),want=new Set(els.map(e=>e.data.id));let changed=false;
 cy.batch(()=>{cy.elements().forEach(x=>{if(!want.has(x.id())){x.remove();changed=true}});
  const add=els.filter(e=>!cy.getElementById(e.data.id).nonempty());if(add.length){cy.add(add);changed=true}});
 if(changed)cy.layout({name:'dagre',rankDir:'LR',nodeSep:14,rankSep:60,animate:false,fit:true,padding:16}).run();
 $('#dnote').textContent=dash.g.activities.length>MAX_ACTIVITIES?`showing the latest ${MAX_ACTIVITIES} of ${dash.g.activities.length} activities`:''}
function dashControls(){const box=$('#dctl');if(!box||!dash.g)return;const g=dash.g,colors=typeColors(),by={};
 for(const e of Object.values(g.entities))(by[e.type]=by[e.type]||[]).push(e);
 const types=Object.keys(by).sort();const sig=types.map(t=>t+by[t].length).join()+[...view.picked].length;if(dash.sig===sig)return;dash.sig=sig;const open=$('#dpick')&&$('#dpick').open;
 box.innerHTML=`<details id="dpick" ${open?'open':''}><summary class="btn">Entities: ${view.picked.size?view.picked.size+' selected':'all'}</summary><div class="dmenu">
   ${view.picked.size?'<a id="dclear">clear selection</a>':''}${types.map(t=>`<details class="dgroup"><summary><span class="swatch" style="background:${colors[t]}"></span>${esc(t)} <span class="cnt">${by[t].length}</span></summary>
    <label class="dall"><input type="checkbox" data-all="${esc(t)}" ${by[t].every(e=>view.picked.has(e.id))?'checked':''}> all ${esc(t)}</label>
    ${by[t].slice(0,500).map(e=>`<label><input type="checkbox" data-e="${esc(e.id)}" ${view.picked.has(e.id)?'checked':''}> ${esc(entName(e))}</label>`).join('')}</details>`).join('')}</div></details>
  <span class="dshow">Show</span><label class="chip3"><input type="checkbox" data-v="activities" ${view.activities?'checked':''}> Activities</label><label class="chip3"><input type="checkbox" data-v="io" ${view.io?'checked':''}> I/O</label>
  ${types.map(t=>`<label class="chip3"><input type="checkbox" data-t="${esc(t)}" ${view.hidden.has(t)?'':'checked'}><span class="swatch" style="background:${colors[t]}"></span>${esc(t)}</label>`).join('')}`;
 const redo=()=>{dash.sig=null;dash.dirty=true;dashRender(false)};
 box.querySelectorAll('[data-v]').forEach(i=>i.onchange=()=>{view[i.dataset.v]=i.checked;redo()});
 box.querySelectorAll('[data-t]').forEach(i=>i.onchange=()=>{i.checked?view.hidden.delete(i.dataset.t):view.hidden.add(i.dataset.t);redo()});
 box.querySelectorAll('[data-e]').forEach(i=>i.onchange=()=>{i.checked?view.picked.add(i.dataset.e):view.picked.delete(i.dataset.e);redo()});
 box.querySelectorAll('[data-all]').forEach(i=>i.onchange=()=>{for(const e of by[i.dataset.all])i.checked?view.picked.add(e.id):view.picked.delete(e.id);redo()});
 const c=$('#dclear');if(c)c.onclick=()=>{view.picked.clear();redo()}}
function dashRender(force){const v=$('#dashview');if(v.hidden||!(force||dash.dirty))return;dash.dirty=false;
 if(!dash.pid){v.innerHTML='<div class="d-empty"><h2>Nothing to show yet</h2>A moirai run opens this tab by itself. For existing traces, start an Analysis pipeline.</div>';return}
 if(!$('#dcy')){v.innerHTML=`<div class="d-head"><h2>Provenance</h2><span class="d-state ${dash.live?'live':''}">${dash.live?'live':'finished trace'}</span><span class="tr">${esc(dash.traces)}</span></div><div class="tiles" id="dtiles"></div>
  <div class="dctl" id="dctl"></div><div class="card dgraph"><div id="dcy"></div></div><div id="dinfo" class="dinfo"></div><div id="dnote" class="dinfo"></div>`;if(dash.cy){dash.cy.destroy();dash.cy=null}}
 dashTiles();dashControls();dashGraph();if(dash.cy)dash.cy.resize()}
setInterval(()=>dashRender(false),1000);

// ---- theme + status
function themeNow(){return document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}
function themeIcon(){$('#themebtn').textContent=themeNow()==='dark'?'☀':'☾'}
$('#themebtn').onclick=()=>{const t=themeNow()==='dark'?'light':'dark';document.documentElement.dataset.theme=t;try{localStorage.setItem('moirai_theme',t)}catch(_){}themeIcon();if(dash.cy)dash.cy.style(cyStyle())};themeIcon();
$('#statusbtn').onclick=e=>{e.stopPropagation();const w=$('#statuswrap'),o=w.classList.toggle('open');$('#statusbtn').setAttribute('aria-expanded',o)};
document.addEventListener('click',e=>{if(!$('#statuswrap').contains(e.target))$('#statuswrap').classList.remove('open')});
new MutationObserver(()=>$('#statusbtn').classList.toggle('bad',!!document.querySelector('#pills .pill.bad'))).observe($('#pills'),{childList:true,subtree:true});

// ---- self-refresh + time limits
let VER=null,RELOADING=false;
function fmtLeft(s){return s>=3600?Math.round(s/3600)+' h':Math.max(1,Math.round(s/60))+' min'}
async function pollVersion(){let v;try{v=await(await fetch('api/version')).json()}catch(_){return}
 const b=$('#banner');let h='',bad=false;
 if(v.error){h=`Update not applied, the running code stays: ${esc(v.error)}`;bad=true}
 else if(v.pending)h='An update is waiting for running chats to finish.';
 else if(v.stops&&v.stops.warn)h=`This server stops in ${fmtLeft(v.stops.seconds)} (${esc(v.stops.why)}). ${v.stops.why==='idle'?'<button id="keep">Keep it running</button>':''}`;
 b.hidden=!h;b.className=bad?'bad':'';b.innerHTML=h;const k=$('#keep');if(k)k.onclick=async()=>{await act('api/keepalive',{});pollVersion()};
 if(VER===null){VER=v.version;return}
 if(v.version>VER){const typing=$('#input').value.trim();
  if(!running&&!typing&&!document.querySelector('.ask:not(.done) textarea:focus')&&!RELOADING){RELOADING=true;location.reload()}}}
function startPanels(){if(location.hash==='#dashboard')showTab('dash');pollPipes();pollLessons();pollCaps();pollVersion();
 (function(){const K='moirai_secs';let st={};try{st=JSON.parse(localStorage.getItem(K)||'{}')}catch(_){}
  document.querySelectorAll('details.sec').forEach(d=>{if(d.dataset.sec in st)d.open=!!st[d.dataset.sec];d.addEventListener('toggle',()=>{st[d.dataset.sec]=d.open;try{localStorage.setItem(K,JSON.stringify(st))}catch(_){}})})})()}
setInterval(()=>{if(me&&document.visibilityState==='visible')pollPipes()},PANEL_S);
setInterval(()=>{if(me&&document.visibilityState==='visible'){pollLessons();pollCaps()}},PANEL_S*3);
setInterval(pollVersion,3000);
boot();
