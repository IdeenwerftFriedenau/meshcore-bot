'use strict';
// Dauerhaftes Archiv der rbb-88.8-Nachrichten (ungerade Stunden, DAB+ 7D) auf der Seite „Radio & Aufnahmen“.
window.RbbArchiv=(()=>{
 let offset=0,data=null;
 const q=s=>document.querySelector(s);
 const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const when=t=>{const d=new Date(t);return isNaN(d)?t:d.toLocaleString('de-DE',{timeZone:'Europe/Berlin',weekday:'short',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});};
 function panel(){return `<section class="panel" id="rbb-archiv"><h2>rbb-Nachrichten-Archiv</h2><p>Jede ungerade volle Stunde nimmt der Pi die Nachrichten von rbb 88.8 über DAB+ (Kanal 7D) auf. Diese Aufnahmen werden dauerhaft gespeichert, zusammen mit den erkannten Schlagzeilen und dem Text.</p><p id="rbb-sum" class="funk-time"></p><div id="rbb-list"><p class="empty">Wird geladen …</p></div><div class="send-row"><button type="button" id="rbb-newer" disabled>◀ Neuere</button><button type="button" id="rbb-older" disabled>Ältere ▶</button></div></section>`;}
 function render(){if(!data||!q('#rbb-list'))return;
  q('#rbb-sum').textContent=data.total?`${data.total} Sendungen gespeichert · ${(data.bytes/1048576).toFixed(0)} MB`:'';
  q('#rbb-list').innerHTML=data.items.map(r=>{const [y,m,n]=r.id.split('/');return `<article class="recording rbb-item"><div class="recording-top"><span class="sourceicon">◉</span><div><h3>rbb 88.8 · ${e(when(r.time))} Uhr</h3><p>${r.audio?(r.bytes?(r.bytes/1048576).toFixed(1).replace('.',',')+' MB':'Ton gespeichert'):'kein Ton'}${r.error?' · Auswertung: '+e(r.error):''}</p></div></div>
   ${r.audio?`<audio controls preload="none" src="/rbb-archiv/${encodeURIComponent(y)}/${encodeURIComponent(m)}/${encodeURIComponent(n)}.mp3"></audio>`:''}
   ${r.headlines.length?`<ul class="rbb-heads">${r.headlines.map(h=>`<li>${e(h.replace(/^rbb88\.8 \d\d:\d\d: /,''))}</li>`).join('')}</ul>`:''}
   ${r.transcript?`<details><summary>Text (automatisch erkannt, kann Fehler enthalten)</summary><p class="rbb-text">${e(r.transcript)}</p></details>`:''}</article>`;}).join('')||'<p class="empty">Noch keine Aufnahme im Archiv. Die erste kommt zur nächsten ungeraden vollen Stunde.</p>';
  q('#rbb-newer').disabled=offset<=0;q('#rbb-older').disabled=offset+data.limit>=data.total;}
 async function load(){try{const r=await fetch('/api/rbb-archiv?offset='+offset,{cache:'no-store'});if(!r.ok)throw Error();data=await r.json();}catch(_){if(q('#rbb-list'))q('#rbb-list').innerHTML='<p class="empty">Archiv gerade nicht lesbar.</p>';return;}render();}
 function mount(){const main=q('#main');if(!main||q('#rbb-archiv'))return;main.insertAdjacentHTML('beforeend',panel());
  q('#rbb-newer').addEventListener('click',()=>{offset=Math.max(0,offset-(data?data.limit:24));load();q('#rbb-archiv').scrollIntoView();});
  q('#rbb-older').addEventListener('click',()=>{offset+=data?data.limit:24;load();q('#rbb-archiv').scrollIntoView();});offset=0;load();}
 return {mount};
})();
