const studio={sample:'',day:'',category:'',store:'H800',pickedGroup:'',evalSet:'',evalPath:'',trainingJobs:new Set(),evaluations:[],history:[],issues:[],ready:false};
let studioUpdated=0,studioRequest=null;
const colors=['#438dfa','#28b79a','#ac89ed','#74c9ef','#e7aa65'];
const kinds=['训练','评测','测试','推理','用途待识别'];
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
function labelType(label){
 const n=label.toLowerCase();
 if(/eval|benchmark|评测/.test(n))return 1;
 if(/test|probe|smoke|debug|preflight|测试/.test(n))return 2;
 if(/infer(?:ence)?|serving|vllm|sglang|推理/.test(n))return 3;
 if(/train|finetune|sft|(?:^|[-_])rlt|训练/.test(n))return 0;
 return 4;
}
function taskType(j){
 const named=labelType(j.name);
 if(named!==4)return named;
 if(j.trainingPhase||studio.trainingJobs.has(j.id))return 0;
 const labels=j.launchLabels;
 if(!labels)return 4;
 const script=entryType(labels.script||'');
 if(script!==4)return script;
 const types=new Set((labels.programs||[]).map(entryType).filter(type=>type!==4));
 return types.size===1?[...types][0]:4;
}
function entryType(name){
 // Match task words in entry points, e.g. direct_packet_train.py, without treating "latest" as a test.
 const tokens=name.toLowerCase().split(/[._+-]+/);
 for(const [i,pattern] of [[1,/^(?:eval|benchmark|评测)/],[2,/^(?:test|pytest|unittest|probe|smoke|debug|preflight|测试)/],
                          [3,/^(?:infer|serving|vllm|sglang|推理)/],[0,/^(?:train|pretrain|finetun|sft|rlt|训练)/]]){
  if(tokens.some(t=>pattern.test(t)))return i;
 }
 return 4;
}
let timelinePage=0;
function gantt(){
 const active=j=>['RUNNING','PENDING','CONFIGURING','COMPLETING','SUSPENDED'].includes(j.state);
 const recent=j=>j.endTs||j.startTs||j.submitTs;
 const jobs=jobList.filter(j=>j.submitTs).sort((a,b)=>Number(active(b))-Number(active(a))||
  (active(a)?b.submitTs-a.submitTs:recent(b)-recent(a))||b.submitTs-a.submitTs);
 const pageSize=6;
 timelinePage=Math.max(0,Math.min(timelinePage,Math.ceil(jobs.length/pageSize)-1));
 const ordered=jobs.slice(timelinePage*pageSize,(timelinePage+1)*pageSize);
 if(!ordered.length)return chartEmpty('暂无带提交时间的作业记录');
 const now=overview.stamp;
 const end=j=>active(j)?now:(j.endTs||j.startTs||j.submitTs);
 const predicted=j=>j.state==='PENDING'&&j.estimateTs>now?j.estimateTs:null;
 const lo=Math.min(...ordered.map(j=>j.submitTs)),hi=Math.max(lo+60,...ordered.map(j=>Math.max(end(j),predicted(j)||0)));
 function mark(a,b,cls){if(a==null||b==null||b<a)return '';return '<i class="gantt-mark '+cls+'" style="left:'+100*(a-lo)/(hi-lo)+'%;width:'+100*(b-a)/(hi-lo)+'%"></i>'}
 const rows=ordered.map(j=>{
  const future=predicted(j),started=j.state!=='PENDING'&&j.startTs&&j.startTs<=now?j.startTs:null;
  const state=states[slurmState(j)]||j.state;
  const title=j.name+' · Slurm '+state+' · 提交 '+chartTime(j.submitTs)+(started?' · 开始 '+chartTime(started):'')+(!active(j)&&j.endTs?' · 结束 '+chartTime(j.endTs):'')+(future?' · 预计开始 '+chartTime(future):'');
  return '<button class="gantt-row" data-job="'+attr(j.id)+'" title="'+attr(title)+'"><span class="gantt-label"><b>'+esc(j.name)+'</b><span class="gantt-meta"><small>#'+esc(j.id)+'</small>'+runListBadge(j)+'</span></span><span class="gantt-lane">'+mark(j.submitTs,started||end(j),'wait')+(started?mark(started,end(j),j.state==='FAILED'?'fail':'run'):'')+(future?mark(now,future,'estimate'):'')+'</span></button>';
 }).join('');
 return rows+'<div class="gantt-scale"><span>'+chartTime(lo)+'</span><span class="gantt-mid">'+chartTime((lo+hi)/2)+'</span><span>'+chartTime(hi)+'</span></div><div class="studio-legend">'+chipLegend('var(--amber)','已等待')+chipLegend(colors[0],'已运行')+chipLegend('var(--fail)','失败作业运行段')+'<span>┄ 至预计启动</span></div>'+pageControls('timelinepage',timelinePage,jobs.length,pageSize);
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
 const days=Array.from({length:7},(_,i)=>{const a=new Date(today);a.setDate(a.getDate()+i-6);const b=new Date(a);b.setDate(b.getDate()+1);return {a:+a/1000,b:+b/1000,label:(a.getMonth()+1)+'/'+a.getDate(),values:kinds.map(()=>0),jobs:kinds.map(()=>[])}});
 for(const j of jobList){
  if(!j.startTs||j.state==='PENDING')continue;
  const allocation=j.allocation||'';
  const total=allocation.match(/(?:^|,)gres\/gpu=(\d+)(?:,|$)/);
  const n=total?Number(total[1]):Array.from(allocation.matchAll(/(?:^|,)gres\/gpu:[^,=]+=(\d+)(?=,|$)/g),m=>Number(m[1])).reduce((sum,cards)=>sum+cards,0);
  if(!n)continue;
  const end=j.endTs||(['RUNNING','COMPLETING','SUSPENDED'].includes(j.state)?overview.stamp:0),k=taskType(j);
  for(const d of days){const seconds=Math.min(d.b,end,overview.stamp)-Math.max(d.a,j.startTs);if(seconds>0){d.values[k]+=n*seconds/3600;d.jobs[k].push(j)}}
 }
 return days;
}
function gpuHours(){
 if(!jobsAvailable)return chartEmpty('暂时无法读取 GPU 作业记录');
 const days=weekData(),totals=days.map(d=>d.values.reduce((a,b)=>a+b,0)),max=Math.max(.01,...totals);
 if(!totals.some(Boolean))return chartEmpty('近 7 天没有已分配 GPU 的作业');
 const columns=days.map((day,i)=>{
  const segments=day.values.map((hours,k)=>{
   if(!hours)return '';
   const label=attr(day.label+' · '+kinds[k]+' · '+hours.toFixed(2)+' 卡时');
   return '<button data-gpuh="'+i+'|'+k+'" title="'+label+'" aria-label="'+label+'" style="background:'+colors[k]+';height:'+100*hours/max+'%"></button>';
  }).join('');
  const total=totals[i]>0&&totals[i]<0.1?'＜0.1':usageNumber.format(totals[i]);
  return '<div class="week-col"><div class="week-stack">'+segments+'</div><small>'+day.label+'</small><b class="gpu-day-total">'+total+'</b></div>';
 }).join('');
 let html='<div class="hours-plot"><div class="axis-y"><span>'+max.toFixed(1)+'</span><span>0</span></div><div class="week-bars">'+columns+'</div></div>';
 html+='<div class="studio-legend">'+kinds.map((k,i)=>chipLegend(colors[i],k)).join('')+'</div>';
 const d=days.find(v=>v.label===studio.day);if(d){html+='<div class="chart-detail">'+esc(d.label+' · '+kinds[studio.category])+' · '+d.values[studio.category].toFixed(2)+' 卡时 '+d.jobs[studio.category].map(j=>button('job',j.id,'#'+esc(j.id),'link')).join('')+'</div>'}
 return html+'<p class="usage-note">按分配卡数 × 占用小时累计。点击色块可查看对应作业。</p>';
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
  if(rows.length===1){const r=rows[0],idx=data.findIndex(d=>d.name===r.name);return '<button class="tree-tile" '+(r.other?'data-treeother="1"':'data-tree="'+attr(r.name)+'"')+' title="'+attr(treeLabel(r.name)+' · '+fmt(r.value))+'" style="left:'+x+'%;top:'+y+'%;width:'+w+'%;height:'+h+'%;--shade:'+(r.other?15:63-idx*6)+'%"><b>'+esc(treeLabel(r.name))+'</b><small>'+fmt(r.value)+'</small></button>'}
  const size=sum(rows);let split=1;while(split<rows.length-1&&Math.abs(sum(rows.slice(0,split+1))-size/2)<Math.abs(sum(rows.slice(0,split))-size/2))split++;
  const a=rows.slice(0,split),b=rows.slice(split),f=sum(a)/size;
  return w*2>h?tile(a,x,y,w*f,h)+tile(b,x+w*f,y,w*(1-f),h):tile(a,x,y,w,h*f)+tile(b,x,y+h*f,w,h*(1-f));
 }
 const selected=groups.get(studio.pickedGroup);
 return '<div class="storage-total">'+fmt(total)+' <small>已盘点 · '+groups.size+' 个任务'+(unknown?' · '+unknown+' 项大小未知':'')+'</small></div>'+(leaves.length?'<div class="tree-map">'+tile(leaves,0,0,100,100)+'</div>':chartEmpty(ckSources?.note||'没有已盘点的副本'))+(selected?'<div class="chart-detail">'+esc(treeLabel(studio.pickedGroup))+' '+copyButton(studio.pickedGroup)+button('treeopen',studio.pickedGroup,'查看 checkpoints →','link')+'</div>':'');
}
let efficiencyData=null,efficiencyExpanded=false,efficiencyCell='';
function gpuEfficiencyPanel(){
 const d=efficiencyData;
 if(!d||d.error)return studioPanel('GPU 利用概况','近 24 小时 · 全集群','',chartEmpty(d?.error||'正在读取 GPU 历史监测…'));
 const pct=v=>v==null?'—':numeric(v,1)+'%';
 const hours=v=>d.mean==null?'—':numeric(v,1);
 const metric=(value,label)=>'<div><strong>'+value+'</strong><small>'+label+'</small></div>';
 const summary='<div class="eff-summary">'+metric(pct(d.mean),'平均 GPU 利用率')+metric(hours(d.lowHours),'低利用卡时')+metric(hours(d.allocatedLowHours),'其中已分配卡时')+'</div>';
 const color=v=>v==null?'missing':v<10?'low':v<50?'medium':'busy';
 let heat='<div class="eff-heat" role="group" aria-label="节点逐小时平均 GPU 利用率">';
 for(const node of d.nodes){
  heat+='<div class="eff-node"><span>'+esc(node.name.replace('H800-',''))+'</span><div>';
  node.hours.forEach((h,i)=>{
   const time=chartTime(d.start+i*3600);
   const title=node.name+' · '+time+' · '+pct(h.mean)+' · 覆盖 '+pct(h.coverage*100);
   heat+=button('effcell',node.name+'|'+i,'','eff-cell '+color(h.mean)+(h.coverage<.9?' partial':''),'aria-label="'+attr(title)+'" title="'+attr(title)+'" aria-pressed="'+(efficiencyCell===node.name+'|'+i)+'"');
  });
  heat+='</div></div>';
 }
 heat+='</div><div class="eff-scale"><span>'+chartTime(d.start)+'</span><span>'+chartTime(d.end)+'</span></div>';
 const [picked,index]=efficiencyCell.split('|'),cell=d.nodes.find(n=>n.name===picked)?.hours[Number(index)];
 const selected=cell?'<p class="eff-selection">'+esc(picked)+' · '+chartTime(d.start+Number(index)*3600)+' · 平均 '+pct(cell.mean)+' · 覆盖 '+pct(cell.coverage*100)+'</p>':'';
 const legend='<div class="eff-legend"><span><i class="low"></i>低于 10%</span><span><i class="medium"></i>10–50%</span><span><i class="busy"></i>50–100%</span><span><i class="missing"></i>无采样</span></div>';
 const ordered=[...d.cards].sort((a,b)=>b.longestLowSeconds-a.longestLowSeconds||a.node.localeCompare(b.node)||a.index-b.index);
 const detail=efficiencyExpanded?'<div class="scroll eff-table">'+tableMarkup(['GPU','平均利用率','最长连续低利用','已分配低利用卡时','覆盖率'],ordered.map(c=>'<tr><td>'+esc(c.node.replace('H800-',''))+' / '+c.index+'</td><td>'+pct(c.mean)+'</td><td>'+(c.coverage?numeric(c.longestLowSeconds/60,0)+' 分钟':'—')+'</td><td>'+(c.coverage?numeric(c.allocatedLowHours,1):'—')+'</td><td>'+pct(c.coverage*100)+'</td></tr>'))+'</div>':'';
 const note='<div class="eff-note"><span>'+d.cardCount+' 张卡 · 数据覆盖 '+pct(d.coverage*100)+'</span><span>'+(d.mean==null?'—':d.sustainedCards)+' 张曾连续低利用 ≥ 30 分钟</span></div>';
 const method='<details class="eff-method"><summary>计算口径</summary><p>每 30 秒按物理 GPU 去重采样。平均利用率 = 有效采样利用率之和 ÷ 有效采样数。低利用卡时 = 利用率低于 10% 的采样数 × 30 秒 ÷ 3600。已分配依据监测中的作业标记。连续低利用时长按采样估算，遇到缺测即中断。</p><p>按当前节点卡数计算覆盖率，缺测时段单独标记。热图每格为一小时均值，斜纹表示覆盖不足 90%。当前观测 '+hours(d.observedHours)+' 卡时，内核活跃等效 '+hours(d.activeHours)+' 卡时。</p><p>GPU 利用率反映内核运行时间比例。数据加载、通信与保存权重都可能造成低利用，需结合具体作业分析。峰值算力效率需结合模型 FLOPs 测量。</p></details>';
 return studioPanel('GPU 利用概况','近 24 小时 · 全集群',button('effexpand','1',efficiencyExpanded?'收起明细':'逐卡明细','link','aria-expanded="'+efficiencyExpanded+'"'),(d.stale?'<p class="feedback">历史监测更新失败，显示上次统计至 '+chartTime(d.end)+' 的数据。</p>':'')+summary+note+heat+legend+selected+detail+method);
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

// Keep controls and user rows alive while their contents change.
function rankingKey(node){
 if(node.nodeType!==1)return '';
 if(node.hasAttribute('data-rank-user'))return 'user/'+node.dataset.rankUser;
 if(node.matches('.studio-switch'))return 'switch/'+node.getAttribute('aria-label');
 if(node.tagName==='BUTTON')return 'button/'+[...node.attributes].filter(a=>a.name.startsWith('data-')).map(a=>a.name+'='+a.value).join('/');
 return '';
}
function patchRanking(parent,template){
 const old=[...parent.childNodes], used=new Set();
 const keyed=new Map(old.filter(n=>rankingKey(n)).map(n=>[rankingKey(n),n]));
 let cursor=parent.firstChild;
 for(const fresh of [...template.childNodes]){
  const key=rankingKey(fresh);
  let node=key?keyed.get(key):old.find(n=>!used.has(n)&&!rankingKey(n)&&n.nodeType===fresh.nodeType&&n.nodeName===fresh.nodeName);
  if(!node)node=fresh.cloneNode(true);
  else if(node.nodeType===1){
   for(const a of [...node.attributes])if(!fresh.hasAttribute(a.name))node.removeAttribute(a.name);
   for(const a of fresh.attributes)if(node.getAttribute(a.name)!==a.value)node.setAttribute(a.name,a.value);
   patchRanking(node,fresh);
  }else if(node.nodeValue!==fresh.nodeValue)node.nodeValue=fresh.nodeValue;
  used.add(node);
  if(node!==cursor)parent.insertBefore(node,cursor);
  cursor=node.nextSibling;
 }
 for(const node of old)if(!used.has(node))node.remove();
}
function rankingSnapshot(panel){
 return new Map([...panel.querySelectorAll('[data-rank-user],.studio-switch,.usage-list,.storage-list,.storage-layout,.storage-summary,.storage-files')].map(node=>{
  const bar=node.querySelector('.usage-track i,.storage-bar i');
  const active=node.matches('.studio-switch')?node.querySelector('[aria-pressed=true]'):null;
  let selection=active?.getBoundingClientRect();
  if(active&&node.classList.contains('ranking-switch')){
   const style=getComputedStyle(node,'::before'),matrix=new DOMMatrixReadOnly(style.transform);
   const rect=node.getBoundingClientRect();
   selection={left:rect.left+parseFloat(style.left)+matrix.e,top:rect.top+parseFloat(style.top)+matrix.f,width:parseFloat(style.width)*matrix.a};
  }
  return [node,{rect:node.getBoundingClientRect(),bar:bar?.getBoundingClientRect().width,active:selection}];
 }));
}
function rankingAnimate(node,frames,duration=200){
 const animation=node.animate(frames,{duration,easing:'cubic-bezier(.2,.7,.2,1)'});
 animation.finished.catch(()=>{});
 return animation;
}
function updateRanking(panel,html,motion=''){
 const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
 const before=rankingSnapshot(panel);
 const focus=document.activeElement;
 const scrollTop=window.scrollY;
 // Read the current visual position before cancelling an interrupted transition.
 panel.getAnimations({subtree:true}).filter(a=>a.effect?.target!==panel||panel.tagName!=='DIALOG').forEach(a=>a.cancel());
 const template=document.createElement('template');template.innerHTML=html;
 patchRanking(panel,template.content);
 if(focus?.isConnected&&panel.contains(focus)&&document.activeElement!==focus)focus.focus({preventScroll:true});
 if(reduce)return;
 for(const group of panel.querySelectorAll('.studio-switch')){
  const active=group.querySelector('[aria-pressed=true]');if(!active)continue;
  const r=active.getBoundingClientRect(),g=group.getBoundingClientRect(),old=before.get(group)?.active;
  group.classList.add('ranking-switch');
  group.style.setProperty('--pick-x',(r.left-g.left)+'px');group.style.setProperty('--pick-y',(r.top-g.top)+'px');
  group.style.setProperty('--pick-w',r.width+'px');group.style.setProperty('--pick-h',r.height+'px');
  if(motion&&old&&Math.abs(old.top-r.top)<4){
   const a=group.animate([{transform:'translate('+(old.left-r.left)+'px,0) scaleX('+(old.width/r.width)+')'},{transform:'none'}],
    {duration:160,easing:'cubic-bezier(.2,.7,.2,1)',pseudoElement:'::before'});
   a.finished.catch(()=>{});
  }
 }
 if(!motion)return;
 const fade=motion==='fade'||motion==='next'||motion==='previous';
 const list=panel.querySelector('.usage-list,.storage-list,.storage-files');
 if(list){
  const old=before.get(list),height=list.getBoundingClientRect().height;
  if(old&&Math.abs(old.rect.height-height)>1){
   rankingAnimate(list,[{height:old.rect.height+'px',overflow:'hidden'},{height:height+'px',overflow:'hidden'}],motion==='expand'?220:160);
  }
  if(fade)rankingAnimate(list,[{opacity:.25,transform:'translateX('+(motion==='previous'?-6:motion==='next'?6:0)+'px)'},{opacity:1,transform:'none'}],160);
 }
 if(fade){
  const summary=panel.querySelector('.storage-summary');
  if(summary)rankingAnimate(summary,[{opacity:.35},{opacity:1}],160);
 }else{
  for(const row of panel.querySelectorAll('[data-rank-user]')){
   const old=before.get(row),rect=row.getBoundingClientRect();
   if(old){
    const dy=old.rect.top-rect.top;
    if(Math.abs(dy)>1)rankingAnimate(row,[{transform:'translateY('+dy+'px)'},{transform:'none'}],220);
    const bar=row.querySelector('.usage-track i,.storage-bar i');
    if(bar&&old.bar!=null)rankingAnimate(bar,[{width:old.bar+'px'},{width:bar.style.width}],200);
   }else rankingAnimate(row,[{opacity:0},{opacity:1}],180);
  }
 }
 // Keep the collapse control within reach when a long ranking contracts above the viewport.
 if(motion==='expand'&&scrollTop>panel.getBoundingClientRect().top+window.scrollY){
  const button=panel.querySelector('[data-usageall]');
  if(button&&button.getBoundingClientRect().bottom<0)button.scrollIntoView({block:'center',behavior:'instant'});
 }
}

const usageView={days:7, metric:'gpu', all:false, displayedDays:7, motion:'', cache:new Map(), pending:new Set(), errors:new Set()};
const usageMetrics={gpu:'GPU 卡时', time:'作业时长', jobs:'作业数', success:'作业完成率'};
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
  return '<li class="usage-row'+(personal?' usage-mine':'')+'" data-rank-user="'+attr(r.user)+'"><span class="usage-rank">'+r.rank+'</span><span class="usage-name" title="'+attr(r.user)+'">'+personLabel(r.user)+'</span>'+
   '<span class="usage-track" aria-hidden="true"><i style="width:'+100*(r.value||0)/scale+'%"></i></span>'+
   '<span class="usage-value">'+value+(metric==='success'?'<small>'+r.completed+' / '+r.ended+' 已结束</small>':'')+'</span></li>';
 }).join('');
 const total=data.users.reduce((t,r)=>({gpu:t.gpu+r.gpuSeconds/3600,jobs:t.jobs+r.jobs,ok:t.ok+r.completed,ended:t.ended+r.ended}),{gpu:0,jobs:0,ok:0,ended:0});
 const success=total.ended?100*total.ok/total.ended:null;
 const tabs=Object.entries(usageMetrics).map(([key,label])=>control('usagemetric',key,label,metric===key)).join('');
 return '<div class="usage-layout"><div class="usage-summary"><small>累计 GPU 卡时</small><strong>'+usageNumber.format(total.gpu)+'</strong>'+
  '<span>'+sorted.length+' 位用户 · '+usageNumber.format(total.jobs)+' 个 GPU 作业</span><div class="usage-finish"><i aria-hidden="true" style="--share:'+(success||0)+'%"></i><div>作业完成率 <b>'+usageFormat(success,'success')+'</b><small>'+total.ok+' 已完成 / '+total.ended+' 已结束</small></div></div></div>'+
  '<div class="usage-plot"><div class="studio-switch" role="group" aria-label="排行指标">'+tabs+'</div>'+
  (rows?'<ol class="usage-list" aria-label="'+usageMetrics[metric]+'排行">'+rows+'</ol>':chartEmpty('此时段没有分配 GPU 的作业'))+
  (sorted.length>5?button('usageall','1',all?'收起排行':'查看全部 '+sorted.length+' 位用户','link','aria-expanded="'+all+'"'):'')+'</div></div>'+
  '<p class="usage-note">统计当前账号可见、实际分配过 GPU 的所有作业。卡时按时段内的占用时长乘以卡数累计。完成率为 Slurm 状态为 COMPLETED 的作业数占已结束作业数的比例。</p>';
}
function rankIcon(kind){
 const drawing=kind==='gpu'
  ? '<rect x="7" y="7" width="18" height="18" rx="4"/><path d="M11 4v3m5-3v3m5-3v3M11 25v3m5-3v3m5-3v3M4 11h3m-3 5h3m-3 5h3m18-10h3m-3 5h3m-3 5h3"/><path class="rank-glyph-fill" d="m12 20 2-8h2l-2 8zm5-2 2-8h2l-2 8z"/>'
  : '<ellipse cx="16" cy="8" rx="10" ry="4"/><path d="M6 8v15c0 2.2 4.5 4 10 4s10-1.8 10-4V8M6 15c0 2.2 4.5 4 10 4s10-1.8 10-4"/><path d="M21 14.2l1.4-.5m-1.4 8 1.4-.5"/>';
 return '<span class="rank-glyph rank-glyph-'+kind+'" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round">'+drawing+'</svg></span>';
}
function usageCard(){
 const {days,cache,errors}=usageView, data=cache.get(days), failed=errors.has(days);
 if(data)usageView.displayedDays=days;
 const previous=cache.get(usageView.displayedDays);
 const waiting=!data&&previous;
 const status=waiting?(failed?'读取近 '+days+' 天失败':'正在读取近 '+days+' 天')+' · 暂显示近 '+usageView.displayedDays+' 天':'';
 const period=[7,30].map(n=>control('usagedays',n,'近 '+n+' 天',days===n)).join('');
 const hint=status|| (failed?'暂时无法更新':data?'采样于 '+chartTime(data.stamp):'读取 Slurm 记账数据中…');
 return '<div class="studio-heading"><div class="usage-title">'+rankIcon('gpu')+'<div><h2>GPU 用量排行</h2><small>当前账号可见的用户 · '+hint+'</small></div></div><div class="studio-switch" role="group" aria-label="统计周期">'+period+'</div></div>'+
  (data||waiting?'<div class="ranking-result" aria-busy="'+!!waiting+'">'+usageRanking(data||previous)+'</div>':chartEmpty(failed?'Slurm 记账数据暂时不可用。':'正在汇总 GPU 作业…'))+
  (failed?button('usageretry','1','重新读取','link'):'');
}
function refreshUsageCard(motion=''){
 const panel=$('trainingUsage');if(page!==0||!panel)return;
 updateRanking(panel,usageCard(),motion||usageView.motion);
 if(usageView.cache.has(usageView.days)||usageView.errors.has(usageView.days))usageView.motion='';
}
async function loadUsage(){
 const {days,cache,pending,errors}=usageView;
 if(pending.has(days)||Date.now()/1000-(cache.get(days)?.stamp||0)<300&&!errors.has(days))return;
 pending.add(days);
 try{cache.set(days,await api('gpu-usage',{days}));errors.delete(days)}
 catch{errors.add(days)}
 finally{pending.delete(days);if(days===usageView.days)refreshUsageCard()}
}
function dashboard(){
 loadStorageUsage();
 loadUsage();
 return dashboardOverview()+(studio.issues.length?'<p class="feedback">'+esc(studio.issues.join(' · '))+'</p>':'')+'<div class="studio-board">'+
 studioPanel('我的作业时间线','活跃作业优先 · 历史作业按结束时间倒序',button('page','2','训练记录 →','link'),gantt(),true)+
 studioPanel('节点空闲窗口','近 12 小时 · 每小时最后一次采样','',heatmap())+
 studioPanel('我的 GPU 卡时','近 7 天 · 每日分配用量','',gpuHours())+
 studioPanel('权重占用分布','各训练任务的存储占比','<div class="studio-switch">'+['H800','NAS'].map(s=>control('treestore',s,s,studio.store===s)).join('')+'</div>',storageMap())+
 gpuEfficiencyPanel()+'</div><section id="storageUsage" class="surface studio-card below" aria-label="存储用量排行">'+storageCard()+'</section><section id="trainingUsage" class="surface studio-card below" aria-label="GPU 用量排行">'+usageCard()+'</section>';
}
async function loadStudio(){
 if(studio.ready&&Date.now()-studioUpdated<50000&&ckSources)return;
 if(studioRequest)return studioRequest;
 studioRequest=(async()=>{
  const names=['节点历史','训练摘要','权重清单','GPU 利用率'];
  const results=await Promise.allSettled([api('node-history'),api('training-summary'),ckSources||api('checkpoint-sources'),api('gpu-efficiency')]);
  studio.issues=results.flatMap((r,i)=>r.status==='rejected'?[names[i]+'暂时无法读取']:[]);
  if(results[0].status==='fulfilled')studio.history=results[0].value;
  if(results[1].status==='fulfilled'){studio.trainingJobs=new Set(results[1].value.jobs);studio.evaluations=results[1].value.evaluations}
  if(results[2].status==='fulfilled')ckSources=results[2].value;
  efficiencyData=results[3].status==='fulfilled'?results[3].value:efficiencyData?{...efficiencyData,stale:true}:{error:'GPU 历史监测暂时不可用'};
  studioUpdated=Date.now();studio.ready=true;
 })().finally(()=>{studioRequest=null});return studioRequest;
}
document.addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b||page!==0)return;const d=b.dataset;
 if(d.usagedays||d.usagemetric||d.usageall||d.usageretry){
  if(d.usagedays){if(usageView.days===Number(d.usagedays))return;usageView.days=Number(d.usagedays);usageView.all=false;usageView.motion='reorder'}
  if(d.usagemetric){if(usageView.metric===d.usagemetric)return;usageView.metric=d.usagemetric}
  if(d.usageall)usageView.all=!usageView.all;
  refreshUsageCard(d.usageall?'expand':d.usagemetric?'reorder':'fade');if(d.usagedays||d.usageretry)loadUsage();return;
 }
 if(d.effexpand){efficiencyExpanded=!efficiencyExpanded;render();return}
 if(d.effcell){efficiencyCell=efficiencyCell===d.effcell?'':d.effcell;render();return}
 if(d.metric==='ck'){ckStore='H800';setResource('ck');return}
 else if(d.metric){selectedState=d.metric==='all'?'':d.metric;selectedJob='';navigate(2);return}
 else if(d.heat)studio.sample=d.heat;
 else if(d.timelinepage!==undefined)timelinePage=Number(d.timelinepage);
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

window.addEventListener('resize',()=>{
 for(const panel of document.querySelectorAll('#storageUsage,#trainingUsage')){
  for(const group of panel.querySelectorAll('.ranking-switch')){
   const active=group.querySelector('[aria-pressed=true]');if(!active)continue;
   const r=active.getBoundingClientRect(),g=group.getBoundingClientRect();
   group.style.setProperty('--pick-x',(r.left-g.left)+'px');group.style.setProperty('--pick-y',(r.top-g.top)+'px');
   group.style.setProperty('--pick-w',r.width+'px');group.style.setProperty('--pick-h',r.height+'px');
  }
 }
});
