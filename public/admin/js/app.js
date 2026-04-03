const {createApp,ref,reactive,computed,onMounted,nextTick,watch}=Vue;
createApp({setup(){

// ════════════ Auth ════════════
const authed=ref(false),ltk=ref(''),lerr=ref('');
async function login(){
 lerr.value='';
 const t=ltk.value.trim();
 if(!t){lerr.value='请输入 Token';return}
 try{
  const r=await fetch('/v1/admin/auth/check',{headers:{'Authorization':'Bearer '+t}});
  if(!r.ok){lerr.value='Token 无效';return}
  localStorage.setItem('ctk',t);
  authed.value=true;
  initLoad();
 }catch(e){lerr.value='连接失败'}
}
function logout(){localStorage.removeItem('ctk');authed.value=false;ltk.value=''}

// ════════════ Toast ════════════
const tshow=ref(false),tmsg=ref(''),ttyp=ref('tok');
function toast(m,ok=true){tmsg.value=m;ttyp.value=ok?'tok':'terr';tshow.value=true;setTimeout(()=>tshow.value=false,2200)}

// ════════════ API ════════════
async function api(u,o={}){
 const tk=localStorage.getItem('ctk')||'';
 const h={...(o.headers||{}),'Authorization':'Bearer '+tk};
 const r=await fetch(u,{...o,headers:h});
 if(r.status===401){authed.value=false;localStorage.removeItem('ctk');throw new Error('401')}
 if(!r.ok){let d='';try{d=(await r.json()).detail||''}catch(e){}toast(`错误 ${r.status} ${d}`,false);throw new Error(String(r.status))}
 return r.json()
}

// ════════════ Navigation ════════════
const navTabs=[{id:'canvas',label:'节点画布',icon:'⬡'},{id:'tools',label:'工具',icon:'🔧'},{id:'skills',label:'技能',icon:'📚'},{id:'config',label:'配置',icon:'⚙'},{id:'dash',label:'概览',icon:'◎'}];
const view=ref('canvas');
function goView(v){
 _panStart=null;
 _dragItem=null;
 isPanning.value=false;
 dragUid.value=null;
 ed.value=null;
 view.value=v;
 if(v==='dash')loadDash();if(v==='config')cfgLd('runtime','runtime');if(v==='tools')loadTools();if(v==='skills')loadSkills()}

// ════════════ Data ════════════
const allNodeDefs=ref({});
const entryNodeId=ref('');

async function loadNodeDefs(){
 try{
  const nl=await api('/v1/admin/config/nodes');
  const m={};
  nl.forEach(n=>m[n.id]=n);
  allNodeDefs.value=m;
 }catch(e){}
}
async function loadEntryNode(){
 try{
  const raw=await api('/v1/admin/config/runtime/raw');
  const obj=jsyaml.load(raw.content)||{};
  entryNodeId.value=(obj.shell&&obj.shell.entry_node_id)||'bootstrap.shell_orchestrator';
 }catch(e){entryNodeId.value='bootstrap.shell_orchestrator'}
}

// ════════════ Canvas State ════════════
const treeItems=ref([]),treeEdges=ref([]);
const panX=ref(0),panY=ref(0),zoom=ref(1);
const selUid=ref(null),dragUid=ref(null),isPanning=ref(false);
const vpRef=ref(null),editing=ref(null),rawEd=ref(null);
const wireFrom=ref(null),wireEndX=ref(0),wireEndY=ref(0),wireSnap=ref(null),dragSnap=ref(null);

function portInXY(n){return{x:n.x+3,y:n.y+n.h/2}}
function portOutXY(n){return{x:n.x+n.w-1.5,y:n.y+n.h/2}}
const zoomPct=computed(()=>Math.round(zoom.value*100));
const aiItems=computed(()=>treeItems.value.filter(i=>i.kind==='ai'));
const toolItems=computed(()=>treeItems.value.filter(i=>i.kind==='tool'));
const nodeList=computed(()=>Object.values(allNodeDefs.value).sort((a,b)=>(a.id<b.id?-1:1)));

function shortRoute(r){return r&&r.length>22?r.slice(0,20)+'…':r}
function shortTool(s){return s&&s.length>18?s.slice(0,16)+'…':s}
function nodeGlyph(n){return n.isEntry?'▶':'⬡'}
function sanitizeKey(v){return String(v||'').replace(/[^a-zA-Z0-9._-]+/g,'_')}
function cloneObj(v){return JSON.parse(JSON.stringify(v||{}))}

// ════════════ Tree Building ════════════
const ALL_TOOLS=ref([]);
const AI_W=200,AI_H=120,TOOL_W=110,TOOL_H=22;
const CHILD_R=380,TOOL_R=160,CHILD_WT=5,DECAY=0.85;

function resolveTools(nd){
 if(!nd||!nd.tool_access)return[];
 const ta=nd.tool_access;
 const m=(typeof ta==='string')?ta:(ta.mode||'none');
 if(m==='allowlist')return[...(ta.allow||[])];
 if(m==='all'){const d=new Set(ta.deny||[]);return ALL_TOOLS.value.filter(t=>!d.has(t))}
 return[];
}

function buildCallTree(nid,nodeDefs,visited,pathKey=''){
 const nd=nodeDefs[nid];
 if(!nd)return null;
 const uid=pathKey||`node_${sanitizeKey(nid)}`;
 const tools=resolveTools(nd);
 const children=[];
 const vis2=new Set(visited);
 vis2.add(nid);

 const targets=nd.delegate_targets||[];
 for(const targetId of targets){
  if(!vis2.has(targetId)&&nodeDefs[targetId]){
   const childPath=`${uid}__dt_${sanitizeKey(targetId)}`;
   const sub=buildCallTree(targetId,nodeDefs,vis2,childPath);
   if(sub)children.push({label:'',kind:'delegate',sub});
  }
 }

 return{
  uid,
  id:nid,
  name:nd.name||nid.split('.').pop(),
  type:nd.type||'ai',
  model:nd.model||'',
  isEntry:nid===entryNodeId.value,
  tools,
  children,
 };
}

function layoutTree(tree,cx,cy,parentAngle,depth,items,edges){
 const aiItem={uid:tree.uid,id:tree.id,kind:'ai',name:tree.name,type:tree.type,model:tree.model,isEntry:tree.isEntry,toolNames:tree.tools,x:cx-AI_W/2,y:cy-AI_H/2,w:AI_W,h:AI_H};
 items.push(aiItem);

 const outs=[];
 tree.children.forEach(c=>outs.push({kind:'child',ref:c,weight:CHILD_WT}));
 tree.tools.forEach(name=>outs.push({kind:'tool',name,uid:`${tree.uid}__tool_${sanitizeKey(name)}`,weight:1}));
 if(!outs.length)return;

 let sweep,baseAngle;
 if(depth===0){
  sweep=Math.PI*2;
  baseAngle=-Math.PI/2;
 }else{
  const totalWeight=tree.children.length*CHILD_WT+tree.tools.length;
  sweep=Math.min(Math.PI*1.6,Math.max(0.6,totalWeight*0.15));
  baseAngle=parentAngle;
 }

 const totalW=outs.reduce((s,o)=>s+o.weight,0);
 const perW=sweep/totalW;
 let angle=baseAngle-sweep/2;

 outs.forEach(o=>{
  const half=perW*o.weight/2;
  angle+=half;
  if(o.kind==='child'){
   const r=CHILD_R*Math.pow(DECAY,depth);
   const ox=cx+r*Math.cos(angle),oy=cy+r*Math.sin(angle);
   const before=items.length;
   layoutTree(o.ref.sub,ox,oy,angle,depth+1,items,edges);
   const ch=items.slice(before).find(it=>it.kind==='ai');
   if(ch)edges.push({fromUid:aiItem.uid,toUid:ch.uid,type:'delegate',label:''});
  }else{
   const r=TOOL_R*Math.pow(DECAY,depth);
   const ti={uid:o.uid,kind:'tool',name:o.name,parentUid:aiItem.uid,parentNodeId:tree.id,x:cx+r*Math.cos(angle)-TOOL_W/2,y:cy+r*Math.sin(angle)-TOOL_H/2,w:TOOL_W,h:TOOL_H};
   items.push(ti);
   edges.push({fromUid:aiItem.uid,toUid:ti.uid,type:'tool'});
  }
  angle+=half;
 });
}

function _loadPositions(){
 try{return JSON.parse(localStorage.getItem('clonoth_ui_pos')||'{}')}catch(e){return{}}
}
function _savePositions(){
 const pos={};
 treeItems.value.forEach(it=>{pos[it.uid]={x:Math.round(it.x),y:Math.round(it.y)}});
 localStorage.setItem('clonoth_ui_pos',JSON.stringify(pos));
}
function applySavedPositions(items){
 const pos=_loadPositions();
 items.forEach(it=>{
  const p=pos[it.uid];
  if(p&&Number.isFinite(Number(p.x))&&Number.isFinite(Number(p.y))){
   it.x=Number(p.x);
   it.y=Number(p.y);
  }
 });
}

function rebuildTree(){
 const nodeDefs=allNodeDefs.value;
 const nodeIds=Object.keys(nodeDefs);
 if(!nodeIds.length){treeItems.value=[];treeEdges.value=[];return}
 const items=[],edgesList=[];
 const rootId=entryNodeId.value&&nodeDefs[entryNodeId.value]?entryNodeId.value:nodeIds[0];
 if(rootId){
  const tree=buildCallTree(rootId,nodeDefs,new Set(),`node_${sanitizeKey(rootId)}`);
  if(tree)layoutTree(tree,600,500,0,0,items,edgesList);
 }
 const treeIds=new Set(items.filter(i=>i.kind==='ai').map(i=>i.id));
 const orphans=nodeIds.filter(id=>!treeIds.has(id));
 if(orphans.length){
  let ox=600-(orphans.length*(AI_W+20))/2;
  orphans.forEach(id=>{
   const nd=nodeDefs[id]||{};
   items.push({uid:`orphan_${sanitizeKey(id)}`,id,kind:'ai',name:nd.name||id.split('.').pop(),type:'ai',model:nd.model||'',isEntry:id===entryNodeId.value,toolNames:resolveTools(nd),x:ox,y:920,w:AI_W,h:AI_H});
   ox+=AI_W+20;
  });
 }
 applySavedPositions(items);
 treeItems.value=items;
 treeEdges.value=edgesList;
}

// ════════════ Connections ════════════
function rectEdge(rx,ry,rw,rh,tx,ty){
 const cx=rx+rw/2,cy=ry+rh/2,dx=tx-cx,dy=ty-cy;
 if(dx===0&&dy===0)return{x:cx,y:cy};
 const s=Math.abs(dx)/rw>Math.abs(dy)/rh?rw/2/Math.abs(dx):rh/2/Math.abs(dy);
 return{x:cx+dx*s,y:cy+dy*s};
}
const conns=computed(()=>{
 const map={};
 treeItems.value.forEach(it=>map[it.uid]=it);
 return treeEdges.value.map(e=>{
  const f=map[e.fromUid],t=map[e.toUid];
  if(!f||!t)return null;
  const fcx=f.x+f.w/2,fcy=f.y+f.h/2,tcx=t.x+t.w/2,tcy=t.y+t.h/2;
  const sp=rectEdge(f.x,f.y,f.w,f.h,tcx,tcy),ep=rectEdge(t.x,t.y,t.w,t.h,fcx,fcy);
  const dx=ep.x-sp.x,dy=ep.y-sp.y,len=Math.sqrt(dx*dx+dy*dy)||1;
  const mx=(sp.x+ep.x)/2,my=(sp.y+ep.y)/2;
  const off=e.type==='tool'?0:len*0.08;
  const cpx=mx-dy/len*off,cpy=my+dx/len*off;
  const path=`M ${sp.x} ${sp.y} Q ${cpx} ${cpy} ${ep.x} ${ep.y}`;
  return{path,type:e.type,lx:(sp.x+ep.x)/2,ly:(sp.y+ep.y)/2-8};
 }).filter(Boolean);
});

// ════════════ Canvas Mouse ════════════
let _panStart=null,_dragItem=null;
function cvDown(e){
 if(e.target.closest('.nd')||e.target.closest('.tool-chip')||e.target.closest('.rpanel')||e.target.closest('.rpbg')||e.target.closest('.raw-box'))return;
 _panStart={mx:e.clientX,my:e.clientY,px:panX.value,py:panY.value};
 isPanning.value=true;
}
function cvMove(e){
 if(_panStart){
  panX.value=_panStart.px+(e.clientX-_panStart.mx);
  panY.value=_panStart.py+(e.clientY-_panStart.my);
  return;
 }
 if(_dragItem){
  _dragItem.item.x=_dragItem.ox+(e.clientX-_dragItem.mx)/zoom.value;
  _dragItem.item.y=_dragItem.oy+(e.clientY-_dragItem.my)/zoom.value;
  if(_dragItem.item.kind==='ai'){
   const dn=_dragItem.item;
   const myOut=portOutXY(dn);
   const myIn=portInXY(dn);
   let best=null,bestDist=45;
   aiItems.value.forEach(nd=>{
    if(nd.uid===dn.uid)return;
    const thIn=portInXY(nd);
    const thOut=portOutXY(nd);
    let dx=myOut.x-thIn.x,dy=myOut.y-thIn.y,d=Math.sqrt(dx*dx+dy*dy);
    if(d<bestDist){bestDist=d;best={x1:myOut.x,y1:myOut.y,x2:thIn.x,y2:thIn.y,fromId:dn.id,toId:nd.id}}
    dx=thOut.x-myIn.x;dy=thOut.y-myIn.y;d=Math.sqrt(dx*dx+dy*dy);
    if(d<bestDist){bestDist=d;best={x1:thOut.x,y1:thOut.y,x2:myIn.x,y2:myIn.y,fromId:nd.id,toId:dn.id}}
   });
   dragSnap.value=best;
  }else{dragSnap.value=null}
 }
 if(wireFrom.value){
  const rect=vpRef.value.getBoundingClientRect();
  wireEndX.value=(e.clientX-rect.left-panX.value)/zoom.value;
  wireEndY.value=(e.clientY-rect.top-panY.value)/zoom.value;
  let best=null,bestDist=60;
  const seekSide=wireFrom.value.dir==='out'?'in':'out';
  aiItems.value.forEach(nd=>{
   if(nd.uid===wireFrom.value.uid)return;
   const pt=seekSide==='in'?portInXY(nd):portOutXY(nd);
   const px=pt.x,py=pt.y;
   const dx=wireEndX.value-px,dy=wireEndY.value-py;
   const dist=Math.sqrt(dx*dx+dy*dy);
   if(dist<bestDist){bestDist=dist;best={x:px,y:py,uid:nd.uid,id:nd.id}}
  });
  wireSnap.value=best;
 }
}
function cvUp(){
  if(wireFrom.value&&wireSnap.value){
   const fromId=wireFrom.value.dir==='out'?wireFrom.value.nodeId:wireSnap.value.id;
   const toId=wireFrom.value.dir==='out'?wireSnap.value.id:wireFrom.value.nodeId;
   _doConnect(fromId,toId);
  }
  wireFrom.value=null;wireSnap.value=null;
  if(dragSnap.value){
   const ds=dragSnap.value;
   _doConnect(ds.fromId,ds.toId);
   dragSnap.value=null;
  }
  if(_dragItem)_savePositions();
  _panStart=null;_dragItem=null;isPanning.value=false;dragUid.value=null;
}
async function _doConnect(fromId,toId){
  if(fromId===toId)return;
  try{
   const raw=await api(`/v1/admin/config/nodes/${fromId}/raw`);
   let obj=jsyaml.load(raw.content)||{};
   if(!Array.isArray(obj.delegate_targets))obj.delegate_targets=[];
   if(obj.delegate_targets.includes(toId)){toast('已存在该委派关系');return}
   obj.delegate_targets.push(toId);
   const ys=jsyaml.dump(obj,{sortKeys:false,lineWidth:120});
   await api(`/v1/admin/config/nodes/${fromId}/raw`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ys})});
   await loadNodeDefs();
   rebuildTree();
   toast('委派关系已创建');
  }catch(e){}
}
function cvWheel(e){
 const d=e.deltaY>0?0.92:1.08;
 const ns=Math.max(0.15,Math.min(3,zoom.value*d));
 const rect=vpRef.value.getBoundingClientRect();
 const mx=e.clientX-rect.left,my=e.clientY-rect.top;
 panX.value=mx-(mx-panX.value)*(ns/zoom.value);
 panY.value=my-(my-panY.value)*(ns/zoom.value);
 zoom.value=ns;
}
function ndDown(e,n){
 selUid.value=n.uid;dragUid.value=n.uid;
 _dragItem={item:n,mx:e.clientX,my:e.clientY,ox:n.x,oy:n.y};
 _panStart=null;
 if(e.detail===2)openEd(n.id);
}
function toolDown(e,t){
 selUid.value=t.uid;dragUid.value=t.uid;
 _dragItem={item:t,mx:e.clientX,my:e.clientY,ox:t.x,oy:t.y};
 _panStart=null;
 if(e.detail===2)openEd(t.parentNodeId,'tools');
}
function wireStart(e,n,dir){
 const pt=dir==='out'?portOutXY(n):portInXY(n);
 const px=pt.x,py=pt.y;
 wireFrom.value={uid:n.uid,nodeId:n.id,dir,x:px,y:py};
 wireEndX.value=px;
 wireEndY.value=py;
}
function zoomIn(){zoom.value=Math.min(3,zoom.value*1.15)}
function zoomOut(){zoom.value=Math.max(0.15,zoom.value*0.87)}
function fitView(){
 const ns=treeItems.value;
 if(!ns.length)return;
 const el=vpRef.value;
 if(!el)return;
 const vw=el.clientWidth,vh=el.clientHeight;
 let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
 ns.forEach(n=>{minX=Math.min(minX,n.x);minY=Math.min(minY,n.y);maxX=Math.max(maxX,n.x+n.w);maxY=Math.max(maxY,n.y+n.h)});
 const gw=maxX-minX+100,gh=maxY-minY+100;
 const z=Math.max(0.15,Math.min(1.5,Math.min(vw/gw,vh/gh)*0.85));
 zoom.value=z;
 panX.value=(vw-gw*z)/2-minX*z+50*z;
 panY.value=(vh-gh*z)/2-minY*z+50*z;
}

// ════════════ Node Editor ════════════
const newTool=ref(''),newDeny=ref(''),newDelegate=ref('');
const otherNodes=computed(()=>{
 return Object.keys(allNodeDefs.value)
  .filter(id=>!editing.value||id!==editing.value.id)
  .map(id=>({id,name:(allNodeDefs.value[id]||{}).name||id}));
});
async function openEd(nodeId,focus=''){
 let model='',promptText='',ta=[],td=[],tm='none',desc='',name=nodeId,type='ai',skillMode='all',skillAllow=[],delegateTargets=[];
 try{
  const raw=await api(`/v1/admin/config/nodes/${nodeId}/raw`);
  const obj=jsyaml.load(raw.content)||{};
  const t=obj.tool_access||{};
  if(typeof t==='string')tm=t;
  else{tm=t.mode||'none';ta=[...(t.allow||[])];td=[...(t.deny||[])]}
  const s=obj.skills||{};
  if(typeof s==='string')skillMode=s;
  else{skillMode=s.mode||'all';skillAllow=[...(s.allow||[])]}
  desc=obj.description||'';
  name=obj.name||nodeId;
  type=obj.type||'ai';
  model=obj.model||'';
  promptText=typeof obj.prompt==='string'?obj.prompt:'';
  delegateTargets=[...(obj.delegate_targets||[])];
 }catch(e){}
 editing.value=reactive({
  id:nodeId,
  name,
  type,
  model,
  description:desc,
  prompt_text:promptText,
  tool_mode:tm,
  tool_allow:ta,
  tool_deny:td,
  skill_mode:skillMode,
  skill_allow:skillAllow,
  delegate_targets:delegateTargets,
  isEntry:nodeId===entryNodeId.value,
 });
 await nextTick();
 if(focus)jumpEd(focus);
}
function closeEd(){editing.value=null}
function jumpEd(section){nextTick(()=>{const el=document.getElementById('pn-sec-'+section);if(el)el.scrollIntoView({behavior:'smooth',block:'start'})})}

function addTool(){const t=newTool.value.trim();if(t&&!editing.value.tool_allow.includes(t))editing.value.tool_allow.push(t);newTool.value=''}
function addDeny(){const t=newDeny.value.trim();if(t&&!editing.value.tool_deny.includes(t))editing.value.tool_deny.push(t);newDeny.value=''}
function addDelegate(){const t=newDelegate.value;if(t&&!editing.value.delegate_targets.includes(t))editing.value.delegate_targets.push(t);newDelegate.value=''}
function setEntry(checked){
 if(!editing.value)return;
 editing.value.isEntry=checked;
}

// ════════════ Raw Editor ════════════
async function openRawEd(nodeId){
 if(!nodeId)return;
 let nodeContent='';
 try{
  const d=await api(`/v1/admin/config/nodes/${nodeId}/raw`);
  nodeContent=d.content;
 }catch(e){return}
 rawEd.value=reactive({nodeId,nodeContent});
}
function closeRawEd(){rawEd.value=null}
async function saveRawEd(){
 const r=rawEd.value;
 if(!r)return;
 try{
  await api(`/v1/admin/config/nodes/${r.nodeId}/raw`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:r.nodeContent})});
 }catch(e){return}
 await loadNodeDefs();
 rebuildTree();
 if(editing.value&&editing.value.id===r.nodeId)await openEd(r.nodeId);
 rawEd.value=null;
 toast('已保存');
}

// ════════════ Save Node ════════════
async function saveNode(){
 const e=editing.value;
 if(!e)return;
 const ta={mode:e.tool_mode};
 if(e.tool_mode==='allowlist'&&e.tool_allow.length)ta.allow=[...e.tool_allow];
 if(e.tool_mode==='all'&&e.tool_deny.length)ta.deny=[...e.tool_deny];
 const sa={mode:e.skill_mode||'all'};
 if(e.skill_mode==='allowlist'&&e.skill_allow.length)sa.allow=[...e.skill_allow];
 let nodeObj={};
 try{
  const raw=await api(`/v1/admin/config/nodes/${e.id}/raw`);
  const parsed=jsyaml.load(raw.content||'')||{};
  if(parsed&&typeof parsed==='object'&&!Array.isArray(parsed))nodeObj=parsed;
 }catch(err){}
 nodeObj.id=e.id;
 nodeObj.type=e.type||'ai';
 nodeObj.name=e.name;
 nodeObj.description=e.description||'';
 if(e.delegate_targets&&e.delegate_targets.length)nodeObj.delegate_targets=[...e.delegate_targets];
 else delete nodeObj.delegate_targets;
 nodeObj.model=e.model||'';
 nodeObj.prompt=e.prompt_text||'';
 nodeObj.tool_access=ta;
 nodeObj.skills=sa;
 delete nodeObj.model_route;
 delete nodeObj.skill_access;
 delete nodeObj.output_mode;
 delete nodeObj.version;
 delete nodeObj.kind;
 const yamlStr=jsyaml.dump(nodeObj,{sortKeys:false,lineWidth:120,quotingType:'"',forceQuotes:false});
 try{
  await api(`/v1/admin/config/nodes/${e.id}/raw`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:yamlStr})});
 }catch(err){return}

 // 如果设为入口，更新 runtime config
 if(e.isEntry&&entryNodeId.value!==e.id){
  await _setEntryNode(e.id);
 }else if(!e.isEntry&&entryNodeId.value===e.id){
  await _setEntryNode('');
 }

 await Promise.all([loadNodeDefs(),loadAllToolNames()]);
 rebuildTree();
 await openEd(e.id);
 toast('节点已保存');
}

async function _setEntryNode(nodeId){
 try{
  const raw=await api('/v1/admin/config/runtime/raw');
  let obj=jsyaml.load(raw.content)||{};
  if(!obj.shell)obj.shell={};
  obj.shell.entry_node_id=nodeId||'bootstrap.shell_orchestrator';
  const ys=jsyaml.dump(obj,{sortKeys:false,lineWidth:120});
  await api('/v1/admin/config/runtime/raw',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ys})});
  entryNodeId.value=nodeId||'bootstrap.shell_orchestrator';
 }catch(e){}
}

// ════════════ Tool Picker (canvas popup) ════════════
const toolPicker=ref(null);
const allToolNames=computed(()=>[...ALL_TOOLS.value]);

function openToolPicker(n){
 const nd=allNodeDefs.value[n.id]||{};
 const ta=nd.tool_access||{};
 const mode=(typeof ta==='string')?ta:(ta.mode||'none');
 const allow=[...(ta.allow||[])];
 const deny=[...(ta.deny||[])];
 toolPicker.value={nodeId:n.id,mode,allow,deny};
}
function tpModeChange(){
 const tp=toolPicker.value;
 if(!tp)return;
 if(tp.mode==='none'){tp.allow=[];tp.deny=[]}
 else if(tp.mode==='all'){tp.allow=[];tp.deny=[]}
 else if(tp.mode==='allowlist'){tp.deny=[];tp.allow=[]}
}
function tpChecked(name){
 const tp=toolPicker.value;
 if(!tp)return false;
 if(tp.mode==='all')return!tp.deny.includes(name);
 if(tp.mode==='allowlist')return tp.allow.includes(name);
 return false;
}
function tpToggle(name,checked){
 const tp=toolPicker.value;
 if(!tp)return;
 if(tp.mode==='all'){
  if(checked)tp.deny=tp.deny.filter(n=>n!==name);
  else if(!tp.deny.includes(name))tp.deny.push(name);
 }else if(tp.mode==='allowlist'){
  if(checked&&!tp.allow.includes(name))tp.allow.push(name);
  else if(!checked)tp.allow=tp.allow.filter(n=>n!==name);
 }
}
async function saveToolPicker(){
 const tp=toolPicker.value;
 if(!tp)return;
 const nid=tp.nodeId;
 try{
  const raw=await api(`/v1/admin/config/nodes/${nid}/raw`);
  let obj=jsyaml.load(raw.content)||{};
  const ta={mode:tp.mode};
  if(tp.mode==='allowlist'&&tp.allow.length)ta.allow=[...tp.allow];
  if(tp.mode==='all'&&tp.deny.length)ta.deny=[...tp.deny];
  obj.tool_access=ta;
  const ys=jsyaml.dump(obj,{sortKeys:false,lineWidth:120});
  await api(`/v1/admin/config/nodes/${nid}/raw`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ys})});
 }catch(e){toast('保存失败',false);return}
 await Promise.all([loadNodeDefs(),loadAllToolNames()]);
 rebuildTree();
 toolPicker.value=null;
 toast('工具配置已保存');
}

// ════════════ Add / Delete Node ════════════
async function addNode(){
 const existing=new Set(Object.keys(allNodeDefs.value||{}));
 let idx=1;
 let nid=`custom.node_${idx}`;
 while(existing.has(nid)){idx+=1;nid=`custom.node_${idx}`}
  const nodeObj={
   id:nid,
   type:'ai',
   name:`新节点 ${idx}`,
   description:'',
   model:'',
   prompt:'你是一个 AI 节点。请根据指令完成处理。',
   tool_access:{mode:'none'},
   skills:{mode:'all'}
  };
 const ys=jsyaml.dump(nodeObj,{sortKeys:false,lineWidth:120});
 try{
  await api('/v1/admin/config/nodes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:nid,content:ys})});
 }catch(e){return}
 await loadNodeDefs();
 rebuildTree();
 nextTick(async()=>{
  fitView();
  await openEd(nid);
 });
 toast('节点已添加');
}

async function delNode(){
 if(!editing.value)return;
 const nid=editing.value.id;
 if(!confirm(`确定删除节点 ${nid}？`))return;
 try{await api(`/v1/admin/config/nodes/${nid}`,{method:'DELETE'})}catch(e){}
 // 从其他节点的 delegate_targets 中移除
 for(const [id,nd] of Object.entries(allNodeDefs.value)){
  if(nd.delegate_targets&&nd.delegate_targets.includes(nid)){
   try{
    const raw=await api(`/v1/admin/config/nodes/${id}/raw`);
    let obj=jsyaml.load(raw.content)||{};
    if(Array.isArray(obj.delegate_targets)){
     obj.delegate_targets=obj.delegate_targets.filter(t=>t!==nid);
     if(!obj.delegate_targets.length)delete obj.delegate_targets;
     const ys=jsyaml.dump(obj,{sortKeys:false,lineWidth:120});
     await api(`/v1/admin/config/nodes/${id}/raw`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ys})});
    }
   }catch(e){}
  }
 }
 if(entryNodeId.value===nid)await _setEntryNode('');
 await loadNodeDefs();
 editing.value=null;selUid.value=null;rawEd.value=null;rebuildTree();toast('节点已删除');
}

function selectNode(id){
 const item=treeItems.value.find(it=>it.kind==='ai'&&it.id===id);
 if(item){
  selUid.value=item.uid;
  openEd(id);
 }
}

// ════════════ Dashboard ════════════
const health=ref(null),adm=ref(null),appCfg=ref(null);
async function loadDash(){try{health.value=await api('/v1/health');adm.value=await api('/v1/admin/state');appCfg.value=await api('/v1/config')}catch(e){}}
function fmtUp(s){if(!s)return'';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}h${m}m`:`${m}m`}
async function restart(){if(!confirm('确定重启引擎？'))return;await api('/v1/admin/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:'engine',reason:'Admin UI'})});toast('重启已下发');setTimeout(loadDash,2000)}

// ════════════ Config ════════════
const ed=ref(null);
const cfgTabs=[{k:'runtime',a:'runtime',l:'运行时'},{k:'policy',a:'policy',l:'安全策略'},{k:'schedules',a:'schedules',l:'定时调度'}];
const csub=ref('');
const cfgLbl=computed(()=>(cfgTabs.find(c=>c.k===csub.value)||{}).l||'');
async function cfgLd(a,k){csub.value=k;const d=await api(`/v1/admin/config/${a}/raw`);ed.value={type:'cfg',a,content:d.content}}
async function cfgSv(){await api(`/v1/admin/config/${ed.value.a}/raw`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ed.value.content})});toast('已保存')}

// ════════════ Tools & MCP ════════════
const toolsList=ref([]),mcpList=ref([]),toolEd=ref(null),mcpEd=ref(null);
const reloading=ref(false);
async function loadTools(){
 try{toolsList.value=await api('/v1/admin/config/tools');await loadAllToolNames()}catch(e){}
 try{mcpList.value=await api('/v1/admin/config/mcp-clients')}catch(e){}
}
async function openToolEd(name){
 try{
  const d=await api(`/v1/admin/config/tools/${name}/raw`);
  toolEd.value={name,content:d.content,isNew:false};
 }catch(e){}
}
function newToolEd(){
 const name=prompt('工具名称（如 my_tool）：');
 if(!name)return;
 toolEd.value={name:name.trim(),content:`SPEC = {\n    "name": "${name.trim()}",\n    "description": "",\n    "input_schema": {"type": "object", "properties": {}, "required": []}\n}\n\nimport json, sys\n\ndef main():\n    args = json.loads(sys.stdin.read())\n    result = {"ok": True}\n    print(json.dumps(result))\n\nif __name__ == "__main__":\n    main()\n`,isNew:true};
}
async function saveToolEd(){
 const e=toolEd.value;if(!e)return;
 const m=e.isNew?'POST':'PUT';
 const u=e.isNew?'/v1/admin/config/tools':(`/v1/admin/config/tools/${e.name}/raw`);
 const body=e.isNew?JSON.stringify({id:e.name,content:e.content}):JSON.stringify({content:e.content});
 try{await api(u,{method:m,headers:{'Content-Type':'application/json'},body})}catch(err){return}
 toolEd.value=null;await loadTools();try{await api('/v1/tools/reload',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}catch(e){}toast('工具已保存，引擎将自动重载');
}
async function delTool(name){
 if(!confirm(`确定删除工具 ${name}？`))return;
 try{await api(`/v1/admin/config/tools/${name}`,{method:'DELETE'})}catch(e){}
 toolEd.value=null;await loadTools();try{await api('/v1/tools/reload',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}catch(e){}toast('工具已删除，引擎将自动重载');
}
async function openMcpEd(){
 try{
  const d=await api('/v1/admin/config/mcp-clients/raw');
  mcpEd.value={content:d.content};
 }catch(e){}
}

async function reloadTools(){
 reloading.value=true;
 try{await api('/v1/tools/reload',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});toast('重载信号已发送')}catch(e){toast('发送失败',false)}
 setTimeout(async()=>{await loadTools();reloading.value=false},2000);
}

async function saveMcpEd(){
 if(!mcpEd.value)return;
 try{await api('/v1/admin/config/mcp-clients/raw',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:mcpEd.value.content})})}catch(e){return}
 mcpEd.value=null;await loadTools();toast('MCP 配置已保存');
}

// ════════════ Skills ════════════
const skillsList=ref([]),skillEd=ref(null);
async function loadSkills(){
 try{skillsList.value=await api('/v1/admin/config/skills')}catch(e){}
}
async function openSkillEd(name){
 try{
  const d=await api(`/v1/admin/config/skills/${name}/raw`);
  skillEd.value={name,content:d.content,isNew:false};
 }catch(e){}
}
function newSkillEd(){
 const name=prompt('技能名称（如 my_skill）：');
 if(!name)return;
 skillEd.value={name:name.trim(),content:`---\nname: ${name.trim()}\ndescription: ""\nenabled: true\nstrategy: normal\nkeywords: []\n---\n\n技能内容\n`,isNew:true};
}
async function saveSkillEd(){
 const e=skillEd.value;if(!e)return;
 const m=e.isNew?'POST':'PUT';
 const u=e.isNew?'/v1/admin/config/skills':(`/v1/admin/config/skills/${e.name}/raw`);
 const body=e.isNew?JSON.stringify({id:e.name,content:e.content}):JSON.stringify({content:e.content});
 try{await api(u,{method:m,headers:{'Content-Type':'application/json'},body})}catch(err){return}
 skillEd.value=null;await loadSkills();toast('技能已保存');
}
async function delSkill(name){
 if(!confirm(`确定删除技能 ${name}？`))return;
 try{await api(`/v1/admin/config/skills/${name}`,{method:'DELETE'})}catch(e){}
 skillEd.value=null;await loadSkills();toast('技能已删除');
}

// ════════════ Keyboard ════════════
function onKey(e){
 if(!authed.value)return;
 if(e.key==='Escape'){
  if(rawEd.value)closeRawEd();
  else if(editing.value)closeEd();
  else selUid.value=null;
 }
}

// ════════════ Init ════════════
async function initLoad(){
 await Promise.all([loadNodeDefs(),loadEntryNode(),loadAllToolNames()]);
 rebuildTree();
 nextTick(()=>fitView());
}
async function loadAllToolNames(){
 try{
  let names=[];
  try{
   names=await api('/v1/admin/config/all-tool-names');
  }catch(e1){
   try{
    const ext=await api('/v1/admin/config/tools');
    names=ext.filter(t=>t.name).map(t=>t.name);
   }catch(e2){}
  }
  if(Array.isArray(names)&&names.length)ALL_TOOLS.value=[...new Set(names)];
 }catch(e){}
}

onMounted(()=>{
 const s=localStorage.getItem('ctk');
 if(s){ltk.value=s;login()}
 document.addEventListener('keydown',onKey);
});
watch(vpRef,(el,old)=>{
 if(old)old.removeEventListener('wheel',nativeWheel);
 if(el)el.addEventListener('wheel',nativeWheel,{passive:false});
});
function nativeWheel(e){
 if(e.target.closest('.rpanel')||e.target.closest('.rpbg')||e.target.closest('.raw-mask')||e.target.closest('.raw-box')||e.target.closest('.tool-picker'))return;
 e.preventDefault();
 cvWheel(e);
}

return{authed,ltk,lerr,login,logout,tshow,tmsg,ttyp,navTabs,view,goView,nodeList,entryNodeId,allNodeDefs,treeItems,treeEdges,aiItems,toolItems,panX,panY,zoom,zoomPct,selUid,dragUid,isPanning,vpRef,editing,rawEd,conns,shortRoute,shortTool,nodeGlyph,cvDown,cvMove,cvUp,cvWheel,ndDown,toolDown,wireStart,zoomIn,zoomOut,fitView,otherNodes,openEd,closeEd,jumpEd,openRawEd,closeRawEd,saveRawEd,saveNode,delNode,newTool,newDeny,newDelegate,addTool,addDeny,addDelegate,setEntry,addNode,selectNode,health,adm,appCfg,loadDash,fmtUp,restart,ed,cfgTabs,csub,cfgLbl,cfgLd,cfgSv,wireFrom,wireEndX,wireEndY,wireSnap,dragSnap,toolsList,mcpList,toolEd,mcpEd,reloading,loadTools,openToolEd,newToolEd,saveToolEd,delTool,openMcpEd,saveMcpEd,reloadTools,skillsList,skillEd,loadSkills,openSkillEd,newSkillEd,saveSkillEd,delSkill,toolPicker,allToolNames,openToolPicker,tpModeChange,tpChecked,tpToggle,saveToolPicker}
}}).mount('#app');
