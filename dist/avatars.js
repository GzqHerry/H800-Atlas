const crewNames=[
 '太空面罩','双目探测车','土星环','天文台','轨道卫星','星际狐','月牙','深空天线','升空火箭','方头机器人','地球','分段镜望远镜','登月舱','太空猫','返回舱','月面巡游车',
 '月岭','太空漫步','日全食','机械臂','彗尾','月面足迹','太阳帆','环形空间站','旋涡星系','轨道对接','航天飞机','纸鹤信标','月面旗帜','双星','返回伞','黑洞',
 '月面行者','机器犬','月面基地','小行星','雷达塔','冰泉','勘测无人机','月壤样本','履带钻机','撞击坑','航天手套','轨道起重机','地球舷窗','冰晶','滑翔器','安全系绳'
];
const crewEditions=['探路者','深空航行','月面勘探'];
let avatarChoices=new Map(), avatarRevision=0, avatarLoading=false;
let avatarDraft=null, avatarRead=0, avatarSaving=false;

function assignedAvatar(user){
 // A fixed seed keeps already assigned portraits stable across sessions and releases.
 let hash=2166136261;
 for(const ch of 'atlas-lunar-crew-v1/'+user)hash=Math.imul(hash^ch.charCodeAt(0),16777619);
 return (hash>>>0)%crewNames.length;
}
function portrait(value){
 if(Number.isInteger(value)&&value>=0&&value<crewNames.length){
  const position=(value%4)*100/3+'% '+Math.floor(value/4)*100/11+'%';
  return '<i aria-hidden="true" class="crew-portrait" style="background-position:'+position+'"></i>';
 }
 return '<img alt="" class="crew-portrait" src="'+attr(value)+'">';
}
function userAvatar(user,editable=false){
 const image=portrait(avatarChoices.get(user)??assignedAvatar(user));
 return editable&&user===overview.user?
  '<button class="crew-edit" data-editavatar aria-label="更换我的头像">'+image+'</button>':image;
}
function personLabel(user,editable=true){
 return '<span class="crew-person">'+userAvatar(user,editable)+button('profile',user,esc(user),'profile-link','aria-haspopup="dialog"')+'</span>';
}
function paintAvatars(){
 if(!overview.user)return;
 $('initials').innerHTML=userAvatar(overview.user);
 if($('profileDialog').open)$('profilePortrait').innerHTML=userAvatar($('profileName').textContent,true);
 document.querySelectorAll('.crew-person').forEach(el=>{
  const name=el.lastElementChild.textContent;
  const host=el.firstElementChild.matches('button')?el.firstElementChild:el;
  host.querySelector('.crew-portrait').outerHTML=userAvatar(name);
 });
}
async function syncAvatars(){
 if(avatarLoading||!live||document.hidden)return;
 avatarLoading=true;
 const revision=avatarRevision;
 try{
  const values=await api('avatars');
  if(revision===avatarRevision){avatarChoices=new Map(Object.entries(values));paintAvatars()}
 }catch{/* Keep the last known portraits until the next refresh. */}
 finally{avatarLoading=false}
}
function previewAvatar(value){
 avatarDraft=value;
 $('avatarPreview').innerHTML=portrait(value);
 $('avatarPresets').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',Number(b.dataset.preset)===value));
 $('avatarMessage').textContent='';
}
function showCrewEdition(group){
 $('avatarEditions').innerHTML=crewEditions.map((label,i)=>button('edition',i,esc(label),'studio-pick','aria-pressed="'+(group===i)+'"')).join('');
 const start=group*16;
 $('avatarPresets').innerHTML=crewNames.slice(start,start+16).map((name,i)=>button('preset',start+i,portrait(start+i)+'<span>'+esc(name)+'</span>','crew-option','aria-pressed="'+(avatarDraft===start+i)+'"')).join('');
}
function openAvatarPicker(){
 if(!overview.user||avatarSaving)return;
 $('avatarUser').textContent=overview.user;
 $('avatarUser').dataset.profile=overview.user;
 previewAvatar(avatarChoices.get(overview.user)??assignedAvatar(overview.user));
 showCrewEdition(Number.isInteger(avatarDraft)?Math.floor(avatarDraft/16):0);
 $('avatarDialog').showModal();
}
$('avatarEditions').onclick=e=>{
 const b=e.target.closest('[data-edition]');if(!b||avatarSaving)return;
 showCrewEdition(Number(b.dataset.edition));$('avatarEditions').querySelector('[aria-pressed="true"]').focus();
};
$('avatarPresets').onclick=e=>{
 const b=e.target.closest('[data-preset]');if(!b||avatarSaving)return;
 avatarRead++;$('avatarSave').disabled=false;previewAvatar(Number(b.dataset.preset));
};
$('avatarUpload').onchange=async e=>{
 const file=e.target.files[0];e.target.value='';if(!file||avatarSaving)return;
 const read=++avatarRead;
 $('avatarSave').disabled=true;
 try{
  if(file.size>12*1024*1024)throw Error('请选择小于 12 MB 的照片');
  const image=await createImageBitmap(file);
  try{
   const canvas=document.createElement('canvas');canvas.width=canvas.height=128;
   const ctx=canvas.getContext('2d'),side=Math.min(image.width,image.height);
   ctx.fillStyle='#202733';ctx.fillRect(0,0,128,128);
   ctx.drawImage(image,(image.width-side)/2,(image.height-side)/2,side,side,0,0,128,128);
   if(read===avatarRead)previewAvatar(canvas.toDataURL('image/jpeg',0.86));
  }finally{image.close()}
 }catch(error){if(read===avatarRead)$('avatarMessage').textContent=error.message.startsWith('请选择')?error.message:'图片无法读取，请选择 JPG、PNG 或 WebP。'}
 finally{if(read===avatarRead)$('avatarSave').disabled=false}
};
$('avatarDialog').addEventListener('close',()=>{avatarRead++;$('avatarSave').disabled=false});
$('avatarDialog').addEventListener('cancel',e=>{if(avatarSaving)e.preventDefault()});
$('avatarClose').onclick=()=>$('avatarDialog').close();
$('avatarSave').onclick=async()=>{
 if(avatarSaving)return;
 const draft=avatarDraft;
 avatarSaving=true;$('avatarControls').disabled=true;
 try{
  const payload=typeof draft==='number'?{preset:draft}:{photo:draft.split(',')[1]};
  const response=await atlasFetch('/api/avatar',{method:'POST',headers:{'Content-Type':'application/json','X-Atlas-Action':'avatar'},body:JSON.stringify(payload)});
  if(response.status===401){loginPage();return}
  if(!response.ok)throw Error();
  const {avatar}=await response.json();
  avatarRevision++;avatarChoices.set(overview.user,avatar);paintAvatars();$('avatarDialog').close();
 }catch{$('avatarMessage').textContent='头像保存失败，请重试。'}
 finally{avatarSaving=false;$('avatarControls').disabled=false}
};
document.addEventListener('click',e=>{if(e.target.closest('[data-editavatar]'))openAvatarPicker()});
setInterval(syncAvatars,60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)syncAvatars()});

const profileDialog=$('profileDialog');
let member=null, inventoryRequest=null, inventoryFetched=0;
function refreshMyInventory(){
 if(!inventoryRequest&&Date.now()-inventoryFetched>1800000){
  inventoryRequest=api('profile-inventory').then(data=>{
   inventoryFetched=Date.now();
   if(member?.user===overview.user){member.inventory=data;showInventory(data)}
  }).catch(()=>{
   if(member?.user===overview.user&&!member.inventory)$('profileInventory').textContent='资源统计暂时不可用';
  }).finally(()=>{inventoryRequest=null});
 }
 return inventoryRequest;
}
function showInventory(data){
 $('profileStamp').textContent=data?.datasetStamp?'共享目录 '+chartTime(data.datasetStamp)+' 更新':'';
 $('profileInventory').innerHTML=data?
  [['共享数据集',data.datasets],['个人训练任务目录',data.tasks],['个人 Checkpoints',data.checkpoints]].map(([label,n])=>'<div><strong>'+(n==null?'待同步':usageNumber.format(n))+'</strong><small>'+label+'</small></div>').join(''):
  '<small>暂无资源统计，首次同步该账号的工作台后显示。</small>';
}
function showBio(view){
 const mine=view.user===overview.user;
 $('profileBio').innerHTML='<p>'+esc(view.bio||'尚未填写个人简介')+'</p>'+(mine?button('editbio',1,view.bio?'编辑简介':'添加简介','link'):'');
}
function editMemberBio(view){
  $('profileBio').innerHTML='<textarea id="bioDraft" maxlength="240" rows="3" aria-label="个人简介" placeholder="写下你的研究方向或正在做的事">'+esc(view.bio||'')+'</textarea><div class="profile-edit-tools"><small>团队成员可见 · 最多 240 字</small>'+button('cancelbio',1,'取消','link')+button('savebio',1,'保存','outline')+'</div><small role="status" id="bioError"></small>';
  $('bioDraft').focus();
}
async function loadMember(view){
 try{
  const data=await api('profile',{user:view.user});
  if(member!==view)return;
  view.bio=data.bio;view.inventory=view.inventory||data.inventory;showBio(view);showInventory(view.inventory);
  if(view.editing){view.editing=false;editMemberBio(view)}
  if(view.user===overview.user&&!view.inventory)refreshMyInventory();
 }catch{if(member===view){$('profileBio').innerHTML=button('retryprofile',1,'简介读取失败 · 重试','link');if(!view.inventory)$('profileInventory').textContent='暂时无法读取资源统计'}}
}
async function showProfileStats(view,days){
 view.days=days;
 const request=++view.request;
 $('profilePeriods').innerHTML=[7,30].map(n=>control('profiledays',n,'近 '+n+' 天',n===days)).join('');
 $('profileStats').textContent='正在读取 Slurm 记账…';
 try{
  let data=usageView.cache.get(days);
  if(!data||Date.now()/1000-data.stamp>=300){data=await api('training-usage',{days});usageView.cache.set(days,data)}
  if(member!==view||view.request!==request)return;
  const r=data.users.find(row=>row.user===view.user);
  if(!r){$('profileStats').innerHTML='<small>近 '+days+' 天没有可见的训练记录。</small>';return}
  const rate=usageValue(r,'success');
  const metrics=[['GPU 卡时',usageFormat(usageValue(r,'gpu'),'gpu')],['累计时长',usageFormat(usageValue(r,'time'),'time')],['训练次数',r.jobs+' 次']];
  $('profileStats').innerHTML='<div class="profile-metrics">'+metrics.map(([label,value])=>'<div><small>'+label+'</small><strong>'+value+'</strong></div>').join('')+'</div>'+
   '<div class="profile-result"><span>成功率 <b>'+usageFormat(rate,'success')+'</b></span><i aria-hidden="true"><i style="width:'+(rate||0)+'%"></i></i><small>'+r.completed+' 成功 / '+r.ended+' 已结束</small></div>';
 }catch{if(member===view&&view.request===request)$('profileStats').innerHTML=button('profiledays',days,'训练统计读取失败 · 重试','link')}
}
function openMember(trigger,editing=false){
 const user=trigger.dataset.profile, source=trigger.getBoundingClientRect();
 const view={user,trigger,editing,request:0};member=view;
 $('profileName').textContent=user;$('profilePortrait').innerHTML=userAvatar(user,true);
 $('profileAvatarEdit').hidden=!editing;
 $('profileBio').textContent='正在读取简介…';$('profileStamp').textContent='';$('profileInventory').textContent='正在读取资源统计…';
 showProfileStats(view,7);
 profileDialog.showModal();
 if(!matchMedia('(prefers-reduced-motion: reduce)').matches){
  const target=profileDialog.getBoundingClientRect();
  const dx=source.left+source.width/2-target.left-target.width/2;
  const dy=source.top+source.height/2-target.top-target.height/2;
  const scale=Math.max(.035,Math.min(.25,source.width/target.width));
  profileDialog.animate([{transform:'translate('+dx+'px,'+dy+'px) scale('+scale+')',opacity:.4},{transform:'none',opacity:1}],{duration:720,easing:'cubic-bezier(.38,.03,.22,1)'});
 }
 loadMember(view);
}
document.addEventListener('click',e=>{
 const link=e.target.closest('[data-profile]');
 if(link&&link!==$('account')&&!profileDialog.open)openMember(link);
});
profileDialog.addEventListener('click',async e=>{
 const b=e.target.closest('button'),view=member;
 if(!view)return;
 if(e.target===profileDialog){
  const r=profileDialog.getBoundingClientRect();
  if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)profileDialog.close();
 }
 if(!b)return;
 const d=b.dataset;
 if(b.id==='profileClose')profileDialog.close();
 else if(d.profiledays)showProfileStats(view,Number(d.profiledays));
 else if(d.retryprofile)loadMember(view);
 else if(d.editbio){
  editMemberBio(view);
 }else if(d.cancelbio)showBio(view);
 else if(d.savebio){
  const editor=$('bioDraft');b.disabled=true;editor.disabled=true;b.previousElementSibling.disabled=true;
  try{
   const response=await atlasFetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json','X-Atlas-Action':'bio'},body:JSON.stringify({bio:editor.value})});
   if(response.status===401){loginPage();return}
   if(!response.ok)throw Error();
   const result=await response.json();
   if(member===view){view.bio=result.bio;showBio(view)}
  }catch{if(member===view){$('bioError').textContent='保存失败，请重试。';editor.disabled=false;b.disabled=false;b.previousElementSibling.disabled=false}}
 }
});
profileDialog.addEventListener('close',()=>{
 const link=member?.trigger;member=null;
 profileDialog.getAnimations().forEach(a=>a.cancel());
 if(link?.isConnected)link.focus({preventScroll:true});
});
setInterval(()=>{
 if(!member||document.hidden)return;
 const sample=usageView.cache.get(member.days);
 if(!sample||Date.now()/1000-sample.stamp>=300)showProfileStats(member,member.days);
},60000);
