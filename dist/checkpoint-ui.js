let visibleCheckpoints=new Map();
let focusedCheckpoint=null;
const checkpointStores=['H800','NAS','Grus','Tianji'];

function transferStore(job){
 return job.action==='remove'?'H800':uploadTarget(job);
}

function activeCheckpointBar(job,store){
 const transferring=job.state==='传输中';
 const verifying=['重新校验','校验中','计算 SHA-256'].includes(job.state);
 if(!transferring&&!verifying)return '';
 const bytes=transferring?job.processedBytes:job.checkedBytes;
 let percent=null;
 if(Number.isFinite(bytes)&&job.bytes>0)percent=Math.floor(100*bytes/job.bytes);
 else if(transferring&&Number.isFinite(job.percent))percent=job.percent;
 if(percent!==null)percent=Math.max(0,Math.min(100,percent));
 const detail=Number.isFinite(bytes)&&job.bytes>0?fmt(bytes)+' / '+fmt(job.bytes):'';
 const value=percent===null?'aria-valuetext="'+attr(job.state)+'"':'aria-valuenow="'+percent+'"';
 return '<div class="ck-active '+(verifying?'verify ':'')+(percent===null?'indeterminate':'')+'">'+
  '<div class="upload-track" role="progressbar" aria-label="'+attr(store+' '+job.state)+'" aria-valuemin="0" aria-valuemax="100" '+value+'><i style="--filled:'+(percent??25)+'%"></i></div>'+
  '<small>'+esc(detail)+(percent===null?'':' <b>'+percent+'%</b>')+'</small></div>';
}

function statusMark(label,tone='',icon=''){
 return '<span class="ck-mark '+tone+'">'+(icon?'<i aria-hidden="true">'+icon+'</i>':'')+esc(label)+'</span>';
}

function checkpointStatus(c,store,job){
 if(job&&job.state!=='完成'){
  const bar=activeCheckpointBar(job,store);
  const failed=job.state==='失败',queued=['待传输','等待复核'].includes(job.state);
  const tone=failed?'bad':queued?'wait':job.state==='传输中'?'moving':'checking';
  let label=job.state==='待传输'?'排队中':job.state;
  if(job.state==='传输中'&&job.updated&&Date.now()/1000-job.updated>45)label='等待进度更新';
  return statusMark(label,tone,failed?'!':queued?'◷':'•')+bar;
 }
 if(store==='H800'){
  if(c.present)return statusMark('已保留');
  if(job)return statusMark('已清理','ok','✓')+(job.bytes?'<small>'+fmt(job.bytes)+'</small>':'');
 }else if(store==='NAS'&&c.copies.length){
  return c.copies.some(copy=>copy.verified)?statusMark('已校验','ok','✓'):statusMark('待校验','wait','◷');
 }else if((store==='Grus'&&c.grus)||(store==='Tianji'&&job)){
  return statusMark('已上传','ok','✓');
 }
 return '<span class="ck-absent" aria-label="无副本记录">—</span>';
}

function groupDetailView(group,items){
 visibleCheckpoints=new Map(items.map(c=>[c.path,c]));
 const rows=items.map(c=>{
  const epoch=c.epoch==null?'':'<small>Epoch '+numeric(c.epoch)+(c.epochs==null?'':' / '+numeric(c.epochs,0))+'</small>';
  return '<tr data-ckrow="'+attr(c.path)+'"><th scope="row">'+button('ckfocus',c.path,esc(c.name),'ck-focus-link','aria-haspopup="dialog"')+epoch+'</th>'+
   checkpointStores.map(store=>'<td data-ckstorecell="'+store+'"><small class="ck-mobile-label">'+store+'</small><div></div></td>').join('')+'</tr>';
 }).join('');
 return '<article class="surface below ck-library"><div class="line">'+button('backgroups','1','← 全部训练任务','link')+'<small>'+items.length+' 个 checkpoint</small></div>'+
  '<div class="ck-group-title"><h2>'+esc(groupLabel(group))+'</h2>'+copyButton(group.copyPath)+'</div>'+
  '<table class="ck-list"><thead><tr><th>Checkpoint</th>'+checkpointStores.map(store=>'<th data-ckstorecell="'+store+'">'+store+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table></article>';
}

function paintFocus(c){
 focusedCheckpoint=c;
 $('ckFocusName').textContent=c.name;
 const metrics=[['Step',numeric(c.step,0)],['Epoch',numeric(c.epoch)+' / '+numeric(c.epochs,0)],
  ['训练 loss',numeric(c.loss,8),atStep(c.lossStep,c.step)],['学习率',c.lr==null?'—':Number(c.lr).toExponential(3),atStep(c.lrStep,c.step)],
  ['盘点大小',fmt(c.bytes)],['更新时间',updatedLabel(c)]];
 $('ckFocusBody').innerHTML='<dl class="ck-facts">'+metrics.map(([label,value,note])=>'<div><dt>'+label+'</dt><dd>'+esc(value)+'</dd>'+(note?'<small>'+esc(note)+'</small>':'')+'</div>').join('')+
  '</dl><div class="ck-actions">'+checkpointLocations(c)+'</div><div id="ckFocusActivity"></div><p role="status" id="ckFeedback"></p>';
}

function openCheckpointFocus(trigger){
 const c=visibleCheckpoints.get(trigger.dataset.ckfocus);
 if(!c)return;
 const origin=trigger.getBoundingClientRect(),dlg=$('ckFocus');
 paintFocus(c);dlg.showModal();refreshTransferViews();
 if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const r=dlg.getBoundingClientRect();
 const x=origin.left+origin.width/2-r.left-r.width/2,y=origin.top+origin.height/2-r.top-r.height/2;
 dlg.animate([{transform:'translate('+x+'px,'+y+'px) scale(.18)',opacity:.3},{transform:'none',opacity:1}],
  {duration:480,easing:'cubic-bezier(.22,.68,.18,1)'});
}

function historyContents(){
 const rows=transferRows.filter(job=>job.meta?.kind==='checkpoint'&&(!groupPath||job.meta.group===groupPath));
 if(!rows.length)return '<p class="empty">暂无操作记录。</p>';
 return rows.map(j=>{
  const store=transferStore(j),done=j.state==='完成';
  const phase=statusMark(done?(store==='H800'?'已清理':'已完成'):j.state,done?'ok':j.state==='失败'?'bad':'',done?'✓':'');
  const time=j.started?new Date(j.started*1000).toLocaleString('zh-CN',{hour12:false}):'';
  return '<article class="ck-event"><small>'+esc(time)+'</small><h3>'+esc(j.meta.name)+'</h3>'+
   (!groupPath?'<small>'+esc(j.meta.group.split('/').pop())+'</small>':'')+
   '<div class="ck-event-state"><b>'+esc(store==='H800'?'本地清理':store+' 上传')+'</b>'+phase+(done&&j.bytes?'<small>'+fmt(j.bytes)+'</small>':'')+'</div>'+
   activeCheckpointBar(j,store)+(j.error?'<small class="feedback">'+esc(j.error)+'</small>':'')+copyButton(j.destination)+'</article>';
 }).join('');
}

function openCheckpointHistory(){
 $('ckHistoryBody').innerHTML=historyContents();
 $('ckHistory').showModal();
}

function setMarkup(el,html){
 if(el._ckMarkup!==html){el.innerHTML=html;el._ckMarkup=html}
}

function refreshCheckpointUI(){
 const jobs=new Map();
 for(const job of transferRows){
  if(job.meta?.kind!=='checkpoint')continue;
  if(!jobs.has(job.source))jobs.set(job.source,new Map());
  const stores=jobs.get(job.source),store=transferStore(job);
  if(!stores.has(store))stores.set(store,job);
 }
 const list=document.querySelector('.ck-list');
 if(list){
  list.classList.toggle('with-tianji',[...visibleCheckpoints.keys()].some(path=>jobs.get(path)?.has('Tianji')));
  for(const row of list.tBodies[0].rows){
   const c=visibleCheckpoints.get(row.dataset.ckrow);
   if(!c)continue;
   for(const cell of row.querySelectorAll('[data-ckstorecell]')){
    const store=cell.dataset.ckstorecell;
    setMarkup(cell.lastElementChild,checkpointStatus(c,store,jobs.get(c.path)?.get(store)));
   }
  }
 }
 if($('ckFocus').open&&focusedCheckpoint){
  const c=visibleCheckpoints.get(focusedCheckpoint.path);
  if(!list||!c)$('ckFocus').close();
  else{
   if(c!==focusedCheckpoint)paintFocus(c);
   const notices=removalNotices.get(c.path);
   const activity=[...(jobs.get(c.path)||[])].filter(([,j])=>j.state!=='完成');
   setMarkup($('ckFocusActivity'),(notices?'<p class="feedback">'+esc(notices)+'</p>':'')+activity.map(([store,j])=>
    '<div class="ck-focus-status"><b>'+store+'</b> '+checkpointStatus(c,store,j)+(j.error?'<small class="feedback">'+esc(j.error)+'</small>':'')+'</div>').join(''));
  }
 }
 if($('ckHistory').open)setMarkup($('ckHistoryBody'),historyContents());
}

for(const dlg of [$('ckHistory'),$('ckFocus')]){
 dlg.addEventListener('click',e=>{
  if(e.target!==dlg)return;
  const r=dlg.getBoundingClientRect();
  if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dlg.close();
 });
}
