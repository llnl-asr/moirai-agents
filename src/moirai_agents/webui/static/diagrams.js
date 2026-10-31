// Diagrams in the page: what a step published (api/diagram/<pipeline>/<name>), shown on the gate card, on the step page and from the Steps link.
// A graph is drawn with Cytoscape (drag, zoom, find, highlight, edit, save) and downloads as SVG, PDF, PNG or JSON; a chart is a Bokeh plot (pan, zoom, hover) that downloads as SVG, PDF or PNG.
// Libraries load the first time a diagram is on screen: static/vendor/*.js (see vendor/THIRD_PARTY.md), BokehJS from the installed bokeh (vendor/bokeh/).
const DG={libs:{},seq:0};
const DGC={artifact:['#bbf7d0','#15803d'],input:['#bfdbfe','#1d4ed8'],intermediate:['#fef3c7','#b45309'],output:['#bbf7d0','#15803d'],parameter:['#e9d5ff','#7e22ce'],
 reference:['#e5e7eb','#4b5563'],network:['#fecaca','#b91c1c'],entity:['#e5e7eb','#4b5563'],activity:['#fed7aa','#c2410c'],file:['#f3f4f6','#6b7280']};
const DGE={used:'#64748b',generated:'#15803d',invalidated:'#dc2626',updated:'#d97706',relation:'#2563eb',flow:'#64748b'};
const DGG=['#fed7aa','#fde68a','#fbcfe8','#c7d2fe','#a7f3d0','#e9d5ff','#bae6fd','#fecdd3'];       // one fill per group (an activity's source file)
const dgEsc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function dgLoad(src){return DG.libs[src]||(DG.libs[src]=new Promise((ok,bad)=>{const s=document.createElement('script');s.src=src;s.onload=ok;s.onerror=()=>{delete DG.libs[src];bad(new Error('could not load '+src))};document.head.appendChild(s)}))}
async function dgNeedGraph(){await dgLoad('vendor/cytoscape.min.js');await dgLoad('vendor/dagre.min.js');await dgLoad('vendor/cytoscape-dagre.js');if(!DG.dagre){try{cytoscape.use(cytoscapeDagre)}catch(_){}DG.dagre=1}}
async function dgNeedPdf(){await dgLoad('vendor/jspdf.umd.min.js');await dgLoad('vendor/svg2pdf.umd.min.js')}
function dgSaveFile(name,blob){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},1500)}
// The placeholder the page scans for. pid: pipeline id, d: an entry from the stage's `diagrams` list.
function dgHtml(pid,d){return `<div class="dgm"><div class="dgh"><b>${dgEsc(d.title)}</b>${d.summary?` <span class="sub">${dgEsc(d.summary)}</span>`:''}${d.edited_ts?' <span class="dgedited">edited by you</span>':''}</div><div class="dgv" data-pid="${dgEsc(pid)}" data-name="${dgEsc(d.name)}"></div></div>`}

// ---- the graph ----
// Box size from the label (text wraps at 150px): Cytoscape's own 'label' sizing is deprecated.
const dgW=n=>Math.min(160,Math.max(52,String(n.data('label')).length*6.6+18)),dgH=n=>15*Math.max(1,Math.ceil(String(n.data('label')).length*6.8/150))+14;
function dgStyle(){const dark=themeNow()==='dark',tx=dark?'#e5e7eb':'#111827',bg=dark?'#1f2937':'#ffffff';
 const st=[{selector:'node',style:{label:'data(label)','text-wrap':'wrap','text-max-width':150,'font-size':12,color:'#111827','text-valign':'center','text-halign':'center',shape:'rectangle',width:dgW,height:dgH,'background-color':'#e5e7eb','border-width':1,'border-color':'#6b7280'}},
  {selector:'node.activity',style:{shape:'round-rectangle'}},{selector:'node.artifact',style:{'border-width':2.5}},
  {selector:'edge',style:{width:1.5,'curve-style':'bezier','target-arrow-shape':'triangle','line-color':DGE.flow,'target-arrow-color':DGE.flow,label:'data(label)','font-size':9,color:tx,'text-background-color':bg,'text-background-opacity':.9,'text-background-padding':2,'text-wrap':'wrap','text-max-width':170,'text-rotation':'autorotate'}},
  {selector:'edge.cond',style:{'line-style':'dashed'}},{selector:'edge.invalidated',style:{'line-style':'dotted',width:2}},
  {selector:'.dim',style:{opacity:.14}},{selector:'.hit',style:{'border-width':3,'border-color':'#f59e0b'}},{selector:'.gone',style:{display:'none'}},
  {selector:':selected',style:{'border-width':3,'border-color':'#2563eb','line-color':'#2563eb','target-arrow-color':'#2563eb'}},
  {selector:'.src',style:{'border-width':3,'border-color':'#16a34a'}}];
 for(const [k,[b,d]] of Object.entries(DGC))st.push({selector:'node.'+k,style:{'background-color':b,'border-color':d}});
 st.push({selector:'node[gcolor]',style:{'background-color':'data(gcolor)'}});
 for(const [k,c] of Object.entries(DGE))st.push({selector:'edge.'+k,style:{'line-color':c,'target-arrow-color':c,...(k==='generated'||k==='relation'?{width:2.5}:{})}});
 return st}
// A node's `parent` is a group: the parent's box is not drawn (the layout cannot place boxes inside boxes well), its children get its name and a colour.
function dgFlatten(g){const par=new Map(g.nodes.filter(n=>g.nodes.some(m=>m.parent===n.id)).map(n=>[n.id,n]));
 const nodes=g.nodes.filter(n=>!par.has(n.id)).map(n=>({...n,parent:undefined,group:n.group||(n.parent&&par.get(n.parent)?par.get(n.parent).label:undefined)}));
 const ids=new Set(nodes.map(n=>n.id));return {...g,nodes,edges:g.edges.filter(e=>ids.has(e.source)&&ids.has(e.target))}}
// Entity relationships: each activity becomes a labelled arrow from what it used to what it made; a delete is a loop on the entity.
function dgCollapse(g){const act=new Set(g.nodes.filter(n=>n.kind==='activity').map(n=>n.id)),lab=Object.fromEntries(g.nodes.map(n=>[n.id,n.label])),nodes=g.nodes.filter(n=>!act.has(n.id));
 const into={},outof={};for(const e of g.edges){if(act.has(e.target)&&!act.has(e.source))(into[e.target]=into[e.target]||[]).push(e);if(act.has(e.source)&&!act.has(e.target))(outof[e.source]=outof[e.source]||[]).push(e)}
 const m=new Map(),put=(s,t,kind,l,c)=>{const k=[s,t,kind,c].join('|'),o=m.get(k);if(o){if(!o.labels.includes(l))o.labels.push(l)}else m.set(k,{source:s,target:t,kind,conditional:c,labels:[l]})};
 for(const a of act){const ins=(into[a]||[]).filter(e=>e.kind==='used'),outs=(outof[a]||[]).filter(e=>e.kind==='generated'||e.kind==='updated'),dels=(outof[a]||[]).filter(e=>e.kind==='invalidated');
  for(const i of ins)for(const o of outs)put(i.source,o.target,o.kind,lab[a],!!(i.conditional||o.conditional));
  for(const d of dels)put(d.target,d.target,'invalidated',lab[a]+' (deletes)',!!d.conditional);
  if(!ins.length)for(const o of outs)put(o.target,o.target,o.kind,lab[a]+' (makes)',!!o.conditional)}
 const edges=[...m.values()].map((e,i)=>({id:'r'+i,source:e.source,target:e.target,kind:e.kind,conditional:e.conditional,label:e.labels.join(', ')}));
 for(const e of g.edges)if(!act.has(e.source)&&!act.has(e.target))edges.push({...e,id:'x'+edges.length});
 return {...g,nodes,edges}}
function dgEls(g){const grp=[...new Set(g.nodes.map(n=>n.group).filter(Boolean))],col=x=>x?DGG[grp.indexOf(x)%DGG.length]:undefined;
 return [...g.nodes.map(n=>({group:'nodes',data:{id:n.id,label:n.label,parent:n.parent||undefined,kind:n.kind||'entity',info:n.info||{},grp:n.group,gcolor:n.kind==='activity'?col(n.group):undefined},classes:n.kind||'entity',...(n.x!=null&&n.y!=null?{position:{x:n.x,y:n.y}}:{})})),
 ...g.edges.map((e,i)=>({group:'edges',data:{id:e.id||'e'+i,source:e.source,target:e.target,label:e.label||'',kind:e.kind||'flow',conditional:!!e.conditional},classes:(e.kind||'flow')+(e.conditional?' cond':'')}))]}
function dgOut(cy){return {nodes:cy.nodes().map(n=>({id:n.id(),label:n.data('label'),kind:n.data('kind'),info:n.data('info'),group:n.data('grp'),x:Math.round(n.position('x')),y:Math.round(n.position('y'))})),
 edges:cy.edges().map(e=>({id:e.id(),source:e.data('source'),target:e.data('target'),label:e.data('label'),kind:e.data('kind'),conditional:!!e.data('conditional')}))}}
function dgLayout(kind,fit){return kind==='force'?{name:'cose',animate:false,fit:fit!==false,padding:30,nodeRepulsion:()=>9000}:kind==='preset'?{name:'preset',fit:fit!==false,padding:30}:{name:'dagre',rankDir:kind==='tb'?'TB':'LR',nodeSep:14,rankSep:46,edgeSep:8,ranker:'tight-tree',fit:fit!==false,padding:30,animate:false}}
function dgWrap(t,n){const w=String(t).split(/\s+/),out=[];let cur='';for(const x of w){if((cur+' '+x).trim().length>n&&cur){out.push(cur);cur=x}else cur=(cur+' '+x).trim()}if(cur)out.push(cur);return out.length?out:['']}
// The SVG is written here, not by a library: it is the picture of the graph as drawn, in vector form, and the PDF is made from it.
function dgSvg(cy,title,note){
 const bb=cy.elements().boundingBox({includeLabels:true,includeOverlays:false}),pad=24,top=title?34:0,W=Math.ceil(Math.max(bb.w+2*pad,340)),H=Math.ceil(bb.h+2*pad+top),ox=pad-bb.x1,oy=pad+top-bb.y1;
 const X=x=>(x+ox).toFixed(1),Y=y=>(y+oy).toFixed(1),o=[`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="Helvetica, Arial, sans-serif">`,'<rect width="100%" height="100%" fill="#ffffff"/>'];
 if(title)o.push(`<text x="${pad}" y="22" font-size="15" font-weight="bold" fill="#111827">${dgEsc(title)}</text>${note?`<text x="${pad}" y="36" font-size="10" fill="#6b7280">${dgEsc(note.slice(0,220))}</text>`:''}`);
 for(const e of cy.edges()){if(e.hasClass('gone'))continue;const k=e.data('kind'),c=DGE[k]||DGE.flow,w=(k==='generated'||k==='relation')?2.5:1.5,dash=e.data('conditional')?'6 4':k==='invalidated'?'2 3':'';
  if(e.isLoop()){const b=e.source().boundingBox({includeLabels:false,includeOverlays:false}),x1=b.x1+b.w*.3,x2=b.x1+b.w*.7,y=b.y1;
   o.push(`<path d="M${X(x1)} ${Y(y)} C${X(x1-16)} ${Y(y-40)} ${X(x2+16)} ${Y(y-40)} ${X(x2)} ${Y(y-9)}" fill="none" stroke="${c}" stroke-width="${w}"${dash?` stroke-dasharray="${dash}"`:''}/><polygon points="${X(x2)},${Y(y)} ${X(x2-4)},${Y(y-9)} ${X(x2+4)},${Y(y-9)}" fill="${c}"/>`);
   const lb=e.data('label');if(lb){const tw=lb.length*5.2+6;o.push(`<rect x="${X((b.x1+b.x2)/2-tw/2)}" y="${Y(y-44)}" width="${tw.toFixed(1)}" height="12" fill="#ffffff" fill-opacity=".9"/><text x="${X((b.x1+b.x2)/2)}" y="${Y(y-35)}" text-anchor="middle" font-size="9" fill="#374151">${dgEsc(lb)}</text>`)}continue}
  const s=e.sourceEndpoint(),t=e.targetEndpoint(),cp=e.controlPoints()||[];
  const tail=cp.length?cp[cp.length-1]:s,ang=Math.atan2(t.y-tail.y,t.x-tail.x),L=9,hx=t.x-Math.cos(ang)*L,hy=t.y-Math.sin(ang)*L,px=-Math.sin(ang)*4,py=Math.cos(ang)*4;
  const d=cp.length>=2?`M${X(s.x)} ${Y(s.y)} C${X(cp[0].x)} ${Y(cp[0].y)} ${X(cp[1].x)} ${Y(cp[1].y)} ${X(hx)} ${Y(hy)}`:cp.length?`M${X(s.x)} ${Y(s.y)} Q${X(cp[0].x)} ${Y(cp[0].y)} ${X(hx)} ${Y(hy)}`:`M${X(s.x)} ${Y(s.y)} L${X(hx)} ${Y(hy)}`;
  o.push(`<path d="${d}" fill="none" stroke="${c}" stroke-width="${w}"${dash?` stroke-dasharray="${dash}"`:''}/>`);
  o.push(`<polygon points="${X(t.x)},${Y(t.y)} ${X(hx+px)},${Y(hy+py)} ${X(hx-px)},${Y(hy-py)}" fill="${c}"/>`);
  const lb=e.data('label');if(lb){const m=e.midpoint(),ls=dgWrap(lb,34),tw=Math.max(...ls.map(x=>x.length))*5.2+6;o.push(`<rect x="${X(m.x-tw/2)}" y="${Y(m.y-6-(ls.length-1)*5.5)}" width="${tw.toFixed(1)}" height="${ls.length*11+2}" fill="#ffffff" fill-opacity=".9"/><text text-anchor="middle" font-size="9" fill="#374151">${ls.map((l,i)=>`<tspan x="${X(m.x)}" y="${Y(m.y+3+(i-(ls.length-1)/2)*11)}">${dgEsc(l)}</tspan>`).join('')}</text>`)}}
 for(const n of cy.nodes()){const b=n.boundingBox({includeLabels:false,includeOverlays:false}),bg=n.style('background-color'),bd=n.style('border-color'),r=n.hasClass('activity')?8:2;
  o.push(`<rect x="${X(b.x1)}" y="${Y(b.y1)}" width="${b.w.toFixed(1)}" height="${b.h.toFixed(1)}" rx="${r}" fill="${bg}" stroke="${bd}" stroke-width="${n.hasClass('artifact')?2.5:1}"/>`);
  const lines=dgWrap(n.data('label'),24),fs=12,lh=fs+2,cx=(b.x1+b.x2)/2,y0=(b.y1+b.y2)/2-(lines.length-1)*lh/2+fs/3;
  o.push(`<text text-anchor="middle" font-size="${fs}" fill="#111827">${lines.map((l,i)=>`<tspan x="${X(cx)}" y="${Y(y0+i*lh)}">${dgEsc(l)}</tspan>`).join('')}</text>`)}
 o.push('</svg>');return o.join('\n')}
async function dgPdfFromSvg(svg,name){await dgNeedPdf();const host=document.createElement('div');host.style.cssText='position:fixed;left:-99999px;top:0';host.innerHTML=svg;document.body.appendChild(host);
 try{const el=host.firstElementChild;let w=+el.getAttribute('width'),h=+el.getAttribute('height');const k=Math.min(1,14000/Math.max(w,h));w*=k;h*=k;
  const {jsPDF}=window.jspdf,doc=new jsPDF({orientation:w>h?'l':'p',unit:'pt',format:[w,h]});await doc.svg(el,{x:0,y:0,width:w,height:h});doc.save(name+'.pdf')}finally{host.remove()}}
function dgChanges(c){const L=[];const add=(t,a)=>a&&a.length&&L.push(`${t}: ${a.map(x=>typeof x==='string'?x:`${x.from} -> ${x.to}`).join('; ')}`);
 add('removed boxes',c.removed_nodes);add('added boxes',c.added_nodes);add('renamed',c.renamed);add('removed arrows',c.removed_edges);add('added arrows',c.added_edges);return L}

async function dgGraph(el,d){
 await dgNeedGraph();const pid=el.dataset.pid,name=d.name;let base=dgFlatten(d.graph),cur=dgFlatten(d.edited||d.graph),undo=[];
 const hasAct=cur.nodes.some(n=>n.kind==='activity');let view=hasAct&&!d.edited?'rel':'full';
 const kinds=[...new Set(dgCollapse(cur).edges.concat(cur.edges).map(e=>e.kind||'flow'))],groups=[...new Set(cur.nodes.map(n=>n.group).filter(Boolean))];
 el.innerHTML=`<div class="dgbar"><button class="btn" data-k="fit" title="Fit the whole graph">Fit</button>${hasAct?`<select data-k="view" title="What to draw"><option value="rel">Entity relationships</option><option value="full">Entities and activities</option></select>`:''}<select data-k="layout" title="Layout"><option value="lr">Layers →</option><option value="tb">Layers ↓</option><option value="force">Force</option></select><input data-k="find" placeholder="Find…" spellcheck="false" aria-label="Find a box">
  ${kinds.map(k=>`<label class="dgk" title="Show or hide ${dgEsc(k)} arrows"><input type="checkbox" data-kind="${dgEsc(k)}" checked><i style="background:${DGE[k]||DGE.flow}"></i>${dgEsc(k)}</label>`).join('')}<span class="sp"></span>
  <button class="btn" data-k="edit" title="Rename, add, link and delete boxes, then save your version (edits the full view)">Edit</button>
  <select data-k="dl" title="Download what is on screen"><option value="">Download…</option><option value="svg">SVG (vector)</option><option value="pdf">PDF (vector)</option><option value="png">PNG</option><option value="json">JSON (graph)</option></select><button class="btn" data-k="big" title="Larger (Esc closes)">⤢</button></div>
  <div class="dgedit" hidden><button class="btn" data-k="add">Add box</button><button class="btn" data-k="link" title="Click a box, then another">Link</button><select data-k="lkind" title="Kind of the new arrow">${['used','generated','invalidated','updated','relation','flow'].map(k=>`<option>${k}</option>`).join('')}</select><button class="btn" data-k="del">Delete selected</button><button class="btn" data-k="undo">Undo</button>
   <span class="sp"></span><button class="btn primary" data-k="save">Save my version</button><button class="btn" data-k="ask" title="Put what you changed into the chat as feedback" hidden>Send changes to the agent</button><button class="btn" data-k="reset" ${d.edited?'':'hidden'}>Back to published</button><span class="dgmsg"></span></div>
  <div class="dgcv"></div><div class="dginfo">Click a box to see what it is and what it is connected to.</div><div class="dglegend">${(d.graph.legend||[]).map(l=>`<span><i class="${dgEsc(l.kind)}"></i>${dgEsc(l.label)}</span>`).join('')}${groups.map((g,i)=>`<span class="dggrp" title="Activities attached in this source file"><i style="background:${DGG[i%DGG.length]}"></i>${dgEsc(g)}</span>`).join('')}</div>`;
 if(hasAct)el.querySelector('[data-k=view]').value=view;
 const $=s=>el.querySelector(s),box=$('.dgcv'),info=$('.dginfo'),msg=$('.dgmsg');let mode='',src=null;
 const pos=g=>g.nodes.length&&g.nodes.every(n=>n.x!=null&&n.y!=null);
 const shown=()=>view==='rel'?dgCollapse(cur):cur;
 const cy=cytoscape({container:box,elements:dgEls(shown()),style:dgStyle(),layout:dgLayout(view==='full'&&pos(cur)?'preset':'lr'),wheelSensitivity:.25,minZoom:.08,maxZoom:3,boxSelectionEnabled:true});
 el._cy=cy;el._reflow=()=>{cy.resize();cy.fit(undefined,30)};el.querySelectorAll('.dggrp').forEach(x=>x.hidden=view==='rel');
 const say=(t,bad)=>{msg.textContent=t;msg.className='dgmsg'+(bad?' bad':'')};
 const push=()=>{undo.push(JSON.stringify(dgOut(cy)))};
 const draw=(g,lay)=>{el.querySelectorAll('.dggrp').forEach(x=>x.hidden=view==='rel');cy.elements().remove();cy.add(dgEls(g));cy.layout(dgLayout(lay||(pos(g)?'preset':'lr'))).run();[...el.querySelectorAll('[data-kind]')].forEach(c=>cy.edges('.'+c.dataset.kind).toggleClass('gone',!c.checked))};
 const describe=n=>{const d=n.data(),inf=Object.entries(d.info||{}).filter(([,v])=>v!==''&&v!=null).map(([k,v])=>`<tr><td>${dgEsc(k)}</td><td>${dgEsc(v)}</td></tr>`).join('');
  const rel=(es,arrow)=>es.map(e=>`<div class="dgrel"><i style="background:${DGE[e.data('kind')]||DGE.flow}"></i>${arrow} <b>${dgEsc((arrow==='←'?e.source():e.target()).data('label'))}</b> <span class="sub">${e.data('label')?dgEsc(e.data('label'))+' · ':''}${dgEsc(e.data('kind'))}${e.data('conditional')?' · only if a condition holds':''}</span></div>`).join('');
  info.innerHTML=`<b>${dgEsc(d.label)}</b>${d.grp?` <span class="sub">${dgEsc(d.grp)}</span>`:''}${inf?`<table>${inf}</table>`:''}${rel(n.incomers('edge'),'←')}${rel(n.outgoers('edge'),'→')}`};
 cy.on('tap','node',ev=>{const n=ev.target;
  if(mode==='link'){if(!src){src=n;n.addClass('src');say('Now click the box it points to');return}if(src.id()!==n.id()){push();const k=$('[data-k=lkind]').value,lb=prompt('Label of the arrow (optional)','')||'';
    cy.add({group:'edges',data:{id:'u'+(++DG.seq),source:src.id(),target:n.id(),label:lb,kind:k,conditional:false},classes:k});say('Arrow added; Save to keep it')}src.removeClass('src');src=null;return}
  cy.elements().addClass('dim');n.closedNeighborhood().removeClass('dim');describe(n)});
 cy.on('tap',ev=>{if(ev.target===cy){cy.elements().removeClass('dim');info.textContent='Click a box to see what it is and what it is connected to.'}});
 cy.on('mouseover','node',ev=>{if(!cy.$(':selected').length&&!cy.$('.dim').length)describe(ev.target)});
 cy.on('dbltap','node,edge',ev=>{if(!mode)return;const e=ev.target,t=prompt('New label',e.data('label'));if(t!=null&&t!==e.data('label')){push();e.data('label',t)}});
 cy.on('dragfree','node',()=>{if(mode)push()});
 const setMode=m=>{if(m&&view!=='full'){view='full';$('[data-k=view]').value='full';draw(cur,'lr')}mode=m;src&&src.removeClass('src');src=null;$('.dgedit').hidden=!m;$('[data-k=edit]').classList.toggle('on',!!m);$('[data-k=link]').classList.toggle('on',m==='link');box.classList.toggle('editing',!!m);say(m==='link'?'Click a box, then the box it points to':m?'Drag to move, double-click to rename':'')};
 el.onclick=async ev=>{const b=ev.target.closest('[data-k]');if(!b||b.tagName==='SELECT'||b.tagName==='INPUT')return;const k=b.dataset.k;
  if(k==='fit')cy.fit(undefined,30);
  else if(k==='big'){el.classList.toggle('big');setTimeout(el._reflow,50)}
  else if(k==='edit')setMode(mode?'':'edit');
  else if(k==='link')setMode(mode==='link'?'edit':'link');
  else if(k==='add'){const t=prompt('Name of the new box','');if(t){push();const p=cy.extent();cy.add({group:'nodes',data:{id:'u'+(++DG.seq),label:t,kind:'entity',info:{kind:'added by you'}},classes:'entity',position:{x:(p.x1+p.x2)/2,y:(p.y1+p.y2)/2}})}}
  else if(k==='del'){const s=cy.$(':selected');if(!s.length){say('Select a box or an arrow first',1);return}push();cy.remove(s)}
  else if(k==='undo'){const s=undo.pop();if(s){cur=JSON.parse(s);draw(cur)}else say('Nothing to undo')}
  else if(k==='save'){const g=dgOut(cy),r=await api('api/diagram/save',{pipeline:pid,name,graph:g});if(r.error){say(r.error,1);return}cur=g;el._changes=r.changes;
   const L=dgChanges(r.changes);say(L.length?'Saved. '+L.join(' · '):'Saved (no change from the published diagram)');$('[data-k=ask]').hidden=!L.length;$('[data-k=reset]').hidden=false}
  else if(k==='ask'){const L=dgChanges(el._changes||{}),inp=document.getElementById('input');if(!inp)return;
   inp.value=`[Gate: pipeline ${pid}, stage ${d.stage}] Process feedback, pipeline_feedback kind=process: I edited the diagram "${d.title}" (${name}). ${L.join('. ')}. Please change the annotation spec to match, then annotate_apply and publish the diagram again.`;if(typeof showTab==='function')showTab('chat');inp.focus()}
  else if(k==='reset'){const r=await api('api/diagram/reset',{pipeline:pid,name});if(r.error){say(r.error,1);return}undo=[];cur=base;draw(shown());b.hidden=true;$('[data-k=ask]').hidden=true;say('Back to the published diagram')}};
 el.onchange=async ev=>{const t=ev.target;
  if(t.dataset.k==='view'){if(mode){say('Leave Edit to change the view',1);t.value=view;return}view=t.value;draw(shown(),'lr')}
  else if(t.dataset.k==='layout'){cy.layout(dgLayout(t.value)).run()}
  else if(t.dataset.kind){cy.edges('.'+t.dataset.kind).toggleClass('gone',!t.checked)}
  else if(t.dataset.k==='dl'){const v=t.value;t.value='';if(!v)return;const note=d.summary||'';
   try{if(v==='svg')dgSaveFile(name+'.svg',new Blob([dgSvg(cy,d.title,note)],{type:'image/svg+xml'}));
    else if(v==='png')dgSaveFile(name+'.png',await (await fetch(cy.png({full:true,scale:2,bg:'#ffffff',output:'base64uri'}))).blob());
    else if(v==='json')dgSaveFile(name+'.json',new Blob([JSON.stringify(dgOut(cy),null,1)],{type:'application/json'}));
    else if(v==='pdf')await dgPdfFromSvg(dgSvg(cy,d.title,note),name)}catch(e){say('Download failed: '+(e.message||e),1)}}};
 el.oninput=ev=>{if(ev.target.dataset.k!=='find')return;const q=ev.target.value.trim().toLowerCase();cy.nodes().removeClass('hit');if(!q)return;
  const m=cy.nodes().filter(n=>n.data('label').toLowerCase().includes(q));m.addClass('hit');if(m.length)cy.animate({fit:{eles:m,padding:80},duration:200})};
 el._onTheme=()=>cy.style(dgStyle());
 if(d.edited)say('Showing your saved version. "Back to published" returns to what the step produced.');
}

// ---- the Bokeh chart ----
async function dgBokeh(el,d){await dgLoad('vendor/bokeh/bokeh.min.js');
 const want=String(d.item.version||'').split('.').slice(0,2).join('.'),have=String(Bokeh.version||'').split('.').slice(0,2).join('.');
 el.innerHTML=`<div class="dgbar"><span class="sub">${dgEsc(d.kind)} chart · pan, wheel to zoom, hover for values, click a legend entry to hide it</span><span class="sp"></span><select data-k="dl" title="Download"><option value="">Download…</option><option value="svg">SVG (vector)</option><option value="pdf">PDF (vector)</option><option value="png">PNG</option></select><button class="btn" data-k="big" title="Larger (Esc closes)">⤢</button></div><div class="dgcv bk"></div><div class="dgmsg"></div>`;
 const box=el.querySelector('.dgcv'),msg=el.querySelector('.dgmsg');box.id='bk'+(++DG.seq);if(want&&have&&want!==have)msg.textContent=`This chart was made by bokeh ${d.item.version} and the page has ${Bokeh.version}; publish it again if it does not draw.`;
 const views=await Bokeh.embed.embed_item(d.item,box.id),v=views?[...views][0]:null;el._reflow=()=>{};      // embed_item returns a view manager: iterate it
 el.onclick=ev=>{if(ev.target.closest('[data-k=big]'))el.classList.toggle('big')};
 el.onchange=async ev=>{const t=ev.target;if(t.dataset.k!=='dl')return;const f=t.value;t.value='';if(!f)return;
  try{if(!v)throw new Error('the chart is not ready');const blob=await v.export(f==='png'?'png':'svg').to_blob();if(f==='pdf')await dgPdfFromSvg(await blob.text(),d.name);else dgSaveFile(d.name+'.'+f,blob)}catch(e){msg.textContent='Download failed: '+(e.message||e)}}}

// ---- finding the placeholders ----
async function dgOpen(el){el.dataset.done='1';el.innerHTML='<div class="dgnote">drawing…</div>';
 const d=await api(`api/diagram/${encodeURIComponent(el.dataset.pid)}/${encodeURIComponent(el.dataset.name)}`);
 if(!d||d.error){el.innerHTML=`<div class="dgerr">${dgEsc((d&&d.error)||'could not load the diagram')}</div>`;return}
 try{if(d.kind==='bokeh')await dgBokeh(el,d);else await dgGraph(el,d)}catch(e){el.innerHTML=`<div class="dgerr">Could not draw this diagram: ${dgEsc(e.message||e)}</div>`}}
const dgScan=()=>document.querySelectorAll('.dgv:not([data-done])').forEach(dgOpen);
new MutationObserver(()=>{clearTimeout(dgScan.t);dgScan.t=setTimeout(dgScan,60)}).observe(document.body,{childList:true,subtree:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){const b=document.querySelector('.dgv.big');if(b){b.classList.remove('big');b._reflow&&setTimeout(b._reflow,50)}}});
function dgTheme(){document.querySelectorAll('.dgv').forEach(e=>e._onTheme&&e._onTheme())}
// A gate that names a stage shows that stage's diagrams on its card, before the options.
async function dgGate(card,q){const s=await api(`api/pipeline/${encodeURIComponent(q.pipeline)}/${encodeURIComponent(q.stage)}`);
 if(!s||s.error||!(s.diagrams||[]).length||card.querySelector('.dgv'))return;
 const w=document.createElement('div');w.className='dgate';w.innerHTML=s.diagrams.map(d=>dgHtml(q.pipeline,d)).join('');
 card.insertBefore(w,card.querySelector('.opt')||card.querySelector('textarea'))}
