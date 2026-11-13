const studio={sample:'',day:'',category:'',store:'H800',pickedGroup:'',evalSet:'',evalPath:'',trainingJobs:new Set(),evaluations:[],history:[],issues:[],ready:false};
let studioUpdated=0,studioRequest=null;
const colors=['#358cff','#19b99b','#aa84f4','#8492a8'];
const kinds=['训练','评测','测试','未分类'];
const chartTime=t=>new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
const shortName=p=>p.split('/').filter(Boolean).pop()||p;
function treeLabel(path){
 const name=groupLabel({path,name:shortName(path)});
 const parts=name.replace(/^train_/,'').split('_');
 return parts.length>5?parts.slice(0,2).join(' ')+' · '+parts.slice(-2).join(' '):name;
}
const chartEmpty=t=>'<p class="studio-empty">'+esc(t)+'</p>';
function control(key,val,label,active){return button(key,val,esc(label),'studio-pick','aria-pressed="'+active+'"')}
function chipLegend(color,label){return '<span><i style="background:'+color+'"></i>'+esc(label)+'</span>'}
function studioPanel(title,subtitle,tools,body,wide=false){
 return '<section class="surface studio-card'+(wide?' studio-wide':'')+'"><div class="studio-heading"><div><h2>'+esc(title)+'</h2><small>'+esc(subtitle)+'</small></div>'+tools+'</div>'+body+'</section>';
}
function taskType(j){
 const n=j.name.toLowerCase();
 if(/eval|benchmark|评测/.test(n))return 1;
 if(/test|probe|smoke|debug|preflight|测试/.test(n))return 2;
 if(/train|finetune|sft|(?:^|[-_])rlt|训练/.test(n)||studio.trainingJobs.has(j.id))return 0;
 return 3;
}
function gantt(){
 const active=j=>['RUNNING','PENDING','CONFIGURING','COMPLETING'].includes(j.state);
 const ordered=jobList.filter(j=>j.submitTs).sort((a,b)=>Number(active(b))-Number(active(a))||b.submitTs-a.submitTs).slice(0,6);
 if(!ordered.length)return chartEmpty('暂无带提交时间的作业记录');
 const now=overview.stamp;
 const end=j=>active(j)?now:(j.endTs||j.startTs||j.submitTs);
 const predicted=j=>j.state==='PENDING'&&j.estimateTs>now?j.estimateTs:null;
 const lo=Math.min(...ordered.map(j=>j.submitTs)),hi=Math.max(lo+60,...ordered.map(j=>Math.max(end(j),predicted(j)||0)));
 function mark(a,b,cls){if(a==null||b==null||b<a)return '';return '<i class="gantt-mark '+cls+'" style="left:'+100*(a-lo)/(hi-lo)+'%;width:'+100*(b-a)/(hi-lo)+'%"></i>'}
 const rows=ordered.map(j=>{
  const future=predicted(j),started=j.state!=='PENDING'&&j.startTs&&j.startTs<=now?j.startTs:null;
  const title=j.name+' · '+(states[j.state]||j.state)+' · 提交 '+chartTime(j.submitTs)+(future?' · 预计开始 '+chartTime(future):'');
  return '<button class="gantt-row" data-job="'+attr(j.id)+'" title="'+attr(title)+'"><span class="gantt-label"><b>'+esc(j.name)+'</b><small>#'+esc(j.id)+'</small></span><span class="gantt-lane">'+mark(j.submitTs,started||end(j),'wait')+(started?mark(started,end(j),j.state==='FAILED'?'fail':'run'):'')+(future?mark(now,future,'estimate'):'')+'</span></button>';
 }).join('');
 return rows+'<div class="gantt-scale"><span>'+chartTime(lo)+'</span><span class="gantt-mid">'+chartTime((lo+hi)/2)+'</span><span>'+chartTime(hi)+'</span></div><div class="studio-legend">'+chipLegend('var(--amber)','已等待')+chipLegend(colors[0],'已运行')+chipLegend('var(--fail)','失败作业运行段')+'<span>┄ 至预计启动</span></div>';
}
function heatmap(){
 const history=studio.history, latest=history.at(-1);if(!latest)return chartEmpty('等待首个 Slurm 节点样本');
 const anchor=Math.floor(overview.stamp/3600)*3600,slots=Array.from({length:12},(_,i)=>anchor-(11-i)*3600),buckets=new Map();
 history.forEach(s=>buckets.set(Math.floor(s.t/3600)*3600,s));
 const names=[...new Set(history.flatMap(s=>Object.keys(s.nodes)))].sort();
 let html='<div class="heat-rows"><div class="heat-row"><span></span>'+slots.map((t,i)=>'<small>'+((i%3===0||i===11)?new Date(t*1000).getHours()+'时':'')+'</small>').join('')+'</div>';
 names.forEach(name=>{html+='<div class="heat-row"><small>'+esc(name.replace(/^H800-/,''))+'</small>'+slots.map(t=>{
  const sample=buckets.get(t),n=sample?.nodes[name];if(!n)return '<span class="heat-missing" title="尚无样本">·</span>';
  const title=name+' · '+chartTime(sample.t)+' · 空闲 '+numeric(n.free,0)+' 张 · '+n.state;
  return '<button class="heat-cell '+(n.blocked?'blocked':'')+'" style="--intensity:'+Math.round(15+75*(n.free||0)/(n.total||1))+'%" data-heat="'+attr(t+'|'+name)+'" title="'+attr(title)+'" aria-label="'+attr(title)+'">'+(n.free??'—')+'</button>';
 }).join('')+'</div>'});
 const [hour,name]=studio.sample.split('|'),sample=buckets.get(+hour),n=sample?.nodes[name];
 return html+'</div><div class="studio-legend"><span>数字为空闲卡数</span><span>▧ 不接收新作业</span><span>· 未采样</span></div>'+(n?'<div class="chart-detail">'+esc(name)+' · '+chartTime(sample.t)+' · '+esc(n.state)+'<br>占用作业 '+esc(n.jobs.join('、')||'无')+'</div>':'');
}
function weekData(){
 const today=new Date(overview.stamp*1000);today.setHours(0,0,0,0);
 const days=Array.from({length:7},(_,i)=>{const a=new Date(today);a.setDate(a.getDate()+i-6);const b=new Date(a);b.setDate(b.getDate()+1);return {a:+a/1000,b:+b/1000,label:(a.getMonth()+1)+'/'+a.getDate(),values:[0,0,0,0],jobs:[[],[],[],[]]}});
 for(const j of jobList){
  if(!j.startTs||j.state==='PENDING')continue;
  const n=Number((j.allocation||'').match(/(?:^|,)gres\/gpu=(\d+)/)?.[1]);if(!n)continue;
  const end=j.endTs||(['RUNNING','COMPLETING','SUSPENDED'].includes(j.state)?overview.stamp:0),k=taskType(j);
  for(const d of days){const seconds=Math.min(d.b,end,overview.stamp)-Math.max(d.a,j.startTs);if(seconds>0){d.values[k]+=n*seconds/3600;d.jobs[k].push(j)}}
 }
 return days;
}
function gpuHours(){
 const days=weekData(),max=Math.max(.01,...days.map(d=>d.values.reduce((a,b)=>a+b,0)));
 let html='<div class="hours-plot"><div class="axis-y"><span>'+max.toFixed(1)+'</span><span>0</span></div><div class="week-bars">'+days.map((d,i)=>'<div class="week-col"><div class="week-stack">'+d.values.map((v,k)=>v?'<button data-gpuh="'+i+'|'+k+'" title="'+attr(d.label+' · '+kinds[k]+' '+v.toFixed(2)+' GPU·小时')+'" aria-label="'+attr(d.label+kinds[k]+v.toFixed(2)+' GPU·小时')+'" style="background:'+colors[k]+';height:'+(100*v/max)+'%"></button>':'').join('')+'</div><small>'+d.label+'</small></div>').join('')+'</div></div>';
 html+='<div class="studio-legend">'+kinds.map((k,i)=>chipLegend(colors[i],k)).join('')+'</div>';
 const d=days.find(v=>v.label===studio.day);if(d){html+='<div class="chart-detail">'+esc(d.label+' · '+kinds[studio.category])+' · '+d.values[studio.category].toFixed(2)+' GPU·小时 '+d.jobs[studio.category].map(j=>button('job',j.id,'#'+esc(j.id),'link')).join('')+'</div>'}
 return html;
}
function storageMap(){
 const items=ckSources?.items||[],groups=new Map();let unknown=0;
 for(const c of items){
  const sizes=studio.store==='NAS'?c.copies.map(x=>x.bytes):c.present?[c.bytes]:[];
  for(const size of sizes){if(size==null){unknown++;continue}if(size>0)groups.set(c.group,(groups.get(c.group)||0)+size)}
 }
 const data=[...groups].map(([name,value])=>({name,value})).sort((a,b)=>b.value-a.value);
 const sum=rows=>rows.reduce((n,r)=>n+r.value,0),total=sum(data);
 const leaves=data.slice(0,7);if(data.length>7)leaves.push({name:'其他 '+(data.length-7)+' 个任务',value:sum(data.slice(7)),other:true});
 function tile(rows,x,y,w,h){
  if(rows.length===1){const r=rows[0],idx=data.findIndex(d=>d.name===r.name);return '<button class="tree-tile" '+(r.other?'data-treeother="1"':'data-tree="'+attr(r.name)+'"')+' title="'+attr(r.name+' · '+fmt(r.value))+'" style="left:'+x+'%;top:'+y+'%;width:'+w+'%;height:'+h+'%;--shade:'+(r.other?15:63-idx*6)+'%"><b>'+esc(treeLabel(r.name))+'</b><small>'+fmt(r.value)+'</small></button>'}
  const size=sum(rows);let split=1;while(split<rows.length-1&&Math.abs(sum(rows.slice(0,split+1))-size/2)<Math.abs(sum(rows.slice(0,split))-size/2))split++;
  const a=rows.slice(0,split),b=rows.slice(split),f=sum(a)/size;
  return w*2>h?tile(a,x,y,w*f,h)+tile(b,x+w*f,y,w*(1-f),h):tile(a,x,y,w,h*f)+tile(b,x,y+h*f,w,h*(1-f));
 }
 const selected=groups.get(studio.pickedGroup);
 return '<div class="storage-total">'+fmt(total)+' <small>已盘点 · '+groups.size+' 个任务'+(unknown?' · '+unknown+' 项大小未知':'')+'</small></div>'+(leaves.length?'<div class="tree-map">'+tile(leaves,0,0,100,100)+'</div>':chartEmpty(ckSources?.note||'没有已盘点的副本'))+(selected?'<div class="chart-detail"><code>'+esc(studio.pickedGroup)+'</code>'+button('treeopen',studio.pickedGroup,'查看 checkpoints →','link')+'</div>':'');
}
function resultPanel(){
 const list=studio.evaluations,sets=[...new Set(list.map(v=>v.dataset))],dataset=sets.includes(studio.evalSet)?studio.evalSet:sets[0];
 if(!sets.length){const rows=(ckSources?.items||[]).filter(c=>c.present).sort((a,b)=>b.localUpdated-a.localUpdated).slice(0,4);
 return studioPanel('最近保存的 checkpoint','按本地保存时间排序','',rows.length?rows.map(c=>'<div class="latest-save">'+button('treeopen',c.group,esc(shortName(c.group)),'name')+'<small>'+esc(c.name)+' · '+chartTime(c.localUpdated)+(c.copies.some(x=>x.verified)?' · NAS 已校验':'')+'</small></div>').join(''):chartEmpty('暂无已登记的本地权重'))}
 const rows=list.filter(v=>v.dataset===dataset),max=Math.max(1,...rows.map(v=>v.step));
 const points=rows.map(v=>'<button class="save-dot" style="left:'+(2+96*v.step/max)+'%;top:'+(95-90*v.success/v.total)+'%;--dot:'+colors[1]+'" data-evalpath="'+attr(v.path)+'" title="'+attr(shortName(v.path)+' · '+v.success+'/'+v.total)+'"></button>').join('');
 const v=rows.find(v=>v.path===studio.evalPath)||rows[0];
 return studioPanel('Checkpoint 评测','同一评测集 · 机器人任务成功率','<select aria-label="评测集" id="eval-set">'+sets.map(s=>'<option '+(s===dataset?'selected':'')+'>'+esc(s)+'</option>').join('')+'</select>','<div class="curve-window"><div class="axis-y"><span>100%</span><span>0%</span></div><div class="curve-body eval-plot">'+points+'</div></div><div class="curve-scale"><span>0</span><span>step</span><span>'+max+'</span></div><div class="chart-detail">'+esc(shortName(v.path))+' · 成功 '+v.success+' / '+v.total+' 次 '+button('treeopen',v.group,'查看权重','link')+'</div>');
}
function dashboardOverview(){
 const jobs=jobsAvailable?jobList:null;
 const count=groupList?.reduce((total,g)=>total+g.count,0);
 const stats=[
  ['作业记录',jobs?.length,'全部 Slurm 作业','all'],
  ['运行中',jobs?.filter(j=>j.state==='RUNNING').length,'正在执行','RUNNING'],
  ['排队中',jobs?.filter(j=>j.state==='PENDING').length,'等待调度','PENDING'],
  ['Checkpoint',count,'H800 本地权重','ck']
 ];
 const cards=stats.map(([label,value,hint,target])=>button('metric',target,'<span class="metric-label"><i aria-hidden="true"></i>'+label+'</span><strong>'+(value??'—')+'</strong><small>'+hint+'</small>','stat','aria-label="查看'+label+'"')).join('');
 const volumes=overview.mounts.map(m=>{
  const personal=m.name==='home';
  const percent=m.free!=null&&m.total>0&&(!personal||m.scope==='personal')?Math.min(100,Math.max(0,m.free/m.total*100)):null;
  return '<div class="volume"><code>/'+esc(m.name)+'</code><strong>'+(percent===null?'待统计':fmt(m.free))+'</strong><div class="track"><div class="fill '+(percent!==null&&percent<7?'hot':'')+'" style="width:'+(percent??0)+'%"></div></div><small>'+((percent===null?'':'剩余 '+Number(percent.toFixed(1))+'% · ')+(personal?'个人配额 30 GiB':'共享卷 '+fmt(m.total)))+'</small></div>';
 }).join('');
 return '<div class="summary" aria-label="我的作业与权重">'+cards+'</div><section class="surface volume-panel"><div class="line"><h2>存储余量</h2><small>个人配额与共享卷余量</small></div><div class="volume-grid">'+(volumes||chartEmpty('暂未读取到存储卷容量'))+'</div></section>';
}
function dashboard(){
 return dashboardOverview()+(studio.issues.length?'<p class="feedback">'+esc(studio.issues.join(' · '))+'</p>':'')+'<div class="studio-board">'+
 studioPanel('我的作业时间线','最近 6 个作业 · 活跃作业优先',button('page','2','训练记录 →','link'),gantt(),true)+
 studioPanel('节点空闲窗口','近 12 小时 · 每小时最后一次采样','',heatmap())+
 studioPanel('我的 GPU 时间','近 7 天 · 已分配 GPU 卡时','',gpuHours())+
 studioPanel('权重占用分布','各训练任务的存储占比','<div class="studio-switch">'+['H800','NAS'].map(s=>control('treestore',s,s,studio.store===s)).join('')+'</div>',storageMap())+
 resultPanel()+'</div><section class="surface below"><div class="line"><h2>最近作业</h2>'+button('page','2','全部作业 →','link')+'</div>'+jobTable(jobList.slice(0,5))+'</section>';
}
async function loadStudio(){
 if(studio.ready&&Date.now()-studioUpdated<50000&&ckSources)return;
 if(studioRequest)return studioRequest;
 studioRequest=(async()=>{
  const names=['节点历史','训练摘要','权重清单'];
  const results=await Promise.allSettled([api('node-history'),api('training-summary'),ckSources||api('checkpoint-sources')]);
  studio.issues=results.flatMap((r,i)=>r.status==='rejected'?[names[i]+'暂时无法读取']:[]);
  if(results[0].status==='fulfilled')studio.history=results[0].value;
  if(results[1].status==='fulfilled'){studio.trainingJobs=new Set(results[1].value.jobs);studio.evaluations=results[1].value.evaluations}
  if(results[2].status==='fulfilled')ckSources=results[2].value;
  studioUpdated=Date.now();studio.ready=true;
 })().finally(()=>{studioRequest=null});return studioRequest;
}
document.addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b||page!==0)return;const d=b.dataset;
 if(d.metric==='ck'){ckStore='H800';setResource('ck');return}
 else if(d.metric){selectedState=d.metric==='all'?'':d.metric;selectedJob='';navigate(2);return}
 else if(d.heat)studio.sample=d.heat;
 else if(d.gpuh){const [i,k]=d.gpuh.split('|').map(Number);studio.day=weekData()[i].label;studio.category=k}
 else if(d.treestore){studio.store=d.treestore;studio.pickedGroup=''}
 else if(d.tree)studio.pickedGroup=d.tree;
 else if(d.treeopen||d.treeother){ckStore=d.treeother?studio.store:'all';setResource('ck',d.treeopen||'');return}
 else if(d.evalpath)studio.evalPath=d.evalpath;
 else return;
 render();
});
document.addEventListener('change',e=>{
 if(e.target.id==='eval-set'){studio.evalSet=e.target.value;studio.evalPath=''}
 else return;render();
});
