// The right panel, the stage gates, the Dashboard and self-refresh. Loaded after the chat code in index.html.
const PANEL_S=4000;
let PIPES=[],curPipe=null,dash={pid:null,run:'',seen:'',view:'provenance'},shown=null;
function act(path,body){return api(path,body)}
function showTab(t){const c=t==='chat',d=t==='dash';try{history.replaceState(null,'',d?'#dashboard':location.pathname)}catch(_){}$('#chatview').hidden=!c;$('#dashview').hidden=!d;$('#stateview').hidden=c||d;$('#tabchat').classList.toggle('on',c);$('#tabdash').classList.toggle('on',d);if(d)dashRender()}
$('#tabchat').onclick=()=>showTab('chat');$('#tabdash').onclick=()=>showTab('dash');

// ---- Steps + Jobs
async function pollPipes(){if(!me)return;const d=await api('api/pipelines'+(session?'?session='+session:''));if(!d||!d.pipelines)return;
 PIPES=d.pipelines;curPipe=PIPES[0]||null;
 $('#scount').textContent=curPipe?`(${curPipe.stages.filter(s=>s.status==='passed').length}/${curPipe.stages.length})`:'';
 $('#steps').innerHTML=PIPES.length?PIPES.slice(0,3).map(p=>`${p.goal?`<div class="pgoal">${esc(p.goal)}</div>`:''}<div class="phead">${esc(p.name)} <span>${esc(p.kind)}${p.session?' · session '+esc(p.session.run_id):''}</span></div>`+p.stages.map(s=>{
   const dl=v=>`<a href="#dashboard" data-run="${esc(s.run)}" data-view="${v}">${v}</a>`;
   return `<div class="step ${p.current===s.name?'cur':''}"><span class="sdot ${esc(s.status)}" title="${esc(STEP_WORD[s.status]||s.status)}"></span><a href="#" class="nm" data-pid="${esc(p.id)}" data-stage="${esc(s.name)}" title="what happened in this step">${esc(s.name)}</a>${s.run?`<span class="l">${dl('timeline')} · ${dl('provenance')}</span>`:''}${s.status==='needs_user'?'<span class="wait">waiting for you</span>':''}</div>`}).join('')).join(''):'<div class="lesson st">no pipeline in this chat</div>';
 $('#steps').querySelectorAll('[data-stage]').forEach(a=>a.onclick=e=>{e.preventDefault();shown={pid:a.dataset.pid,stage:a.dataset.stage};showTab('state');stateRender()});
 $('#steps').querySelectorAll('[data-run]').forEach(a=>a.onclick=e=>{e.preventDefault();dash.run=a.dataset.run;dash.view=a.dataset.view;showTab('dash')});
 if(!$('#stateview').hidden)stateRender();
 const J=PIPES.flatMap(p=>(p.jobs||[]).map(j=>({...j,p:p.id})));const run=J.filter(j=>j.status==='running').length;
 $('#jcount').textContent=J.length?`(${run} running)`:'';
 $('#jobs').innerHTML=J.length?J.slice(0,12).map(j=>{const c=j.status==='running'?'run':j.rc===0?'ok':'bad';
   return `<details class="jb"><summary><span class="jdot ${c}"></span><span class="n">${esc(j.name)}</span><span class="s">${j.status==='running'?Math.round(j.elapsed)+' s':(j.timed_out?'timed out':'rc '+j.rc)}</span></summary><div class="body"><span class="k">stage</span><span class="v">${esc(j.stage)}</span><span class="k">limit</span><span class="v">${j.limit_s||'?'} s</span><span class="k">log</span><span class="v">${esc(j.log)}</span>${j.status==='running'?`<button class="btn danger" data-jp="${esc(j.p)}" data-jid="${esc(j.id)}">Stop</button>`:''}</div></details>`}).join(''):'<div class="job">none</div>';
 $('#jobs').querySelectorAll('[data-jid]').forEach(b=>b.onclick=async()=>{await act('api/job/stop',{pipeline:b.dataset.jp,job:b.dataset.jid});pollPipes()});
 const lv=PIPES.find(p=>p.dashboard);$('#tablive').hidden=!(lv&&lv.dashboard.live&&(lv.jobs||[]).some(j=>j.status==='running'));
 if(lv&&lv.dash_run!==dash.seen){dash.seen=lv.dash_run;dashOpen(lv)}}   // a new run takes over; a path you opened stays until then
const pollJobs=pollPipes;

// ---- A step's state: what happened in it, opened from its name
const STEP_WORD={needs_user:'waiting for you'};
const when=t=>t?new Date(t*1000).toLocaleString():'';
async function stateRender(){const q=shown,s=await api(`api/pipeline/${encodeURIComponent(q.pid)}/${encodeURIComponent(q.stage)}`),v=$('#stateview');if(q!==shown)return;
 if(!s||s.error){v.textContent=s?s.error:'';return}
 const part=(h,b)=>b?`<h4>${h}</h4>${b}`:'',c=s.checks,chk=(x,cl)=>`<div class="chk ${cl}"><b>${esc(x.name)}</b> ${esc(x.detail)}${x.fix?`<div class="fix">${esc(x.fix)}</div>`:''}</div>`;
 const html=`<h2>${esc(s.name)}</h2><div class="sub">${esc(STEP_WORD[s.status]||s.status)} · ${esc(s.owner)}${s.started?' · started '+when(s.started):''}${c.status?` · checks ${esc(c.status)} (${c.passed}/${c.total})`:''}</div>${s.summary?`<p>${esc(s.summary)}</p>`:''}`
  +part('Checks to look at',[...(c.failed||[]).map(x=>chk(x,'bad')),...(c.unknown||[]).map(x=>chk(x,'unk'))].join(''))
  +part('Your answer',s.gate?`<div>${esc(s.gate.answer||'')} <span class="sub">${s.gate.approved?'approved':'not approved'} · ${when(s.gate.ts)}</span></div>`:'')
  +part('What happened',s.history.slice().reverse().map(x=>`<div class="ev"><span class="sub">${when(x.ts)}</span>${esc(x.note)}</div>`).join(''))
  +part('Feedback',s.feedback.map(x=>`<div class="ev"><span class="sub">${esc(x.kind)}${x.resolution?' · '+esc(x.resolution):' · open'}</span>${esc(x.text)}</div>`).join(''))
  +part('Command',s.command?`<code>${esc(s.command)}</code>`:'')
  +part('Plan'+(s.plan_stale?' <b class="stale">out of date</b>':''),s.plan?`<div>${esc(s.plan)}</div>`:'');
 if(v.dataset.h!==html){v.dataset.h=html;v.innerHTML=html}}

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

// ---- Dashboard: dfdashboard, behind this server's port. It opens a pipeline's run by itself, or any traces given by path.
function dashOpen(p){dash.pid=p.id;dash.run=p.dash_run;dashRender()}
function dashRender(){const v=$('#dashview');if(v.hidden)return;
 if(!$('#dpath')){v.innerHTML=`<form id="dpath" class="dpath"><input id="dpathi" placeholder="Path to provenance traces: a directory, a .pfw.gz file or a glob" aria-label="Path to provenance traces" spellcheck="false"><button class="btn primary">Open</button><span id="dpathe" class="err"></span></form><iframe class="dframe" id="dframe" title="dfdashboard"></iframe>`;
  $('#dpath').onsubmit=async e=>{e.preventDefault();const path=$('#dpathi').value.trim(),m=$('#dpathe');m.textContent='';if(!path)return;
   const r=await api('api/dashboard/open',{path});if(r.error){m.textContent=r.error;return}dash.run=r.run;dash.view='provenance';dashRender()}}
 const u=dash.run?`dash/?view=${dash.view}&run=${encodeURIComponent(dash.run)}`:'dash/',f=$('#dframe');if(f.dataset.src!==u){f.dataset.src=u;f.src=u}}

// ---- theme + status
function themeNow(){return document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}
function themeIcon(){$('#themebtn').textContent=themeNow()==='dark'?'☀':'☾'}
$('#themebtn').onclick=()=>{const t=themeNow()==='dark'?'light':'dark';document.documentElement.dataset.theme=t;try{localStorage.setItem('moirai_theme',t)}catch(_){}themeIcon()};themeIcon();
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
