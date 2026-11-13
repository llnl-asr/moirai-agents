// The right panel, the stage gates, the Dashboard and self-refresh. Loaded after the chat code in index.html.
const PANEL_S=4000;
let PIPES=[],curPipe=null,dash={pid:null,run:'',seen:'',view:'provenance'},shown=null;
function act(path,body){return api(path,body)}
const TABS={wb:['#wbview','#tabwb','#workbench'],chat:['#chatview','#tabchat',''],pipe:['#pipeview','#tabpipe','#pipeline'],hist:['#histview','#tabhist','#history'],dash:['#dashview','#tabdash','#dashboard']};
function showTab(t){if(t==='state')t='pipe';if(!TABS[t])t='chat';try{history.replaceState(null,'',TABS[t][2]||location.pathname)}catch(_){}
 for(const [k,[v,b]] of Object.entries(TABS)){$(v).hidden=k!==t;$(b).classList.toggle('on',k===t)}
 if(t==='dash')dashRender();if(t==='pipe'){delete $('#pipeview').dataset.h;pipeRender()}if(t==='hist')histRender(true)}
$('#tabchat').onclick=()=>showTab('chat');$('#tabdash').onclick=()=>showTab('dash');$('#tabpipe').onclick=()=>showTab('pipe');$('#tabwb').onclick=()=>{showTab('wb');if(typeof wbLoad==='function')wbLoad()};$('#tabhist').onclick=()=>showTab('hist');

// ---- Steps + Jobs
const DASH_VIEWS=[['provenance','behaviors'],['prov-runtime','runtime'],['prov-temporal','temporal'],['prov-graph','graph']];   // dfdashboard's Provenance views
async function pollPipes(){if(!me)return;const d=await api('api/pipelines'+(session?'?session='+session:''));if(!d||!d.pipelines)return;
 PIPES=d.pipelines;curPipe=PIPES[0]||null;
 $('#scount').textContent=curPipe?`(${curPipe.stages.filter(s=>s.status==='passed').length}/${curPipe.stages.length})`:'';
 $('#steps').innerHTML=PIPES.length?PIPES.slice(0,3).map(p=>`${p.goal?`<div class="pgoal">${esc(p.goal)}</div>`:''}<div class="phead">${esc(p.name)} <span>${esc(p.kind)}${p.session?' · session '+esc(p.session.run_id):''}</span></div>`+p.stages.map(s=>{
   const dl=([v,label])=>`<a href="#dashboard" data-run="${esc(s.run)}" data-view="${v}" data-label="${esc(s.name+' of '+p.name)}">${label}</a>`;
   return `<div class="step ${p.current===s.name?'cur':''}"><span class="sdot ${esc(s.status)}" title="${esc(STEP_WORD[s.status]||s.status)}"></span><a href="#" class="nm" data-pid="${esc(p.id)}" data-stage="${esc(s.name)}" title="what happened in this step">${esc(s.name)}</a>${s.diagrams?`<a href="#" class="dgl" data-pid="${esc(p.id)}" data-stage="${esc(s.name)}" title="${s.diagrams} diagram${s.diagrams>1?'s':''} of this step, kept as an output of the pipeline">diagram</a>`:''}${s.run?`<span class="l">${DASH_VIEWS.map(dl).join(' · ')}</span>`:''}${s.status==='needs_user'?'<span class="wait">waiting for you</span>':''}</div>`}).join('')).join(''):'<div class="lesson st">no pipeline in this chat</div>';
 $('#steps').querySelectorAll('[data-stage]').forEach(a=>a.onclick=e=>{e.preventDefault();shown={pid:a.dataset.pid,stage:a.dataset.stage};PV_PID=a.dataset.pid;showTab('pipe')});
 $('#steps').querySelectorAll('[data-run]').forEach(a=>a.onclick=e=>{e.preventDefault();dash.run=a.dataset.run;dash.view=a.dataset.view;dash.label=a.dataset.label;showTab('dash')});
 if(!$('#pipeview').hidden)pipeRender();
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
const SCHED_NAME={flux:'Flux',slurm:'Slurm'};
const subRow=j=>`<details class="jb" data-id="${esc(j.id)}" data-kind="sched" data-sched="${esc(j.sched||'flux')}" data-live="${j.group==='sched'||j.group==='run'?1:''}"><summary><span class="jdot ${j.group}"></span><span class="n">${esc(j.name)}</span><span class="s">${j.runtime?Math.round(j.runtime)+' s':esc(j.state.toLowerCase())}</span></summary><div class="body"><span class="k">scheduler</span><span class="v">${esc(SCHED_NAME[j.sched]||j.sched||'Flux')}</span><span class="k">id</span><span class="v">${esc(j.id)}</span><span class="k">state</span><span class="v">${esc(j.result||j.state)}</span><span class="k">${j.sched==='slurm'?'partition':'queue'}</span><span class="v">${esc(j.queue)}</span><span class="k">nodes</span><span class="v">${esc(j.nodes)}</span><span class="k">submitted</span><span class="v">${esc(when(j.submitted))}</span></div><div class="jd"></div></details>`;
const JOB_DETAIL={};
const toEnd=r=>{const p=r&&r.querySelector('pre:last-of-type');if(p)p.scrollTop=p.scrollHeight};   // job output opens at its tail
const jobPane=x=>`${x.command?`<div class="jdh">What ran</div><pre>${esc(x.command)}</pre>`:''}${x.cwd?`<div class="jdh">Where</div><div class="jdv">${esc(x.cwd)}</div>`:''}${x.files&&x.files.length?`<div class="jdh">Output file${x.files.length>1?'s':''}</div><div class="jdv">${x.files.map(esc).join('<br>')}</div>`:''}<div class="jdh">Output</div><pre>${esc(x.output||'no output yet')}</pre>`;
async function jobOpen(e){const id=e.dataset.id,pane=e.querySelector('.jd');if(JOB_DETAIL[id]){pane.innerHTML=jobPane(JOB_DETAIL[id]);toEnd(pane)}else pane.textContent='loading…';
 const live=e.dataset.kind==='sched'?e.dataset.live:!JOB_DETAIL[id]||JOB_DETAIL[id].running;if(JOB_DETAIL[id]&&!live)return;
 const x=await api('api/job/detail?'+(e.dataset.kind==='sched'?`sched=${encodeURIComponent(e.dataset.sched)}&id=${encodeURIComponent(id)}`:`pipeline=${encodeURIComponent(e.dataset.pipe)}&job=${encodeURIComponent(id)}`));
 if(!x||x.error||x.status==='error'){pane.textContent=(x&&x.error)||'could not read this job';return}
 JOB_DETAIL[id]=x;const now=$('#jobs').querySelector(`details.jb[data-id="${CSS.escape(id)}"] .jd`);if(now){const pre=now.querySelector('pre:last-of-type'),end=!pre||pre.scrollHeight-pre.scrollTop-pre.clientHeight<30;now.innerHTML=jobPane(x);const p2=now.querySelector('pre:last-of-type');if(p2&&(end||!pre))p2.scrollTop=p2.scrollHeight}}
async function pollSubmitted(){const box=$('#jobs');if(box.querySelector('details.jb[open] .body:focus-within'))return;
 const P=PIPES.flatMap(p=>(p.jobs||[]).map(j=>({...j,p:p.id}))).sort((x,y)=>(y.ended||y.started)-(x.ended||x.started));
 const since=PIPES.length?Math.min(...PIPES.map(p=>p.created||0)):null;
 const d=since===null?null:await api('api/submitted?since='+since),S=(d&&d.jobs)||[],SC=((d&&d.schedulers)||[]).map(s=>SCHED_NAME[s]||s);
 const n=k=>S.filter(j=>j.group===k).length,running=P.filter(j=>j.status==='running').length;
 $('#jcount').textContent=P.length||S.length?`(${running} process${running===1?'':'es'} · ${n('sched')} scheduled · ${n('run')} running)`:'';
 const open=new Set([...box.querySelectorAll('details.jb[open]')].map(e=>e.dataset.id));
 const sub=JOB_GROUPS.map(([k,name,tip])=>{const js=S.filter(j=>j.group===k);return group(k,`<span class="jdot ${k}"></span><b>${name}</b> <span class="cnt">${js.length}</span>`,tip,js.length?js.map(subRow).join(''):noRows,'nest')}).join('');
  const procs=PROC_GROUPS.map(([k,dot,name,tip])=>{const js=P.filter(j=>procGroup(j)===k);return group(k,`<span class="jdot ${dot}"></span><b>${name}</b> <span class="cnt">${js.length}</span>`,tip,js.length?js.map(procRow).join(''):noRows,'nest')}).join('');
 box.innerHTML=group('proc',`<b>Processes</b> <span class="cnt">${P.length}</span>`,'Work this server started on this node',procs)+group('sub',`<b>Submitted jobs</b>${SC.length?` <span class="cnt">${esc(SC.join(' + '))}</span>`:''} <span class="cnt">${S.length}</span>`,SC.length?`Your ${SC.join(' and ')} jobs since this pipeline began`:'No scheduler answers on this system (Flux or Slurm)',sub);
 box.querySelectorAll('details.jb').forEach(e=>{if(open.has(e.dataset.id)){e.open=true;jobOpen(e)}e.addEventListener('toggle',()=>{if(e.open)jobOpen(e)})});
 box.querySelectorAll('details.jg').forEach(e=>e.addEventListener('toggle',ev=>{if(ev.target===e)GROUP_OPEN[e.dataset.g]=e.open}));
 box.querySelectorAll('[data-jid]').forEach(b=>b.onclick=async()=>{await act('api/job/stop',{pipeline:b.dataset.jp,job:b.dataset.jid});pollPipes()})}

// ---- A step's state: what happened in it, opened from its name
const STEP_WORD={needs_user:'waiting for you'};
const when=t=>t?new Date(t*1000).toLocaleString():'';
async function stateRender(){const q=shown,s=await api(`api/pipeline/${encodeURIComponent(q.pid)}/${encodeURIComponent(q.stage)}`),v=$('#pvdetail');if(q!==shown||!v)return;
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
// Lessons and capabilities: what needs you first, approve everything waiting at once, and hide what is decided
const DMSG={};
const hideDecided=k=>{try{return localStorage.getItem('moirai_hide_'+k)==='1'}catch(_){return false}};
const decideBar=(k,pending,decided)=>`<div class="dbar" data-k="${k}">${pending?`<button class="btn primary" data-all="${k}">✓ Approve all (${pending})</button>`:'<span class="sub">nothing waiting</span>'}<label title="Show only what is waiting for you"><input type="checkbox" data-hide="${k}" ${hideDecided(k)?'checked':''}> hide decided${decided?` (${decided})`:''}</label><span class="dmsg">${esc(DMSG[k]||'')}</span></div>`;
function wireDecideBar(k,ids,approve,refresh){const bar=document.querySelector(`.dbar[data-k="${k}"]`);if(!bar)return;
 const h=bar.querySelector('[data-hide]');h.onchange=()=>{try{localStorage.setItem('moirai_hide_'+k,h.checked?'1':'0')}catch(_){}refresh()};
 const a=bar.querySelector('[data-all]');if(!a)return;a.onclick=async e=>{e.preventDefault();e.stopPropagation();const list=ids();if(!list.length)return;
  if(!confirm(`Approve all ${list.length} waiting ${k==='lessons'?'lessons? Each is written into its capability.':'capability changes? Each is checked again and installed.'}`))return;
  a.disabled=true;const m=bar.querySelector('.dmsg'),bad=[];
  for(let i=0;i<list.length;i++){m.textContent=`approving ${i+1}/${list.length}…`;let r=null;try{r=await approve(list[i])}catch(err){r={error:String(err)}}if(!r||r.error)bad.push(`${list[i]}: ${(r&&r.error)||'no answer'}`)}
  DMSG[k]=bad.length?`${list.length-bad.length} approved, ${bad.length} not: ${bad.join('; ').slice(0,300)}`:`✓ ${list.length} approved`;
  setTimeout(()=>{delete DMSG[k]},bad.length?60000:8000);a.disabled=false;refresh()}}
const toggleEdit=(root,id)=>{const e=root.querySelector(`.ledit[data-for="${id}"]`);e.hidden=!e.hidden;if(!e.hidden)e.querySelector('textarea').focus()};
async function pollLessons(force){if(!me||(!force&&editing('#lessons')))return;const d=await api('api/lessons');if(!d||!d.lessons)return;const L=LESSONS=d.lessons,np=L.filter(x=>x.status==='pending').length;
 $('#lcount').textContent=L.length?`(${np?np+' pending · ':''}${L.length})`:'';
 const open=new Set([...document.querySelectorAll('#lessons details[open]')].map(e=>e.dataset.id));
 const hide=hideDecided('lessons'),shown=L.map((x,i)=>[x,i]).sort((a,b)=>(a[0].status==='pending'?0:1)-(b[0].status==='pending'?0:1)||a[1]-b[1]).map(a=>a[0]).filter(x=>!hide||x.status==='pending');
 const bar=decideBar('lessons',np,L.length-np);
 $('#lessons').innerHTML=bar+(shown.length?shown.slice(0,hide?200:60).map(x=>{const p=x.status==='pending';
  const acts=p?'<button data-do="save">Save</button><button class="primary" data-do="approve">Save &amp; accept</button>':x.status==='applied'?'<button data-do="save" title="It waits for you to accept it; this lesson stays until then">Propose correction</button><button class="primary" data-do="approve" title="Replace it now">Correct now</button>':'<button class="primary" data-do="save">Reopen with edit</button>';
  return `<details class="lesson" data-id="${esc(x.id)}" ${open.has(x.id)?'open':''}><summary><span class="ldot ${esc(x.status)}" title="${esc(x.status)}"></span><span class="t">${esc(x.text)}</span>${p?`<button class="x" data-l="${esc(x.id)}" data-a="1" title="Accept: write into ${esc(x.capability)}">✓</button><button class="x" data-l="${esc(x.id)}" data-a="" title="Reject">✗</button>`:''}</summary><div class="body"><div class="tg">${esc(x.capability)} · ${esc(x.source)}</div><div>${esc(x.text)}</div>${x.evidence?`<div class="st">evidence: ${esc(x.evidence)}</div>`:''}<div class="st">${esc(x.status)}${x.decided_by?' · decided by '+esc(x.decided_by):''}${x.supersedes?' · corrects '+esc(x.supersedes):''}${x.superseded_by?' · replaced by '+esc(x.superseded_by):''}</div>${revs(x)}${x.status==='superseded'?'':`<div class="acts"><button data-ed="${esc(x.id)}">✎ ${x.status==='applied'?'correct':'refine'}</button></div>`+ledit(x.id,`<label>lesson</label><textarea>${esc(x.text)}</textarea><label>capability</label><input class="tgt" value="${esc(x.capability)}"><label>what was wrong (optional, kept with the lesson)</label><input class="note">`,acts)}</div></details>`}).join(''):`<div class="lesson st">${L.length?'nothing waiting for you':'no lessons yet'}</div>`);
 wireDecideBar('lessons',()=>L.filter(x=>x.status==='pending').map(x=>x.id),id=>act('api/lesson/decide',{id,approve:true}),()=>pollLessons(true));
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
 const P=d.pending.map(p=>`<div class="capp"><b>${esc(p.kind)}: ${esc(p.name)}</b>${p.stale?`<div class="flag stale">⚠ Out of date: ${esc(p.stale.why)} Redo it on v${esc(p.stale.version)}, or reject it if the current version already does it.</div>`:''}<div class="why">${esc(p.why)}</div>${(p.review||[]).map(r=>`<div class="flag">⚠ ${esc(r)}</div>`).join('')}${revs(p)}<details><summary>skill</summary><pre>${esc(p.skill)}</pre></details>${p.diff?`<details><summary>diff</summary><pre>${esc(p.diff)}</pre></details>`:`<details><summary>tool.py</summary><pre>${esc(p.code)}</pre></details>`}<div class="acts">${p.stale?`<button class="btn primary" data-redo="${p.id}">Redo on v${esc(p.stale.version)}</button><button class="btn" data-c="${p.id}" data-a="" data-note="out of date: ${esc(p.name)} changed to v${esc(p.stale.version)} after it was proposed">Reject (out of date)</button>`:`<button class="btn primary" data-c="${p.id}" data-a="1">Approve</button><button class="btn" data-c="${p.id}" data-a="">Reject</button>`}<button class="btn" data-ed="${p.id}">✎ Refine</button></div><div class="cmsg"></div>${capEdit(p.id,p.skill,p.code,'<button data-do="save">Save</button><button class="primary" data-do="approve">Save &amp; approve</button>')}</div>`).join('');
 const hideC=hideDecided('capabilities');
 $('#capabilities').innerHTML=decideBar('capabilities',d.pending.length,d.active.length)+P+(hideC?(d.pending.length?'':'<div class="lesson st">nothing waiting for you</div>'):CAP_GROUPS.map(([key,name])=>{const cs=d.active.filter(c=>capGroup(c)===key);return cs.length?group(key,`<b>${name}</b> <span class="cnt">${cs.length}</span>`,name,cs.map(capRow).join('')):''}).join(''));
 wireDecideBar('capabilities',()=>d.pending.filter(p=>!p.stale).map(p=>p.id),async id=>{const r=await act('api/capability/decide',{id,approve:true});return r&&r.status==='error'?{error:r.error}:r},()=>pollCaps(true));
 $('#capabilities').querySelectorAll('details.jg').forEach(e=>e.addEventListener('toggle',ev=>{if(ev.target===e)GROUP_OPEN[e.dataset.g]=e.open}));
 if(d.pending.length){const s=document.querySelector('details[data-sec=capabilities]');if(s)s.open=true}
 $('#capabilities').querySelectorAll('[data-c]').forEach(b=>b.onclick=async()=>{b.disabled=true;const m=b.closest('.capp').querySelector('.cmsg');m.textContent=b.dataset.a?'approving…':'rejecting…';
  const r=await act('api/capability/decide',{id:b.dataset.c,approve:!!b.dataset.a,note:b.dataset.note||''});if(r&&r.status==='error'){m.textContent=r.error;b.disabled=false;return}pollCaps(true)})}
$('#capabilities').addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;const root=$('#capabilities');
 if(b.dataset.ed)return toggleEdit(root,b.dataset.ed);
 if(b.dataset.redo){const p=CAPS[b.dataset.redo];if(!p)return;b.disabled=true;b.closest('.capp').querySelector('.cmsg').textContent='sent to the agent: it redoes the change on the current version, then asks you again';
  await sendText(`Capability proposal ${p.id} (${p.name}) is out of date: ${p.stale.why}\nRead the current version with capability_get, redo only what the proposal meant to change (its why: ${p.why}) on top of it with capability_refine on ${p.id} and on_current=true - keep everything the newer version added - or, if v${p.stale.version} already does it, say so and I will reject it. Then ask me to approve.`,running);return}
 if(b.dataset.ref){const n=b.dataset.ref,slot=root.querySelector(`.capedit[data-name="${n}"]`);if(!slot.firstChild){const g=await api('api/capability/'+encodeURIComponent(n));if(!g||g.error||g.status==='error')return;slot.innerHTML=capEdit(n,g.skill,g.code,'<button class="primary" data-do="save" title="It waits here for your approval">Propose change</button>')}return toggleEdit(root,n)}
 const box=b.closest('.ledit');if(!box||!b.dataset.do)return;const key=box.dataset.for,p=CAPS[key],m=box.querySelector('.msg'),note=box.querySelector('.note').value.trim(),val=f=>box.querySelector(`[data-f=${f}]`).value;m.textContent='';
 if(b.dataset.do==='cancel'){box.hidden=true;return pollCaps(true)}
 if(b.dataset.do==='agent'){if(!note){m.textContent='write what is wrong in the note first';return}
  box.hidden=true;await sendText(p?`Please refine capability proposal ${key} (${p.name}) with capability_refine, then ask me to approve it.\nMy correction: ${note}`:`Please change capability ${key} with capability_update, then ask me to approve it.\nWhat is wrong: ${note}`,running);return pollCaps(true)}
 b.disabled=true;const r=await act('api/capability/refine',{...(p?{id:key}:{name:key}),skill:val('skill'),code:val('code'),note,approve:b.dataset.do==='approve'});b.disabled=false;
 if(r&&r.status==='error'){m.textContent=[r.error,...(r.problems||[])].join(' · ');return}box.hidden=true;pollCaps(true)});

// ---- Consolidation: the server looks for overlaps and conflicts by itself; a person starts the work, in its own chat on the server's node
let CQ=[];const CQ_DO={keep:'keep both: different on purpose',supersede:'one holds, the others are retired',merge:'merge into one lesson',drop:'drop the wrong ones'};
async function pollConsolidation(){if(!me)return;const d=await api('api/consolidation');if(!d||d.error)return;CQ=d.questions||[];drawQuestions();
 for(const [key,id] of [['lessons','#lconsol'],['capabilities','#cconsol']]){const G=d[key]||[],b=$(id),c=G.filter(g=>g.kind==='conflict').length,nq=key==='lessons'?CQ.length:0;
  b.hidden=!G.length&&!nq;b.classList.toggle('bad',c>0||nq>0);
  if(nq){b.textContent=`answer ${nq}`;b.title=`${nq} question${nq===1?'':'s'} from the consolidation agent: lessons that contradict each other. Click to answer.`+(G.length?`\n${G.length} more group${G.length===1?'':'s'} not looked at yet.`:'');
   b.onclick=ev=>{ev.preventDefault();ev.stopPropagation();const s=document.querySelector('details[data-sec=lessons]');if(s)s.open=true;const q=$('#lqs .cq');if(q)q.scrollIntoView({block:'start',behavior:'smooth'})};continue}
  b.textContent=`consolidate ${G.length}`;b.title=`${c} possible conflict${c===1?'':'s'}, ${G.length-c} overlap${G.length-c===1?'':'s'}. Click to run the consolidation agent now, in the background (Agents panel).\n`+G.slice(0,6).map(g=>`${g.kind}: ${g.ids.join(', ')}`).join('\n');
  b.onclick=ev=>{ev.preventDefault();ev.stopPropagation();bgStart(key==='lessons'?'consolidation-lessons':'consolidation-capabilities')}}}
// a conflict the agent could not settle: its question, the lessons in full, the options with its recommendation preselected, or your own answer
function drawQuestions(){const box=$('#lqs');if(!box||box.contains(document.activeElement)&&document.activeElement.tagName==='TEXTAREA')return;
 box.innerHTML=CQ.map(q=>`<div class="cq" data-q="${esc(q.id)}"><div class="cqh">? ${esc(q.question)}</div>
  <div class="cql">${q.ids.map(i=>{const l=(q.lessons||{})[i]||{};return `<div><b>${esc(i)}</b> <span class="tg">${esc(l.capability||'')}</span><div>${esc(l.text||'')}</div></div>`}).join('')}</div>
  <div class="cqo">${q.options.map((o,k)=>`<label class="${k===q.recommended?'rec':''}"><input type="radio" name="${esc(q.id)}" value="${k}" ${k===q.recommended?'checked':''}> <span><b>${esc(o.label)}</b>${k===q.recommended?' <span class="recm">recommended</span>':''}<span class="st"> · ${esc(CQ_DO[o.do]||o.do)}${o.do==='supersede'?': '+esc(o.winner)+' holds':o.do==='drop'?': '+esc((o.drop||[]).join(', ')):''}</span>${o.do==='merge'?`<div class="st">→ ${esc(o.text)}</div>`:''}</span></label>`).join('')}
   <label><input type="radio" name="${esc(q.id)}" value=""> <span><b>Something else</b><span class="st"> · tell the agent in your words, it acts on it now</span></span></label></div>
  <div class="cqw"><b>Why the agent recommends it:</b> ${esc(q.why)}</div>
  <textarea class="note" placeholder="optional: a note kept with the decision (required for “something else”)"></textarea>
  <div class="acts"><button class="btn primary" data-qa="1">Apply</button><span class="msg st"></span></div></div>`).join('')}
$('#lqs').addEventListener('click',async ev=>{const b=ev.target.closest('[data-qa]');if(!b)return;ev.preventDefault();const c=b.closest('.cq'),m=c.querySelector('.msg'),r0=c.querySelector('input[type=radio]:checked'),note=c.querySelector('textarea').value.trim();
 const choice=r0?r0.value:'';if(choice===''&&!note){m.textContent='write your answer for the agent first';return}
 b.disabled=true;m.textContent='applying…';const r=await act('api/consolidation/answer',{id:c.dataset.q,choice:choice===''?null:+choice,note});b.disabled=false;
 if(!r||r.status==='error'||r.error){m.textContent=(r&&(r.error))||'failed';return}
 m.textContent=r.status==='for_agent'?`sent to the consolidation agent${r.agent?' ('+r.agent+')':''}`:'✓ applied';c.querySelector('textarea').blur();
 setTimeout(()=>{pollConsolidation();pollLessons(true);pollBackground()},700)});

// ---- gates: an ask_user that names a pipeline and a stage
const askCard0=Renderer.prototype.askCard;
Renderer.prototype.askCard=function(e){askCard0.call(this,e);let q={};try{q=JSON.parse(e.input)}catch(_){}
 if(!q.pipeline||!q.stage||this.answered)return;
 const cards=this.b.querySelectorAll('.ask'),c=cards[cards.length-1];if(!c||c.classList.contains('done'))return;
 const ta=c.querySelector('textarea');ta.placeholder='What should change, or what should be remembered?';
 const g=document.createElement('div');g.className='gate';
 g.innerHTML=`<button class="btn primary" data-k="approve">Approve ${esc(q.stage)}</button><button class="btn" data-k="process">Change the process</button><button class="btn" data-k="remember">Remember this</button><div class="hint">Change the process becomes a new or updated capability, which you approve. Remember this becomes a lesson.</div>`;
 c.appendChild(g);dgGate(c,q);const head=`[Gate: pipeline ${q.pipeline}, stage ${q.stage}]`;
 g.querySelectorAll('button').forEach(b=>b.onclick=()=>{const t=ta.value.trim(),k=b.dataset.k;if(k!=='approve'&&!t){ta.focus();return}
  const msg=k==='approve'?`${head} Approved.${t?' Comment: '+t:''} Record it with pipeline_decide, then pipeline_advance.`:k==='process'?`${head} Process feedback, pipeline_feedback kind=process: ${t}`:`${head} Remember, pipeline_feedback kind=remember: ${t}`;
  c.classList.add('done');sendText(msg).then(ok=>{if(ok)c._show([],t,{approve:'Approved',process:'Change the process',remember:'Remember this'}[k]);else c.classList.remove('done')})})};

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
$('#statuspanel').onclick=e=>e.stopPropagation();
$('#statusbtn').onclick=e=>{e.stopPropagation();const w=$('#statuswrap'),o=w.classList.toggle('open');$('#statusbtn').setAttribute('aria-expanded',o);if(o)loadMcp()};
// ---- MCP servers in Settings: state of each (last check, copies serving chats now), check and restart
let MCP={servers:[],busy:{}};
async function loadMcp(){const d=await api('api/mcp');if(d&&Array.isArray(d.servers)){MCP={...MCP,...d};drawMcp()}}
function mcpLine(s){const c=s.check,busy=MCP.busy[s.name]||MCP.busy.all;
 const st=busy?'run':c?(c.ok?'ok':c.ok===false?'bad':''):'';
 const det=busy?`${busy}…`:c?(c.ok?`${c.tools} tools · started in ${c.secs}s`:(c.detail||'failed')):'not checked yet';
 return `<div class="mcp-row"><span class="mcp-dot ${st}"></span><b>${esc(s.name)}</b><span class="note" title="${esc(det)}">${esc(det)}${s.processes?` · serving ${s.processes} chat process${s.processes===1?'':'es'}`:''}</span>
  <button class="btn mcp-r" data-mcp="${esc(s.name)}" ${busy?'disabled':''} title="Stop the copies serving chats, then start it once and check that it works">⟳ Restart</button></div>`}
function drawMcp(){const box=$('#mcpbox');if(!box)return;
 box.innerHTML=`<div class="sp-h" style="margin-top:12px;display:flex;align-items:center;gap:8px">MCP servers<span style="margin-left:auto;display:flex;gap:6px">
   <button class="btn mcp-r" data-check="all" title="Start each server once the way Claude Code does and list its tools">Check</button><button class="btn mcp-r" data-mcp="all" ${MCP.busy.all?'disabled':''}>⟳ Restart all</button></span></div>`+
  (MCP.servers||[]).map(mcpLine).join('')+(MCP.chats_running?`<div class="note" style="margin-top:4px">${MCP.chats_running} chat${MCP.chats_running===1?' is':'s are'} running: a restart drops the server from ${MCP.chats_running===1?'it':'them'} until the next message.</div>`:'');
 box.querySelectorAll('[data-mcp]').forEach(b=>b.onclick=()=>mcpAct('restart',b.dataset.mcp));
 box.querySelectorAll('[data-check]').forEach(b=>b.onclick=()=>mcpAct('check',b.dataset.check))}
async function mcpAct(kind,name){if(MCP.busy[name])return;
 if(kind==='restart'&&MCP.chats_running&&!confirm(`${MCP.chats_running} chat(s) are running. Restarting ${name==='all'?'the MCP servers':name} drops ${name==='all'?'them':'it'} from those chats until their next message. Restart now?`))return;
 MCP.busy[name]=kind==='restart'?'restarting':'checking';drawMcp();
 const r=await api('api/mcp/'+kind,{name});delete MCP.busy[name];
 if(r&&r.error)alert(r.error);if(r&&Array.isArray(r.servers))MCP={...MCP,...r};drawMcp()}
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
function startPanels(){const h={'#dashboard':'dash','#pipeline':'pipe','#history':'hist','#chat':'chat'}[location.hash],vw={'#annotate':'ann','#live':'live','#query':'query','#workbench':''}[location.hash];
 if(typeof WB!=='undefined'&&WB.role==='domain')setRole('domain');if(h&&!(typeof WB!=='undefined'&&WB.role==='domain'))showTab(h);else if(typeof wbShow==='function')wbShow(vw||WB.view);pollPipes();pollBackground();pollConsolidation();pollLessons();pollCaps();pollVersion();
 (function(){const K='moirai_secs';let st={};try{st=JSON.parse(localStorage.getItem(K)||'{}')}catch(_){}
  document.querySelectorAll('details.sec').forEach(d=>{if(d.dataset.sec in st)d.open=!!st[d.dataset.sec];d.addEventListener('toggle',()=>{st[d.dataset.sec]=d.open;try{localStorage.setItem(K,JSON.stringify(st))}catch(_){}})})})()}
setInterval(()=>{if(me&&document.visibilityState==='visible')pollPipes()},PANEL_S);
setInterval(()=>{if(me&&document.visibilityState==='visible'){pollConsolidation();pollLessons();pollCaps()}},PANEL_S*3);
setInterval(pollVersion,3000);
boot();
