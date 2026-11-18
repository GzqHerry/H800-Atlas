const storageView={mount:'home',index:0,cache:new Map(),pending:new Set(),errors:new Set(),checked:new Map()};
const storageStates={pending:'统计中',absent:'未建个人目录',permission:'无权限',timeout:'查询超时',unsupported:'暂不支持统计',error:'统计失败'};
function storageCard(){
 const {mount,cache,errors}=storageView,data=cache.get(mount);
 const tabs=['home','workspace','data','scratch'].map(m=>control('storagemount',m,'/'+m,m===mount)).join('');
 const hint=data?.stamp?'统计完成于 '+chartTime(data.stamp):'等待首次统计';
 const header='<div class="studio-heading"><div class="storage-heading"><i class="storage-symbol" aria-hidden="true"></i><div><h2>存储用量排行</h2><small>'+hint+(data?.node?' · '+esc(data.node):'')+(data?.refreshing?' · 正在更新':'')+'</small></div></div><div class="studio-switch" role="group" aria-label="存储盘">'+tabs+'</div></div>';
 if(!data)return header+chartEmpty(errors.has(mount)?'暂时无法连接统计服务，稍后自动重试。':'正在读取用户目录用量…');
 const rows=[...data.rows].sort((a,b)=>(b.bytes!==null)-(a.bytes!==null)||(b.bytes??0)-(a.bytes??0)||a.user.localeCompare(b.user));
 const known=rows.filter(r=>r.bytes!==null),unavailable=rows.filter(r=>r.bytes===null&&r.status!=='absent');
 const total=known.reduce((sum,r)=>sum+r.bytes,0),peak=Math.max(1,...known.map(r=>r.bytes));
 const limit=10;storageView.index=Math.min(storageView.index,Math.max(0,Math.ceil(rows.length/limit)-1));
 const items=rows.slice(storageView.index*limit,(storageView.index+1)*limit).map((row,i)=>{
  const measured=row.bytes!==null,old=row.status!=='ok'&&measured;
  const caption=(row.status==='ok'?(data.approximate?'估算':''):storageStates[row.status]||'待统计')+(old?' · 上次数据':'');
  const percent=measured?Math.min(100,100*row.bytes/(data.quota||peak)):0;
  const meta=[caption,row.stamp?'采集于 '+chartTime(row.stamp):''].filter(Boolean).join(' · ');
  return '<li class="storage-row'+(row.user===overview.user?' storage-mine':'')+'"><span class="storage-rank">'+(measured?storageView.index*limit+i+1:'—')+'</span><div class="storage-person">'+personLabel(row.user)+(row.user===overview.user?'<small>我</small>':'')+'</div><button class="storage-bar'+(!measured?' unmeasured':'')+'" data-storageuser="'+attr(row.user)+'" aria-label="'+attr('查看 '+row.user+' 的存储分类')+'"><i style="width:'+percent+'%"></i></button><div class="storage-value">'+button('storageuser',row.user,(measured?fmt(row.bytes):'—')+' <span aria-hidden="true">›</span>','storage-open','aria-label="'+attr('查看 '+row.user+' 的存储分类')+'"')+'<small title="'+attr(meta)+'">'+esc(meta||'等待统计')+'</small></div></li>';
 }).join('');
 const note=mount==='home'?'个人配额按 30 GiB 展示，进度条表示配额使用比例。':'进度条按当前盘已知用量最大的用户缩放。';
 const coverage=(mount==='scratch'?'仅统计 '+esc(data.node||'当前服务节点')+' 的本地盘；无权限目录会单独标记。':'按同名个人目录汇总，共享目录及其他位置的文件需另行统计。')+(data.approximate?' /data 使用 JuiceFS 快速估算，可能存在误差。':'');
 return header+'<div class="storage-layout" data-mount="'+mount+'"><div class="storage-summary"><small>已统计目录用量</small><strong>'+(known.length?fmt(total):'—')+'</strong><span>'+known.length+' 位已有数据 · '+unavailable.length+' 位待确认</span>'+(data.quota?'<div class="storage-quota"><small>每人 /home 配额</small><b>30 GiB</b></div>':'')+'<p>'+note+'</p></div><div class="storage-ranking"><ol class="storage-list" aria-label="/'+mount+' 用户存储排行">'+items+'</ol>'+(!rows.length?chartEmpty(data.refreshing?'正在发现用户目录…':'暂无可识别的用户目录'):'')+pageControls('storagepage',storageView.index,rows.length,limit)+'</div></div>'+(data.error||errors.has(mount)?'<p class="feedback">'+esc(data.error||'更新暂时失败，将自动重试')+'</p>':'')+'<p class="usage-note">'+coverage+' 每 15 分钟更新，耗时统计完成后自动显示。</p>';
}
function refreshStorageCard(){
 const panel=$('storageUsage');if(page!==0||!panel)return;
 const active=document.activeElement;
 const focus=panel.contains(active)?Object.entries(active.dataset).find(([key])=>key.startsWith('storage')):null;
 panel.innerHTML=storageCard();
 if(focus)panel.querySelector('[data-'+focus[0]+'="'+focus[1]+'"]')?.focus({preventScroll:true});
}
async function loadStorageUsage(){
 const {mount,cache,pending,checked,errors}=storageView;
 if(!live||page!==0||document.hidden||pending.has(mount))return;
 const interval=cache.get(mount)?.refreshing?4000:60000;
 if(Date.now()-(checked.get(mount)||0)<interval)return;
 pending.add(mount);
 try{cache.set(mount,await api('storage-usage',{mount}));errors.delete(mount)}
 catch{errors.add(mount)}
 finally{checked.set(mount,Date.now());pending.delete(mount);if(mount===storageView.mount)refreshStorageCard()}
}
document.addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b||page!==0)return;
 if(b.dataset.storagemount){storageView.mount=b.dataset.storagemount;storageView.index=0;refreshStorageCard();loadStorageUsage()}
 if(b.dataset.storagepage!==undefined){storageView.index=Number(b.dataset.storagepage);refreshStorageCard()}
});
setInterval(loadStorageUsage,5000);

const storageCategories={all:'全部',checkpoints:'Checkpoints',datasets:'Datasets',logs:'日志',environments:'运行环境',cache:'缓存',code:'代码与配置',other:'其他／未分类'};
const storageDetail={mount:'',user:'',category:'all',index:0,data:null,request:0,timer:null};
const storageDialog=document.createElement('dialog');
storageDialog.id='storageDialog';storageDialog.setAttribute('aria-labelledby','storageDetailTitle');
document.body.append(storageDialog);
function paintStorageDetail(){
 const view=storageDetail,data=view.data,items=data?.items||[];
 const totals={};for(const item of items)totals[item.category]=(totals[item.category]||0)+(item.bytes||0);
 const sum=Object.values(totals).reduce((a,b)=>a+b,0);
 const selected=items.filter(item=>view.category==='all'||item.category===view.category).sort((a,b)=>(b.bytes!==null)-(a.bytes!==null)||(b.bytes||0)-(a.bytes||0)||a.name.localeCompare(b.name));
 const segments=Object.entries(totals).filter(([,n])=>n>0).map(([key,n])=>'<i class="storage-kind-'+key+'" style="width:'+100*n/(sum||1)+'%" title="'+attr(storageCategories[key]+' · '+fmt(n))+'"></i>').join('');
 const tabs=Object.entries(storageCategories).map(([key,title])=>button('storagecategory',key,esc(title)+(key==='all'?'':'<small>'+fmt(totals[key]||0)+'</small>'),'storage-category storage-kind-'+key,'aria-pressed="'+(view.category===key)+'"')).join('');
 const count=10;view.index=Math.min(view.index,Math.max(0,Math.ceil(selected.length/count)-1));
 const rows=selected.slice(view.index*count,(view.index+1)*count).map(item=>'<div class="storage-file"><span class="storage-kind-dot storage-kind-'+item.category+'" aria-hidden="true"></span><div><strong title="'+attr(item.name)+'">'+esc(item.name.split('/').pop())+'</strong><small>'+esc(storageCategories[item.category])+'</small></div><div class="storage-file-size"><b>'+fmt(item.bytes)+'</b>'+(item.status==='ok'?'':'<small>'+esc(storageStates[item.status]||'待统计')+'</small>')+'</div>'+copyButton(item.path)+'</div>').join('');
 const hint=!data?'正在读取…':data.refreshing?'正在识别目录，结果逐步显示':data.partial?'部分目录未能完成统计 · '+chartTime(data.stamp): '采集于 '+chartTime(data.stamp);
 const focus=document.activeElement;
 const key=storageDialog.contains(focus)?Object.entries(focus.dataset).find(([k])=>k.startsWith('storage')):null;
 storageDialog.innerHTML='<div class="storage-dialog-heading"><div><small>/'+esc(view.mount)+(view.mount==='scratch'?' · '+esc(storageView.cache.get('scratch')?.node||'node11'):'')+'</small><h2 id="storageDetailTitle">'+esc(view.user)+' 的存储</h2></div>'+button('storageclose','1','×','storage-dialog-close','aria-label="关闭存储明细"')+'</div><div class="storage-detail-summary"><strong>'+fmt(sum)+'</strong><span>已识别用量</span><small role="status">'+hint+'</small></div><div class="storage-segments" aria-hidden="true">'+segments+'</div><div class="storage-categories" role="group" aria-label="文件分类">'+tabs+'</div><div class="storage-files">'+(rows||chartEmpty(data?.refreshing?'正在扫描可访问的目录…':'此分类暂无可显示的项目'))+'</div>'+pageControls('storagedetailpage',view.index,selected.length,count)+'<p class="usage-note">按文件特征与目录名称识别，最多检查 4 层目录。未识别内容归入其他／未分类，受限目录保留状态。分类合计可能小于目录总用量。</p>';
 if(key)storageDialog.querySelector('[data-'+key[0]+'="'+key[1]+'"]')?.focus({preventScroll:true});
}
async function loadStorageDetail(){
 const request=++storageDetail.request;
 clearTimeout(storageDetail.timer);
 try{
  const data=await api('storage-detail',{mount:storageDetail.mount,user:storageDetail.user});
  if(request!==storageDetail.request||!storageDialog.open)return;
  storageDetail.data=data;paintStorageDetail();
  if(data.refreshing)storageDetail.timer=setTimeout(loadStorageDetail,2500);
 }catch{
  if(request!==storageDetail.request||!storageDialog.open)return;
  storageDialog.querySelector('[role=status]').textContent='暂时无法读取，将自动重试';
  storageDetail.timer=setTimeout(loadStorageDetail,10000);
 }
}
document.addEventListener('click',e=>{
 const button=e.target.closest('button');if(!button)return;
 if(button.dataset.storageuser){
  const origin=button.getBoundingClientRect();
  Object.assign(storageDetail,{mount:storageView.mount,user:button.dataset.storageuser,category:'all',index:0,data:null});
  paintStorageDetail();storageDialog.showModal();
  if(!matchMedia('(prefers-reduced-motion: reduce)').matches){
   const target=storageDialog.getBoundingClientRect();
   storageDialog.animate([{transform:'translate('+((origin.left+origin.width/2)-(target.left+target.width/2))+'px,'+((origin.top+origin.height/2)-(target.top+target.height/2))+'px) scale(.35)',opacity:.2},{transform:'none',opacity:1}],{duration:500,easing:'cubic-bezier(.2,.7,.2,1)'});
  }
  loadStorageDetail();
 }
 if(button.dataset.storageclose)storageDialog.close();
 if(button.dataset.storagecategory){storageDetail.category=button.dataset.storagecategory;storageDetail.index=0;paintStorageDetail()}
 if(button.dataset.storagedetailpage!==undefined){storageDetail.index=Number(button.dataset.storagedetailpage);paintStorageDetail()}
});
storageDialog.addEventListener('close',()=>{storageDetail.request++;clearTimeout(storageDetail.timer);storageDialog.getAnimations().forEach(a=>a.cancel())});
storageDialog.addEventListener('click',e=>{if(e.target!==storageDialog)return;const r=storageDialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)storageDialog.close()});
