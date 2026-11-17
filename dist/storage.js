const storageView={mount:'home',index:0,cache:new Map(),pending:new Set(),errors:new Set(),checked:new Map()};
const storageStates={pending:'统计中',absent:'未建个人目录',permission:'无权限',timeout:'查询超时',unsupported:'暂不支持统计',error:'统计失败'};
function storageCard(){
 const {mount,cache,errors}=storageView,data=cache.get(mount);
 const tabs=['home','workspace','data'].map(m=>control('storagemount',m,'/'+m,m===mount)).join('');
 const hint=data?.stamp?'统计完成于 '+chartTime(data.stamp):'等待首次统计';
 const header='<div class="studio-heading"><div class="storage-heading"><i class="storage-symbol" aria-hidden="true"></i><div><h2>存储用量排行</h2><small>'+hint+(data?.refreshing?' · 正在更新':'')+'</small></div></div><div class="studio-switch" role="group" aria-label="存储盘">'+tabs+'</div></div>';
 if(!data)return header+chartEmpty(errors.has(mount)?'暂时无法连接统计服务，稍后自动重试。':'正在读取用户目录用量…');
 const rows=[...data.rows].sort((a,b)=>(b.bytes!==null)-(a.bytes!==null)||(b.bytes??0)-(a.bytes??0)||a.user.localeCompare(b.user));
 const known=rows.filter(r=>r.bytes!==null),unavailable=rows.filter(r=>r.bytes===null&&r.status!=='absent');
 const total=known.reduce((sum,r)=>sum+r.bytes,0),peak=Math.max(1,...known.map(r=>r.bytes));
 const limit=8;storageView.index=Math.min(storageView.index,Math.max(0,Math.ceil(rows.length/limit)-1));
 const items=rows.slice(storageView.index*limit,(storageView.index+1)*limit).map((row,i)=>{
  const measured=row.bytes!==null,old=row.status!=='ok'&&measured;
  const caption=(row.status==='ok'?(data.approximate?'估算':''):storageStates[row.status]||'待统计')+(old?' · 上次数据':'');
  const percent=measured?Math.min(100,100*row.bytes/(data.quota||peak)):0;
  const meta=[caption,row.stamp?'采集于 '+chartTime(row.stamp):''].filter(Boolean).join(' · ');
  return '<li class="storage-row'+(row.user===overview.user?' storage-mine':'')+'"><span class="storage-rank">'+(measured?storageView.index*limit+i+1:'—')+'</span><div class="storage-person">'+personLabel(row.user)+(row.user===overview.user?'<small>我</small>':'')+'</div><div class="storage-bar'+(!measured?' unmeasured':'')+'" aria-hidden="true"><i style="width:'+percent+'%"></i></div><div class="storage-value"><b>'+ (measured?fmt(row.bytes):'—')+'</b><small>'+esc(meta||'等待统计')+'</small></div></li>';
 }).join('');
 const note=mount==='home'?'个人配额按 30 GiB 展示，进度条表示配额使用比例。':'进度条按当前盘已知用量最大的用户缩放。';
 const coverage='按同名个人目录汇总，共享目录及其他位置的文件需另行统计。'+(data.approximate?' /data 使用 JuiceFS 快速估算，可能存在误差。':'');
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
