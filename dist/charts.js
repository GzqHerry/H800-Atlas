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
const usageView={days:7, metric:'gpu', all:false, cache:new Map(), pending:new Set(), errors:new Set()};
const usageMetrics={gpu:'GPU 卡时', time:'训练时长', jobs:'训练次数', success:'成功率'};
const usageNumber=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1});

function usageValue(row,metric){
 switch(metric){
  case 'gpu':return row.gpuSeconds/3600;
  case 'time':return row.seconds/3600;
  case 'jobs':return row.jobs;
  case 'success':return row.ended?100*row.completed/row.ended:null;
 }
}
function usageFormat(value,metric){
 if(value==null)return '—';
 const n=value>0&&value<0.1?'＜0.1':usageNumber.format(value);
 return n+({gpu:' 卡时',time:' 小时',jobs:' 次',success:'%'}[metric]);
}
function usageRanking(data){
 const {metric,all}=usageView, me=overview.user;
 const sorted=data.users.map(r=>({...r,value:usageValue(r,metric)})).sort((a,b)=>
  (b.value??-1)-(a.value??-1)||b.ended-a.ended||a.user.localeCompare(b.user));
 sorted.forEach((r,i)=>{r.rank=r.value==null?'—':i&&r.value===sorted[i-1].value?sorted[i-1].rank:i+1});
 const visible=all?sorted:sorted.slice(0,5),mine=sorted.find(r=>r.user===me);
 if(!all&&mine&&!visible.includes(mine))visible.push(mine);
 const scale=metric==='success'?100:sorted[0]?.value||1;
 const rows=visible.map(r=>{
  const personal=r.user===me, value=usageFormat(r.value,metric);
  return '<li class="usage-row'+(personal?' usage-mine':'')+'"><span class="usage-rank">'+r.rank+'</span><span class="usage-name" title="'+attr(r.user)+'">'+personLabel(r.user)+'</span>'+
   '<span class="usage-track" aria-hidden="true"><i style="width:'+100*(r.value||0)/scale+'%"></i></span>'+
   '<span class="usage-value">'+value+(metric==='success'?'<small>'+r.completed+' / '+r.ended+' 已结束</small>':'')+'</span></li>';
 }).join('');
 const total=data.users.reduce((t,r)=>({gpu:t.gpu+r.gpuSeconds/3600,jobs:t.jobs+r.jobs,ok:t.ok+r.completed,ended:t.ended+r.ended}),{gpu:0,jobs:0,ok:0,ended:0});
 const success=total.ended?100*total.ok/total.ended:null;
 const tabs=Object.entries(usageMetrics).map(([key,label])=>control('usagemetric',key,label,metric===key)).join('');
 return '<div class="usage-layout"><div class="usage-summary"><small>累计 GPU 卡时</small><strong>'+usageNumber.format(total.gpu)+'</strong>'+
  '<span>'+sorted.length+' 位用户 · '+usageNumber.format(total.jobs)+' 次训练</span><div class="usage-finish"><i aria-hidden="true" style="--share:'+(success||0)+'%"></i><div>作业成功率 <b>'+usageFormat(success,'success')+'</b><small>'+total.ok+' 成功 / '+total.ended+' 已结束</small></div></div></div>'+
  '<div class="usage-plot"><div class="studio-switch" role="group" aria-label="排行指标">'+tabs+'</div>'+
  (rows?'<ol class="usage-list" aria-label="'+usageMetrics[metric]+'排行">'+rows+'</ol>':chartEmpty('此时段没有可识别的训练作业'))+
  (sorted.length>5?button('usageall','1',all?'收起排行':'查看全部 '+sorted.length+' 位用户','link','aria-expanded="'+all+'"'):'')+'</div></div>'+
  '<p class="usage-note">按作业名或提交脚本识别训练。时长只计所选时段内的运行时间，卡时按分配卡数累计。成功率按已结束作业计算，取消和超时计入分母。</p>';
}
function usageCard(){
 const {days,cache,errors}=usageView, data=cache.get(days), failed=errors.has(days);
 const period=[7,30].map(n=>control('usagedays',n,'近 '+n+' 天',days===n)).join('');
 const hint=failed?'暂时无法更新':data?'采样于 '+chartTime(data.stamp):'读取 Slurm 记账数据中…';
 return '<div class="studio-heading"><div class="usage-title"><i class="usage-icon" aria-hidden="true"><i></i><i></i><i></i></i><div><h2>集群训练用量</h2><small>当前账号可见的用户 · '+hint+'</small></div></div><div class="studio-switch" role="group" aria-label="统计周期">'+period+'</div></div>'+
  (data?usageRanking(data):chartEmpty(failed?'Slurm 记账数据暂时不可用。':'正在汇总训练记录…'))+
  (failed?button('usageretry','1','重新读取','link'):'');
}
function refreshUsageCard(){
 const panel=$('trainingUsage');if(page!==0||!panel)return;
 const active=document.activeElement;
 const key=panel.contains(active)?Object.entries(active.dataset).find(([k])=>k.startsWith('usage')):null;
 panel.innerHTML=usageCard();
 if(key)panel.querySelector('[data-'+key[0]+'="'+key[1]+'"]')?.focus({preventScroll:true});
}
async function loadUsage(){
 const {days,cache,pending,errors}=usageView;
 if(pending.has(days)||Date.now()/1000-(cache.get(days)?.stamp||0)<300&&!errors.has(days))return;
 pending.add(days);
 try{cache.set(days,await api('training-usage',{days}));errors.delete(days)}
 catch{errors.add(days)}
 finally{pending.delete(days);if(days===usageView.days)refreshUsageCard()}
}
function dashboard(){
 loadUsage();
 return dashboardOverview()+(studio.issues.length?'<p class="feedback">'+esc(studio.issues.join(' · '))+'</p>':'')+'<div class="studio-board">'+
 studioPanel('我的作业时间线','最近 6 个作业 · 活跃作业优先',button('page','2','训练记录 →','link'),gantt(),true)+
 studioPanel('节点空闲窗口','近 12 小时 · 每小时最后一次采样','',heatmap())+
 studioPanel('我的 GPU 时间','近 7 天 · 已分配 GPU 卡时','',gpuHours())+
 studioPanel('权重占用分布','各训练任务的存储占比','<div class="studio-switch">'+['H800','NAS'].map(s=>control('treestore',s,s,studio.store===s)).join('')+'</div>',storageMap())+
 resultPanel()+'</div><section id="trainingUsage" class="surface studio-card below" aria-label="集群训练用量">'+usageCard()+'</section>';
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
 if(d.usagedays||d.usagemetric||d.usageall||d.usageretry){
  if(d.usagedays){usageView.days=Number(d.usagedays);usageView.all=false}
  if(d.usagemetric)usageView.metric=d.usagemetric;
  if(d.usageall)usageView.all=!usageView.all;
  refreshUsageCard();if(d.usagedays||d.usageretry)loadUsage();return;
 }
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
