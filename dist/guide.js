const menuTriggers=[$('initials'),$('account')], accountMenu=$('accountMenu');
let menuOpener=menuTriggers[0];
function closeAccountMenu(focus=false){
 accountMenu.hidden=true;menuTriggers.forEach(b=>b.setAttribute('aria-expanded','false'));
 if(focus)menuOpener.focus({preventScroll:true});
}
function openAccountMenu(trigger){
 menuOpener=trigger;accountMenu.hidden=false;menuTriggers.forEach(b=>b.setAttribute('aria-expanded','true'));
 $('menuProfile').focus({preventScroll:true});
}
for(const trigger of menuTriggers){
 trigger.onclick=()=>accountMenu.hidden?openAccountMenu(trigger):closeAccountMenu(true);
 trigger.onkeydown=e=>{if(e.key==='ArrowDown'){e.preventDefault();openAccountMenu(trigger)}};
}
accountMenu.onkeydown=e=>{
 const items=[...accountMenu.querySelectorAll('a,button')],index=items.indexOf(document.activeElement);
 if(e.key==='Escape'){e.preventDefault();closeAccountMenu(true)}
 if(['ArrowUp','ArrowDown','Home','End'].includes(e.key)){
  e.preventDefault();
  const next=e.key==='Home'?0:e.key==='End'?items.length-1:(index+(e.key==='ArrowUp'?-1:1)+items.length)%items.length;
  items[next].focus();
 }
};
document.addEventListener('pointerdown',e=>{if(!e.target.closest('.account-area'))closeAccountMenu()});
document.addEventListener('focusin',e=>{if(!e.target.closest('.account-area'))closeAccountMenu()});
$('menuProfile').onclick=()=>{closeAccountMenu();if(overview.user)openMember($('account'))};
$('menuEdit').onclick=()=>{closeAccountMenu();if(overview.user)openMember($('account'),true)};
$('menuSettings').onclick=()=>{closeAccountMenu(true);$('settingsDialog').showModal();$('theme').focus()};
$('settingsClose').onclick=()=>$('settingsDialog').close();
$('settingsDialog').onclose=()=>menuOpener.focus({preventScroll:true});
accountMenu.querySelector('a').onclick=()=>{closeAccountMenu();if(location.hash==='#guide')renderGuide(true)};

let guideObserver=null,guideFrame=0;
const guideURLs=new Map();
function leaveGuide(){
 guideObserver?.disconnect();guideObserver=null;
 window.removeEventListener('scroll',scheduleGuideCurrent);
 cancelAnimationFrame(guideFrame);guideFrame=0;
 if($('guideZoom').open)$('guideZoom').close();
}
function scheduleGuideCurrent(){
 if(guideFrame)return;
 guideFrame=requestAnimationFrame(()=>{
  guideFrame=0;
  const sections=[...document.querySelectorAll('.book-chapter')];
  if(!sections.length)return;
  const top=document.querySelector('header').getBoundingClientRect().bottom+50;
  const active=sections.filter(s=>s.getBoundingClientRect().top<=top).pop()||sections[0];
  document.querySelectorAll('.book-toc a').forEach(a=>{
   if(a.hash==='#guide/'+active.dataset.chapter)a.setAttribute('aria-current','location');else a.removeAttribute('aria-current');
  });
 });
}
async function loadGuideImage(img){
 const name=img.dataset.image;
 if(!guideURLs.has(name)){
  const pending=atlasFetch('/api/guide-images/'+name+'.webp').then(async r=>{
   if(r.status===401){loginPage();throw Error()}
   if(!r.ok)throw Error();
   return URL.createObjectURL(await r.blob());
  }).catch(e=>{guideURLs.delete(name);throw e});
  guideURLs.set(name,pending);
 }
 try{img.src=await guideURLs.get(name);return true}
 catch{img.alt='截图加载失败，点击重试';return false}
}
function renderGuide(navigation){
 const first=!document.querySelector('.handbook');
 if(first){
  $('content').replaceChildren($('guideTemplate').content.cloneNode(true));
  guideObserver=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){guideObserver.unobserve(entry.target);loadGuideImage(entry.target)}},{rootMargin:'700px'});
  document.querySelectorAll('.book-shot img').forEach(img=>guideObserver.observe(img));
  window.addEventListener('scroll',scheduleGuideCurrent,{passive:true});
 }
 if(navigation||first){
  const chapter=location.hash.split('/')[1],section=[...document.querySelectorAll('.book-chapter')].find(s=>s.dataset.chapter===chapter);
  if(section)section.scrollIntoView({block:'start'});else window.scrollTo(0,0);
  scheduleGuideCurrent();
 }
}
document.addEventListener('click',async e=>{
 const photo=e.target.closest('.book-shot'),copy=e.target.closest('[data-guide-link]');
 if(copy){await copyPath(copy,location.origin+location.pathname+'#guide/'+copy.dataset.guideLink);return}
 if(!photo)return;
 const img=photo.querySelector('img');
 if(!await loadGuideImage(img)||!photo.isConnected)return;
 $('guideLarge').src=img.src;$('guideLarge').alt=photo.getAttribute('aria-label');
 $('guideZoomTitle').textContent=photo.closest('figure').querySelector('figcaption').textContent;
 $('guideZoom').showModal();
});
$('guideZoomClose').onclick=()=>$('guideZoom').close();
$('guideZoom').addEventListener('click',e=>{if(e.target===$('guideZoom')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close()}});
