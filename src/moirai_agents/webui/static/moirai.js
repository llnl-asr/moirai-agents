// The right panel, the stage gates, the Dashboard and self-refresh. Loaded after the chat code in index.html.
const PANEL_S=4000;
let PIPES=[],curPipe=null,dash={pid:null,run:'',seen:'',view:'provenance'},shown=null;
function act(path,body){return api(path,body)}
function showTab(t){const c=t==='chat',d=t==='dash';try{history.replaceState(null,'',d?'#dashboard':location.pathname)}catch(_){}$('#chatview').hidden=!c;$('#dashview').hidden=!d;$('#stateview').hidden=c||d;$('#tabchat').classList.toggle('on',c);$('#tabdash').classList.toggle('on',d);if(d)dashRender()}
$('#tabchat').onclick=()=>showTab('chat');$('#tabdash').onclick=()=>showTab('dash');

// ---- Steps + Jobs
const DASH_VIEWS=[['provenance','behaviors'],['prov-runtime','runtime'],['prov-temporal','temporal'],['prov-graph','graph']];   // dfdashboard's Provenance views
async function pollPipes(){if(!me)return;const d=await api('api/pipelines'+(session?'?session='+session:''));if(!d||!d.pipelines)return;
 PIPES=d.pipelines;curPipe=PIPES[0]||null;
 $('#scount').textContent=curPipe?`(${curPipe.stages.filter(s=>s.status==='passed').length}/${curPipe.stages.length})`:'';
 $('#steps').innerHTML=PIPES.length?PIPES.slice(0,3).map(p=>`${p.goal?`<div class="pgoal">${esc(p.goal)}</div>`:''}<div class="phead">${esc(p.name)} <span>${esc(p.kind)}${p.session?' · session '+esc(p.session.run_id):''}</span></div>`+p.stages.map(s=>{
   const dl=([v,label])=>`<a href="#dashboard" data-run="${esc(s.run)}" data-view="${v}" data-label="${esc(s.name+' of '+p.name)}">${label}</a>`;
   return `<div class="step ${p.current===s.name?'cur':''}"><span class="sdot ${esc(s.status)}" title="${esc(STEP_WORD[s.status]||s.status)}"></span><a href="#" class="nm" data-pid="${esc(p.id)}" data-stage="${esc(s.name)}" title="what happened in this step">${esc(s.name)}</a>${s.diagrams?`<a href="#" class="dgl" data-pid="${esc(p.id)}" data-stage="${esc(s.name)}" title="${s.diagrams} diagram${s.diagrams>1?'s':''} of this step, kept as an output of the pipeline">diagram</a>`:''}${s.run?`<span class="l">${DASH_VIEWS.map(dl).join(' · ')}</span>`:''}${s.status==='needs_user'?'<span class="wait">waiting for you</span>':''}</div>`}).join('')).join(''):'<div class="lesson st">no pipeline in this chat</div>';
 $('#steps').querySelectorAll('[data-stage]').forEach(a=>a.onclick=e=>{e.preventDefault();shown={pid:a.dataset.pid,stage:a.dataset.stage};showTab('state');stateRender()});
 $('#steps').querySelectorAll('[data-run]').forEach(a=>a.onclick=e=>{e.preventDefault();dash.run=a.dataset.run;dash.view=a.dataset.view;dash.label=a.dataset.label;showTab('dash')});
 if(!$('#stateview').hidden)stateRender();
 pollSubmitted();
 const lv=PIPES.find(p=>p.dashboard);$('#tablive').hidden=!(lv&&lv.dashboard.live&&(lv.jobs||[]).some(j=>j.status==='running'));
 if(lv&&lv.dash_run!==dash.seen){dash.seen=lv.dash_run;dashOpen(lv)}}   // a new run takes over; a path you opened stays until then
const pollJobs=pollPipes;

// ---- Jobs: this server's processes, and what is in the scheduler
const GROUP_OPEN={};   // every group starts closed; what is opened stays open across polls
const PROC_GROUPS=[['p-run','run','running','running now on this node'],['p-ok','ok','passed','finished with exit code 0'],['p-bad','bad','failed','failed, timed out or lost']];
const procGroup=j=>j.status==='running'?'p-run':j.rc===0?'p-ok':'p-bad';
const JOB_GROUPS=[['sched','scheduled','queued, not running yet'],['run','running','running now'],['ok','passed','finished and completed'],['bad','failed','failed, cancelled or timed out']];
const group=(key,head,tip,body,cls='')=>`<details class="jg ${cls}" data-g="${key}" ${GROUP_OPEN[key]?'open':''}><summary title="${esc(tip)}">${head}</summary>${body}</details>`;
const noRows='<div class="job">none</div>';
const procRow=j=>{const c=j.status==='running'?'run':j.rc===0?'ok':'bad';
 return `<details class="jb" data-id="${esc(j.id)}" data-kind="proc" data-pipe="${esc(j.p)}"><summary><span class="jdot ${c}"></span><span class="n">${esc(j.name)}</span><span class="s">${j.status==='running'?Math.round(j.elapsed)+' s':(j.timed_out?'timed out':'rc '+j.rc)}</span></summary><div class="body"><span class="k">stage</span><span class="v">${esc(j.stage)}</span><span class="k">limit</span><span class="v">${j.limit_s||'?'} s</span><span class="k">log</span><span class="v">${esc(j.log)}</span>${j.status==='running'?`<button class="btn danger" data-jp="${esc(j.p)}" data-jid="${esc(j.id)}">Stop</button>`:''}</div><div class="jd"></div></details>`};
const subRow=j=>`<details class="jb" data-id="${esc(j.id)}" data-kind="flux" data-live="${j.group==='sched'||j.group==='run'?1:''}"><summary><span class="jdot ${j.group}"></span><span class="n">${esc(j.name)}</span><span class="s">${j.runtime?Math.round(j.runtime)+' s':esc(j.state.toLowerCase())}</span></summary><div class="body"><span class="k">id</span><span class="v">${esc(j.id)}</span><span class="k">state</span><span class="v">${esc(j.result||j.state)}</span><span class="k">queue</span><span class="v">${esc(j.queue)}</span><span class="k">nodes</span><span class="v">${esc(j.nodes)}</span><span class="k">submitted</span><span class="v">${esc(when(j.submitted))}</span></div><div class="jd"></div></details>`;
const JOB_DETAIL={};
const jobPane=x=>`${x.command?`<div class="jdh">What ran</div><pre>${esc(x.command)}</pre>`:''}${x.cwd?`<div class="jdh">Where</div><div class="jdv">${esc(x.cwd)}</div>`:''}<div class="jdh">Output</div><pre>${esc(x.output||'no output yet')}</pre>`;
async function jobOpen(e){const id=e.dataset.id,pane=e.querySelector('.jd');if(JOB_DETAIL[id])pane.innerHTML=jobPane(JOB_DETAIL[id]);else pane.textContent='loading…';
 const live=e.dataset.kind==='flux'?e.dataset.live:!JOB_DETAIL[id]||JOB_DETAIL[id].running;if(JOB_DETAIL[id]&&!live)return;
 const x=await api('api/job/detail?'+(e.dataset.kind==='flux'?'flux='+encodeURIComponent(id):`pipeline=${encodeURIComponent(e.dataset.pipe)}&job=${encodeURIComponent(id)}`));
 if(!x||x.error||x.status==='error'){pane.textContent=(x&&x.error)||'could not read this job';return}
 JOB_DETAIL[id]=x;const now=$('#jobs').querySelector(`details.jb[data-id="${CSS.escape(id)}"] .jd`);if(now){const pre=now.querySelector('pre:last-of-type'),end=!pre||pre.scrollHeight-pre.scrollTop-pre.clientHeight<30;now.innerHTML=jobPane(x);const p2=now.querySelector('pre:last-of-type');if(p2&&end)p2.scrollTop=p2.scrollHeight}}
async function pollSubmitted(){const box=$('#jobs');if(box.querySelector('details.jb[open] .body:focus-within'))return;
 const P=PIPES.flatMap(p=>(p.jobs||[]).map(j=>({...j,p:p.id}))).sort((x,y)=>(y.ended||y.started)-(x.ended||x.started));
 const since=PIPES.length?Math.min(...PIPES.map(p=>p.created||0)):null;
 const d=since===null?null:await api('api/submitted?since='+since),S=(d&&d.jobs)||[];
 const n=k=>S.filter(j=>j.group===k).length,running=P.filter(j=>j.status==='running').length;
 $('#jcount').textContent=P.length||S.length?`(${running} process${running===1?'':'es'} · ${n('sched')} scheduled · ${n('run')} running)`:'';
 const open=new Set([...box.querySelectorAll('details.jb[open]')].map(e=>e.dataset.id));
 const sub=JOB_GROUPS.map(([k,name,tip])=>{const js=S.filter(j=>j.group===k);return group(k,`<span class="jdot ${k}"></span><b>${name}</b> <span class="cnt">${js.length}</span>`,tip,js.length?js.map(subRow).join(''):noRows,'nest')}).join('');
  const procs=PROC_GROUPS.map(([k,dot,name,tip])=>{const js=P.filter(j=>procGroup(j)===k);return group(k,`<span class="jdot ${dot}"></span><b>${name}</b> <span class="cnt">${js.length}</span>`,tip,js.length?js.map(procRow).join(''):noRows,'nest')}).join('');
 box.innerHTML=group('proc',`<b>Processes</b> <span class="cnt">${P.length}</span>`,'Work this server started on this node',procs)+group('sub',`<b>Submitted jobs</b> <span class="cnt">${S.length}</span>`,'Your jobs in the scheduler since this pipeline began',sub);
 box.querySelectorAll('details.jb').forEach(e=>{if(open.has(e.dataset.id)){e.open=true;jobOpen(e)}e.addEventListener('toggle',()=>{if(e.open)jobOpen(e)})});
 box.querySelectorAll('details.jg').forEach(e=>e.addEventListener('toggle',ev=>{if(ev.target===e)GROUP_OPEN[e.dataset.g]=e.open}));
 box.querySelectorAll('[data-jid]').forEach(b=>b.onclick=async()=>{await act('api/job/stop',{pipeline:b.dataset.jp,job:b.dataset.jid});pollPipes()})}

// ---- A step's state: what happened in it, opened from its name
const STEP_WORD={needs_user:'waiting for you'};
const when=t=>t?new Date(t*1000).toLocaleString():'';
async function stateRender(){const q=shown,s=await api(`api/pipeline/${encodeURIComponent(q.pid)}/${encodeURIComponent(q.stage)}`),v=$('#stateview');if(q!==shown)return;
 if(!s||s.error){v.textContent=s?s.error:'';return}
 const part=(h,b)=>b?`<h4>${h}</h4>${b}`:'',c=s.checks,chk=(x,cl)=>`<div class="chk ${cl}"><b>${esc(x.name)}</b> ${esc(x.detail)}${x.fix?`<div class="fix">${esc(x.fix)}</div>`:''}</div>`;
 const html=`<h2>${esc(s.name)}</h2><div class="sub">${esc(STEP_WORD[s.status]||s.status)} · ${esc(s.owner)}${s.started?' · started '+when(s.started):''}${c.status?` · checks ${esc(c.status)} (${c.passed}/${c.total})`:''}</div>${s.summary?`<p>${esc(s.summary)}</p>`:''}`
  +part('Diagram'+((s.diagrams||[]).length>1?'s':''),(s.diagrams||[]).map(d=>dgHtml(s.pipeline,d)).join(''))
  +part('Checks to look at',[...(c.failed||[]).map(x=>chk(x,'bad')),...(c.unknown||[]).map(x=>chk(x,'unk'))].join(''))
  +part('Your answer',s.gate?`<div>${esc(s.gate.answer||'')} <span class="sub">${s.gate.approved?'approved':'not approved'} · ${when(s.gate.ts)}</span></div>`:'')
  +part('What happened',s.history.slice().reverse().map(x=>`<div class="ev"><span class="sub">${when(x.ts)}</span>${esc(x.note)}</div>`).join(''))
  +part('Feedback',s.feedback.map(x=>`<div class="ev"><span class="sub">${esc(x.kind)}${x.resolution?' · '+esc(x.resolution):' · open'}</span>${esc(x.text)}</div>`).join(''))
  +part('Command',s.command?`<code>${esc(s.command)}</code>`:'')
  +part('Plan'+(s.plan_stale?' <b class="stale">out of date</b>':''),s.plan?`<div>${esc(s.plan)}</div>`:'');
 if(v.dataset.h!==html){v.dataset.h=html;v.innerHTML=html}}

// ---- Lessons
let LESSONS=[],CAPS={};
const ledit=(id,body,acts)=>`<div class="ledit" hidden data-for="${esc(id)}">${body}<div class="acts">${acts}<button data-do="agent" title="Send your note to the agent in this chat and let it rework this">Ask the agent</button><button data-do="cancel">Cancel</button></div><div class="msg"></div></div>`;
const revs=x=>(x.revisions||[]).map(r=>`<div class="rev">✎ ${esc(r.by)}${r.note?': '+esc(r.note):' edited it'}</div>`).join('');
const editing=sel=>!!document.querySelector(sel+' .ledit:not([hidden])');
const toggleEdit=(root,id)=>{const e=root.querySelector(`.ledit[data-for="${id}"]`);e.hidden=!e.hidden;if(!e.hidden)e.querySelector('textarea').focus()};
async function pollLessons(force){if(!me||(!force&&editing('#lessons')))return;const d=await api('api/lessons');if(!d||!d.lessons)return;const L=LESSONS=d.lessons,np=L.filter(x=>x.status==='pending').length;
 $('#lcount').textContent=L.length?`(${np?np+' pending · ':''}${L.length})`:'';
 const open=new Set([...document.querySelectorAll('#lessons details[open]')].map(e=>e.dataset.id));
 $('#lessons').innerHTML=L.length?L.slice(0,40).map(x=>{const p=x.status==='pending';
  const acts=p?'<button data-do="save">Save</button><button class="primary" data-do="approve">Save &amp; accept</button>':x.status==='applied'?'<button data-do="save" title="It waits for you to accept it; this lesson stays until then">Propose correction</button><button class="primary" data-do="approve" title="Replace it now">Correct now</button>':'<button class="primary" data-do="save">Reopen with edit</button>';
  return `<details class="lesson" data-id="${esc(x.id)}" ${open.has(x.id)?'open':''}><summary><span class="ldot ${esc(x.status)}" title="${esc(x.status)}"></span><span class="t">${esc(x.text)}</span>${p?`<button class="x" data-l="${esc(x.id)}" data-a="1" title="Accept: write into ${esc(x.capability)}">✓</button><button class="x" data-l="${esc(x.id)}" data-a="" title="Reject">✗</button>`:''}</summary><div class="body"><div class="tg">${esc(x.capability)} · ${esc(x.source)}</div><div>${esc(x.text)}</div>${x.evidence?`<div class="st">evidence: ${esc(x.evidence)}</div>`:''}<div class="st">${esc(x.status)}${x.decided_by?' · decided by '+esc(x.decided_by):''}${x.supersedes?' · corrects '+esc(x.supersedes):''}${x.superseded_by?' · replaced by '+esc(x.superseded_by):''}</div>${revs(x)}${x.status==='superseded'?'':`<div class="acts"><button data-ed="${esc(x.id)}">✎ ${x.status==='applied'?'correct':'refine'}</button></div>`+ledit(x.id,`<label>lesson</label><textarea>${esc(x.text)}</textarea><label>capability</label><input class="tgt" value="${esc(x.capability)}"><label>what was wrong (optional, kept with the lesson)</label><input class="note">`,acts)}</div></details>`}).join(''):'<div class="lesson st">no lessons yet</div>';
 $('#lessons').querySelectorAll('[data-l]').forEach(b=>b.onclick=async ev=>{ev.preventDefault();ev.stopPropagation();b.disabled=true;await act('api/lesson/decide',{id:b.dataset.l,approve:!!b.dataset.a});pollLessons(true)})}
$('#lessons').addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;
 if(b.dataset.ed)return toggleEdit($('#lessons'),b.dataset.ed);
 const box=b.closest('.ledit');if(!box||!b.dataset.do)return;const id=box.dataset.for,x=LESSONS.find(l=>l.id===id)||{},m=box.querySelector('.msg'),text=box.querySelector('textarea').value.trim(),capability=box.querySelector('.tgt').value.trim(),note=box.querySelector('.note').value.trim();m.textContent='';
 if(b.dataset.do==='cancel'){box.hidden=true;return pollLessons(true)}
 if(b.dataset.do==='agent'){const what=note||(text!==x.text?'I edited it to:\n'+text:'');if(!what){m.textContent='write what is wrong in the note, or edit the text, first';return}
  box.hidden=true;await sendText(`Please refine lesson ${id} (${x.capability}) with lesson_revise, then ask me to accept it.\nCurrent lesson: ${x.text}\nMy correction: ${what}`,running);return pollLessons(true)}
 b.disabled=true;const r=await act('api/lesson/revise',{id,text,capability,note,approve:b.dataset.do==='approve'});b.disabled=false;if(r&&r.error){m.textContent=r.error;return}box.hidden=true;pollLessons(true)});

// ---- Capabilities
const CAP_GROUPS=[['pipeline','Pipeline'],['workload','Workloads'],['software','Software'],['tools','Tools'],['system','Systems'],['framework','Framework']];
const capGroup=c=>{const m=c.name.match(/^(workload|software|tools|system)_/);return m?m[1]:(c.stages||[]).length?'pipeline':'framework'};
const capRow=c=>`<div class="cap ${c.enabled?'':'off'}"><div class="n">${esc(c.name)} <button class="x" data-ref="${esc(c.name)}" title="Propose a change to this capability">✎</button> <span class="s">v${c.version} · ${esc(c.layer)} · ${c.tools.length} tools${c.lessons?' · '+c.lessons+' lessons':''}</span></div>${c.error?`<div class="bad">${esc(c.error)}</div>`:''}<div class="capedit" data-name="${esc(c.name)}"></div></div>`;
const capEdit=(key,skill,code,acts)=>ledit(key,`<label>skill</label><textarea class="src" data-f="skill">${esc(skill)}</textarea><label>tool.py</label><textarea class="src" data-f="code">${esc(code)}</textarea><label>what was wrong</label><input class="note">`,acts);
async function pollCaps(force){if(!me||(!force&&editing('#capabilities')))return;const d=await api('api/capabilities');if(!d||!d.active)return;CAPS=Object.fromEntries(d.pending.map(p=>[p.id,p]));
 $('#ccount').textContent=`(${d.active.length}${d.pending.length?' · '+d.pending.length+' to approve':''})`;
 const P=d.pending.map(p=>`<div class="capp"><b>${esc(p.kind)}: ${esc(p.name)}</b><div class="why">${esc(p.why)}</div>${(p.review||[]).map(r=>`<div class="flag">⚠ ${esc(r)}</div>`).join('')}${revs(p)}<details><summary>skill</summary><pre>${esc(p.skill)}</pre></details>${p.diff?`<details><summary>diff</summary><pre>${esc(p.diff)}</pre></details>`:`<details><summary>tool.py</summary><pre>${esc(p.code)}</pre></details>`}<div class="acts"><button class="btn primary" data-c="${p.id}" data-a="1">Approve</button><button class="btn" data-c="${p.id}" data-a="">Reject</button><button class="btn" data-ed="${p.id}">✎ Refine</button></div>${capEdit(p.id,p.skill,p.code,'<button data-do="save">Save</button><button class="primary" data-do="approve">Save &amp; approve</button>')}</div>`).join('');
 $('#capabilities').innerHTML=P+CAP_GROUPS.map(([key,name])=>{const cs=d.active.filter(c=>capGroup(c)===key);return cs.length?group(key,`<b>${name}</b> <span class="cnt">${cs.length}</span>`,name,cs.map(capRow).join('')):''}).join('');
 $('#capabilities').querySelectorAll('details.jg').forEach(e=>e.addEventListener('toggle',ev=>{if(ev.target===e)GROUP_OPEN[e.dataset.g]=e.open}));
 if(d.pending.length){const s=document.querySelector('details[data-sec=capabilities]');if(s)s.open=true}
 $('#capabilities').querySelectorAll('[data-c]').forEach(b=>b.onclick=async()=>{b.disabled=true;const r=await act('api/capability/decide',{id:b.dataset.c,approve:!!b.dataset.a});if(r&&r.status==='error')alert(r.error);pollCaps(true)})}
$('#capabilities').addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;const root=$('#capabilities');
 if(b.dataset.ed)return toggleEdit(root,b.dataset.ed);
 if(b.dataset.ref){const n=b.dataset.ref,slot=root.querySelector(`.capedit[data-name="${n}"]`);if(!slot.firstChild){const g=await api('api/capability/'+encodeURIComponent(n));if(!g||g.error||g.status==='error')return;slot.innerHTML=capEdit(n,g.skill,g.code,'<button class="primary" data-do="save" title="It waits here for your approval">Propose change</button>')}return toggleEdit(root,n)}
 const box=b.closest('.ledit');if(!box||!b.dataset.do)return;const key=box.dataset.for,p=CAPS[key],m=box.querySelector('.msg'),note=box.querySelector('.note').value.trim(),val=f=>box.querySelector(`[data-f=${f}]`).value;m.textContent='';
 if(b.dataset.do==='cancel'){box.hidden=true;return pollCaps(true)}
 if(b.dataset.do==='agent'){if(!note){m.textContent='write what is wrong in the note first';return}
  box.hidden=true;await sendText(p?`Please refine capability proposal ${key} (${p.name}) with capability_refine, then ask me to approve it.\nMy correction: ${note}`:`Please change capability ${key} with capability_update, then ask me to approve it.\nWhat is wrong: ${note}`,running);return pollCaps(true)}
 b.disabled=true;const r=await act('api/capability/refine',{...(p?{id:key}:{name:key}),skill:val('skill'),code:val('code'),note,approve:b.dataset.do==='approve'});b.disabled=false;
 if(r&&r.status==='error'){m.textContent=[r.error,...(r.problems||[])].join(' · ');return}box.hidden=true;pollCaps(true)});

// ---- Consolidation: the server looks for overlaps and conflicts by itself; a person starts the work, in its own chat on the server's node
const CONSOL_ASK={lessons:'Consolidate the lessons. Call consolidation_report and read the lessons groups. For overlapping lessons, propose one merged lesson with lesson_merge. If lessons contradict each other, ask me which holds before proposing anything. If a group is different on purpose, use consolidation_keep. Work on this node only: no jobs, no compute nodes. List what you proposed.',
 capabilities:'Consolidate the capabilities. Call consolidation_report and read the capabilities groups, then read each capability with capability_get. For an overlap, propose moving what is shared to the better home with capability_update, and ask me before disabling anything. If rules conflict, ask me which holds before proposing anything. If a group is different on purpose, use consolidation_keep. Work on this node only: no jobs, no compute nodes. List what you proposed.'};
async function pollConsolidation(){if(!me)return;const d=await api('api/consolidation');if(!d||d.error)return;
 for(const [key,id] of [['lessons','#lconsol'],['capabilities','#cconsol']]){const G=d[key]||[],b=$(id),c=G.filter(g=>g.kind==='conflict').length;b.hidden=!G.length;b.classList.toggle('bad',c>0);
  b.textContent=`consolidate ${G.length}`;b.title=`${c} possible conflict${c===1?'':'s'}, ${G.length-c} overlap${G.length-c===1?'':'s'}. Click to start it in a new chat on this node.\n`+G.slice(0,6).map(g=>`${g.kind}: ${g.ids.join(', ')}`).join('\n');
  b.onclick=ev=>{ev.preventDefault();ev.stopPropagation();$('#newbtn').onclick();showTab('chat');setMode('run');setTimeout(()=>sendText(CONSOL_ASK[key],false,[]),0)}}}

// ---- gates: an ask_user that names a pipeline and a stage
const askCard0=Renderer.prototype.askCard;
Renderer.prototype.askCard=function(e){askCard0.call(this,e);let q={};try{q=JSON.parse(e.input)}catch(_){}
 if(!q.pipeline||!q.stage||this.answered)return;
 const cards=this.b.querySelectorAll('.ask'),c=cards[cards.length-1];if(!c)return;
 const ta=c.querySelector('textarea');ta.placeholder='What should change, or what should be remembered?';
 const g=document.createElement('div');g.className='gate';
 g.innerHTML=`<button class="btn primary" data-k="approve">Approve ${esc(q.stage)}</button><button class="btn" data-k="process">Change the process</button><button class="btn" data-k="remember">Remember this</button><div class="hint">Change the process becomes a new or updated capability, which you approve. Remember this becomes a lesson.</div>`;
 c.appendChild(g);dgGate(c,q);const head=`[Gate: pipeline ${q.pipeline}, stage ${q.stage}]`;
 g.querySelectorAll('button').forEach(b=>b.onclick=()=>{const t=ta.value.trim(),k=b.dataset.k;if(k!=='approve'&&!t){ta.focus();return}
  const msg=k==='approve'?`${head} Approved.${t?' Comment: '+t:''} Record it with pipeline_decide, then pipeline_advance.`:k==='process'?`${head} Process feedback, pipeline_feedback kind=process: ${t}`:`${head} Remember, pipeline_feedback kind=remember: ${t}`;
  c.classList.add('done');sendText(msg)})};

// ---- Dashboard: dfdashboard, behind this server's port. It opens a pipeline's run by itself, or any traces given by path.
function dashOpen(p){dash.pid=p.id;dash.run=p.dash_run;dash.label='latest run of '+p.name;dashRender()}
function dashRender(){const v=$('#dashview');if(v.hidden)return;
 if(!$('#dpath')){v.innerHTML=`<form id="dpath" class="dpath"><span id="dshow" class="dshow"></span><input id="dpathi" placeholder="Path to provenance traces: a directory, a .pfw.gz file or a glob" aria-label="Path to provenance traces" spellcheck="false"><button class="btn primary">Open</button><span id="dpathe" class="err"></span></form><iframe class="dframe" id="dframe" title="dfdashboard"></iframe>`;
  $('#dpath').onsubmit=async e=>{e.preventDefault();const path=$('#dpathi').value.trim(),m=$('#dpathe');m.textContent='';if(!path)return;
   const r=await api('api/dashboard/open',{path});if(r.error){m.textContent=r.error;return}dash.run=r.run;dash.view='provenance';dash.label=path;dashRender()}}
 $('#dshow').textContent=dash.run?`Showing ${dash.label||dash.run}`:'';$('#dshow').title=dash.run;
 const u=dash.run?`dash/?view=${dash.view}&run=${encodeURIComponent(dash.run)}`:'dash/',f=$('#dframe');if(f.dataset.src!==u){f.dataset.src=u;f.src=u}}

// ---- theme + status
function themeNow(){return document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}
function themeIcon(){$('#themebtn').textContent=themeNow()==='dark'?'☀':'☾'}
$('#themebtn').onclick=()=>{const t=themeNow()==='dark'?'light':'dark';document.documentElement.dataset.theme=t;try{localStorage.setItem('moirai_theme',t)}catch(_){}themeIcon();if(typeof dgTheme==='function')dgTheme()};themeIcon();
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
function startPanels(){if(location.hash==='#dashboard')showTab('dash');pollPipes();pollConsolidation();pollLessons();pollCaps();pollVersion();
 (function(){const K='moirai_secs';let st={};try{st=JSON.parse(localStorage.getItem(K)||'{}')}catch(_){}
  document.querySelectorAll('details.sec').forEach(d=>{if(d.dataset.sec in st)d.open=!!st[d.dataset.sec];d.addEventListener('toggle',()=>{st[d.dataset.sec]=d.open;try{localStorage.setItem(K,JSON.stringify(st))}catch(_){}})})})()}
setInterval(()=>{if(me&&document.visibilityState==='visible')pollPipes()},PANEL_S);
setInterval(()=>{if(me&&document.visibilityState==='visible'){pollConsolidation();pollLessons();pollCaps()}},PANEL_S*3);
setInterval(pollVersion,3000);
boot();
