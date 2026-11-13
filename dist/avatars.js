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
 return '<span class="crew-person">'+userAvatar(user,editable)+'<span>'+esc(user)+'</span></span>';
}
function paintAvatars(){
 if(!overview.user)return;
 $('initials').innerHTML=userAvatar(overview.user);
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
