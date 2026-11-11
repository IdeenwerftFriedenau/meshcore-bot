'use strict';
// Funk-Suchlauf: weitere Funkquellen (UKW, Flugfunk, PMR, 433/868 MHz) mit rtl_power, nur auf Knopfdruck.
window.FunkScan=(()=>{
 let timer=null,data=null,msg='',busy=false,boxes=null,audio=null,lmsg='';
 let unlocked=false;const SILENT=(()=>{const n=400,b=new ArrayBuffer(44+n*2),v=new DataView(b),w=(o,t)=>[...t].forEach((c,i)=>v.setUint8(o+i,c.charCodeAt(0)));w(0,'RIFF');v.setUint32(4,36+n*2,true);w(8,'WAVEfmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);v.setUint32(28,16000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,'data');v.setUint32(40,n*2,true);try{return URL.createObjectURL(new Blob([b],{type:'audio/wav'}));}catch(_){return '';}})();
 const q=s=>document.querySelector(s);
 const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const when=t=>t?new Date(t).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'}):'';
 function page(){return `<div class="pagehead"><div><span class="eyebrow">RTL-SDR · WEITERE FUNKQUELLEN</span><h1>Funk-Suchlauf</h1><p>Misst auf Knopfdruck, was auf CB-Funk, UKW, Flugfunk, Amateurfunk, Freenet, PMR446, Wettersonden, Wettersatelliten, 433 MHz und 868 MHz gerade los ist. Der DAB+-Suchlauf bleibt unter „DAB+ ASA“.</p></div></div>
 <section class="panel"><h2>Suchlauf starten</h2>
 <p>Der Empfänger kann nur eine Sache gleichzeitig. Für die Messung wird der DAB-Monitor kurz angehalten (etwa 15 Sekunden je Band) und danach automatisch wieder gestartet. Das MeshCore-Funkgerät ist davon nicht betroffen. Zwischen xx:55 und xx:06 haben die DAB-Nachrichten Vorrang.</p>
 <div class="funk-bands" id="funk-bands"></div>
 <div class="send-row"><button class="primary" id="funk-start" type="button">Suchlauf starten</button><button id="funk-cancel" type="button" disabled>Abbrechen</button><label class="funk-force"><input type="checkbox" id="funk-force"> trotzdem starten</label></div>
 <div class="funk-bar"><i id="funk-bar"></i></div><p id="funk-state" role="status">Status wird geladen …</p>
 <details><summary>Protokoll</summary><pre id="funk-log">–</pre></details></section>
 <section class="panel"><h2>Funksensoren in der Nähe <small class="funk-time" id="sens-time"></small></h2>
 <p>Viele Geräte funken ihre Messwerte auf 433 und 868 MHz: Wetterstationen, Außenthermometer, Regenmesser, Rauchmelder. rtl_433 entschlüsselt sie. Die Suche lauscht je etwa <span id="sens-sec">75</span> Sekunden auf beiden Frequenzen. Der DAB-Monitor ist so lange aus. Die Werte bleiben auf dem Pi und werden nicht weitergegeben. Von Reifendrucksensoren vorbeifahrender Autos wird nur die Anzahl gezählt.</p>
 <div class="send-row"><button class="primary" id="sens-start" type="button">Funksensoren suchen</button><span id="sens-note"></span></div>
 <div id="sens-out"></div></section>
 <section class="panel"><h2>Reinhören</h2>
 <p>Band wählen, dann eine gefundene Frequenz anklicken. Der Ton kommt direkt in diesen Browser. Auf 433 MHz, 868 MHz und bei den Wettersatelliten wird nur Datenfunk gesendet, dort hörst du Pakete als Zirpen oder Rauschen. Flugfunk ist in Deutschland nicht zum Mithören freigegeben (§ 5 TDDDG) und deshalb gesperrt. Beim Hören ist der DAB-Monitor aus, nach höchstens 10 Minuten oder beim Beenden läuft er wieder.</p>
 <div class="funk-tabs" id="funk-tabs" role="tablist"></div>
 <div class="send-row"><label class="funk-freq">Frequenz in MHz<input id="funk-mhz" type="number" step="0.0125" min="87.5" max="446.2" placeholder="z. B. 88.8"></label><button class="primary" id="funk-listen" type="button">Anhören</button><button id="funk-listen-stop" type="button" disabled>Hören beenden</button></div>
 <p id="funk-listen-state" role="status"></p>
 <div id="funk-suggest" class="funk-suggest"></div></section>
 <section class="panel"><h2>Ergebnis <small class="funk-time" id="funk-time"></small></h2><div id="funk-out"><p class="empty">Noch kein Suchlauf.</p></div></section>`;}
 function chart(sp,floor,signals){if(!sp||sp.length<2)return'';const W=1000,H=160,x0=sp[0][0],x1=sp[sp.length-1][0];
  const ys=sp.map(p=>p[1]),lo=Math.min(...ys),hi=Math.max(...ys)+2,X=f=>(f-x0)/(x1-x0||1)*W,Y=v=>H-20-(v-lo)/(hi-lo||1)*(H-30);
  const path=sp.map((p,i)=>(i?'L':'M')+X(p[0]).toFixed(1)+' '+Y(p[1]).toFixed(1)).join('');
  const marks=signals.slice(0,12).map(s=>`<line x1="${X(s.mhz).toFixed(1)}" x2="${X(s.mhz).toFixed(1)}" y1="6" y2="${H-20}" class="funk-mark"/>`).join('');
  return `<svg class="funk-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Spektrum ${x0} bis ${x1} MHz"><line x1="0" x2="${W}" y1="${Y(floor).toFixed(1)}" y2="${Y(floor).toFixed(1)}" class="funk-floor"/>${marks}<path d="${path}" class="funk-line"/></svg><div class="funk-axis"><span>${x0} MHz</span><span>${x1} MHz</span></div>`;}
 function listenable(m){return data&&Object.values(data.listen_bands||{}).some(b=>m>=b[1]&&m<=b[2]);}
 function renderListen(){const st=q('#funk-listen-state');if(!st||!data)return;const l=data.listen;
  q('#funk-listen-stop').disabled=!l&&!(audio&&!audio.paused);q('#funk-listen').disabled=data.status.running;
  if(lmsg)st.textContent=lmsg;else if(l){const left=Math.max(0,Math.round((new Date(l.started).getTime()/1000+600-Date.now()/1000)/60));st.textContent=`Du hörst ${Number(l.mhz).toFixed(3)} MHz (${l.name}). Endet automatisch in etwa ${left} Min.`;}else st.textContent='';
  document.querySelectorAll('.funk-play').forEach(b=>b.classList.toggle('on',!!l&&Math.abs(l.mhz-b.dataset.mhz)<0.0005));}
 let lband=null;try{lband=sessionStorage.getItem('funk-band');}catch(_){}
 function suggestions(){const b=data&&data.results.bands;const r=b&&b.bands&&b.bands[lband];if(!r||!data.listen_bands[lband])return[];
  return (r.signals||[]).map(x=>({mhz:Number(x.mhz),db:x.db,band:lband,label:x.label})).sort((a,c)=>a.mhz-c.mhz);}
 function renderTabs(){const el=q('#funk-tabs');if(!el)return;const b=data.results.bands&&data.results.bands.bands||{};
  if(!lband||!data.listen_bands[lband])lband=Object.keys(data.listen_bands).find(k=>b[k]&&b[k].signals.length)||'ukw';
  el.innerHTML=Object.entries(data.bands).map(([k,n])=>{const ok=!!data.listen_bands[k],forb=(data.listen_forbidden||[]).includes(k),cnt=b[k]?b[k].signals.length:null;
   return `<button type="button" role="tab" class="funk-tab${k===lband?' on':''}" data-band="${e(k)}" aria-selected="${k===lband}" ${ok?'':(forb?'disabled title="Mithören nicht erlaubt (§ 5 TDDDG)"':'disabled title="Datenfunk, hier wird nur gemessen"')}>${e(n)}<small>${ok?(cnt===null?'nicht gemessen':cnt+(cnt===1?' Signal':' Signale')):(forb?'gesperrt':'nur Messung')}</small></button>`;}).join('');}
 function renderSuggest(){renderTabs();const el=q('#funk-suggest');if(!el)return;const list=suggestions(),lb=data.listen_bands[lband],m=q('#funk-mhz');
  if(lb&&m){m.min=lb[1];m.max=lb[2];m.step=lband==='ukw'?0.1:0.0125;m.placeholder=lband==='ukw'?'z. B. 88.8':String(lb[1]);}
  const b=data.results.bands&&data.results.bands.bands||{};
  if(!list.length){el.innerHTML=`<p class="empty">${b[lband]?'In diesem Band wurde beim letzten Suchlauf nichts gefunden. Frequenz oben eintippen oder später neu suchen.':'Dieses Band beim Suchlauf oben anhaken, dann erscheinen hier die gefundenen Frequenzen.'}</p>`;return;}
  el.innerHTML=`<div class="funk-chips">${list.map(x=>`<button type="button" class="funk-play funk-chip" data-mhz="${e(x.mhz)}" title="${e(x.db)} dB über Rauschen${x.label?' · '+e(x.label):''}">${x.mhz.toFixed(lband==='ukw'?1:3)}<small>${'▮'.repeat(Math.max(1,Math.min(5,Math.round(x.db/6))))}</small></button>`).join('')}</div>`
   +`<div class="send-row funk-step"><button type="button" id="funk-prev">◀ Vorherige</button><button type="button" id="funk-next">Nächste ▶</button></div>`;}
 function step(dir){const list=suggestions();if(!list.length)return;const cur=data.listen?Number(data.listen.mhz):Number(q('#funk-mhz').value);
  let i=list.findIndex(x=>Math.abs(x.mhz-cur)<0.0005);i=i<0?(dir>0?0:list.length-1):(i+dir+list.length)%list.length;listen(list[i].mhz);}
 let suggestSig='';
 function render(){if(!data||!q('#funk-state'))return;const s=data.status;
  const sig=JSON.stringify(data.results.bands&&data.results.bands.time);if(sig!==suggestSig){suggestSig=sig;renderSuggest();}
  renderListen();
  if(!boxes){boxes=true;q('#funk-bands').innerHTML=Object.entries(data.bands).map(([k,n])=>`<label class="region"><input type="checkbox" value="${e(k)}" ${data.default_bands.includes(k)?'checked':''}> ${e(n)}</label>`).join('');}
  q('#funk-start').disabled=s.running||busy;q('#funk-cancel').disabled=!s.running;
  q('#funk-bar').style.width=Math.round((s.running?s.progress:(s.started?1:0))*100)+'%';
  q('#funk-state').innerHTML=msg?e(msg):s.running?'Läuft: '+e(s.step):s.error?'<span class="funk-err">'+e(s.error)+'</span>':data.quiet?e(data.quiet):s.started?'Fertig.':'Bereit.';
  q('#funk-log').textContent=(s.log||[]).join('\n')||'–';
  renderSensors();
  const b=data.results.bands;if(!b)return;
  q('#funk-time').textContent='· '+when(b.time)+(b.complete?'':' (unvollständig)');
  q('#funk-out').innerHTML=Object.values(b.bands).map(r=>`<div class="funk-band"><h3>${e(r.name)} <span class="badge ${r.signals.length?'on':''}">${r.signals.length} ${r.signals.length===1?"Signal":"Signale"}</span></h3><p>${e(r.hint)} Rauschboden ${e(r.floor)} dB.</p>${chart(r.spectrum,r.floor,r.signals)}
   ${r.signals.length?`<div class="table-wrap"><table><thead><tr><th>MHz</th><th>über Rauschen</th><th>Breite</th><th>Auftreten</th><th>Hinweis</th><th></th></tr></thead><tbody>${r.signals.slice(0,15).map(x=>`<tr><td>${Number(x.mhz).toFixed(3)}</td><td>${e(x.db)} dB</td><td>${e(x.width_khz)} kHz</td><td>${e(x.dauer)}</td><td>${e(x.label)}</td><td>${listenable(x.mhz)?`<button type="button" class="funk-play" data-mhz="${e(x.mhz)}">▶ Anhören</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:''}</div>`).join('')||'<p class="empty">Keine Bänder gemessen.</p>';renderListen();}
 async function poll(){try{const r=await fetch('/api/funk/status',{cache:'no-store'});if(!r.ok)throw Error();data=await r.json();}catch(_){msg='Suchlauf-Status nicht erreichbar.';}
  render();clearTimeout(timer);if(q('#funk-state'))timer=setTimeout(poll,data&&(data.status.running||data.listen)?2000:10000);}
 async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'Nicht möglich');return j;}
 const SENS_LABEL={temperature_C:['Temperatur','°C'],temperature_F:['Temperatur','°F'],humidity:['Feuchte','%'],moisture:['Bodenfeuchte','%'],
  pressure_hPa:['Luftdruck','hPa'],wind_avg_km_h:['Wind','km/h'],wind_max_km_h:['Böen','km/h'],wind_avg_m_s:['Wind','m/s'],wind_max_m_s:['Böen','m/s'],
  wind_dir_deg:['Windrichtung','°'],rain_mm:['Regen gesamt','mm'],rain_rate_mm_h:['Regen','mm/h'],uv:['UV',''],light_lux:['Licht','lx'],
  battery_ok:['Batterie',''],button:['Taste',''],state:['Zustand',''],cmd:['Befehl',''],code:['Code',''],alarm:['Alarm',''],power_W:['Leistung','W'],energy_kWh:['Energie','kWh']};
 function sensVal(k,v){const l=SENS_LABEL[k];if(k==='battery_ok')return 'Batterie '+(v?'ok':'schwach');
  const n=typeof v==='number'?String(Math.round(v*10)/10).replace('.',','):String(v);return l?`${l[0]} ${n}${l[1]?' '+l[1]:''}`:`${k} ${n}`;}
 let sensSig='';
 function renderSensors(){const out=q('#sens-out');if(!out)return;const s=data.status,r=data.results.sensors;
  q('#sens-sec').textContent=Math.max(1,Math.round((data.sensor_seconds||150)/2));
  q('#sens-start').disabled=s.running||busy||data.rtl_433===false;
  q('#sens-note').textContent=data.rtl_433===false?'rtl_433 ist auf dem Pi noch nicht installiert.':(s.running&&s.kind==='sensors'?'Läuft: '+s.step:'');
  const sig=JSON.stringify(r&&r.time);if(sig===sensSig)return;sensSig=sig;
  if(!r){out.innerHTML='<p class="empty">Noch keine Sensor-Suche.</p>';return;}
  q('#sens-time').textContent='· '+when(r.time)+(r.complete?'':' (unvollständig)');
  const now=r.devices.filter(d=>d.now),old=r.devices.filter(d=>!d.now);
  const row=d=>`<tr><td><b>${e(d.model)}</b><small>${[d.id!==''&&d.id!=null?'Kennung '+e(d.id):'',d.channel!==''&&d.channel!=null?'Kanal '+e(d.channel):'',e(d.band)+' MHz'].filter(Boolean).join(' · ')}</small></td><td>${Object.entries(d.values||{}).map(([k,v])=>e(sensVal(k,v))).join('<br>')||'–'}</td><td>${e(d.count)}×${d.rssi!=null?'<small>'+e(d.rssi)+' dB</small>':''}</td><td>${e(when(d.last))}${d.seen>1?'<small>schon '+e(d.seen)+'× gesehen</small>':''}</td></tr>`;
  const table=list=>`<div class="table-wrap"><table><thead><tr><th>Gerät</th><th>Messwerte</th><th>Pakete</th><th>Zuletzt</th></tr></thead><tbody>${list.map(row).join('')}</tbody></table></div>`;
  out.innerHTML=(now.length?`<p>${now.length} ${now.length===1?'Gerät':'Geräte'} bei der letzten Suche empfangen.</p>`+table(now):'<p class="empty">Bei der letzten Suche wurde kein Sensor empfangen. Viele senden nur alle 30 bis 60 Sekunden, ein zweiter Versuch lohnt sich.</p>')
   +(old.length?`<details><summary>Früher gesehene Geräte (${old.length})</summary>${table(old)}</details>`:'');}
 async function startSensors(){busy=true;msg='';render();
  try{await post('/api/funk/scan',{kind:'sensors',bands:[],force:q('#funk-force').checked});}catch(err){msg=err.message;}finally{busy=false;poll();}}
 async function start(){busy=true;msg='';render();const bands=[...document.querySelectorAll('#funk-bands input:checked')].map(x=>x.value);
  try{if(!bands.length)throw Error('Bitte mindestens ein Band auswählen.');await post('/api/funk/scan',{bands,force:q('#funk-force').checked});}catch(err){msg=err.message;}finally{busy=false;poll();}}
 async function listen(mhz){lmsg='';const m=Number(mhz);if(!m){lmsg='Bitte eine Frequenz in MHz eingeben.';renderListen();return;}
  if(!audio){audio=new Audio();audio.addEventListener('error',()=>{if(audio.src&&!audio.src.startsWith('blob:')){lmsg='Ton abgebrochen. Status siehe Protokoll.';poll();}});audio.addEventListener('ended',()=>{if(!audio.src.startsWith('blob:'))poll();});}
  // Safari: Wiedergabe nur direkt im Klick erlaubt. Den Player deshalb sofort mit kurzer Stille freischalten,
  // danach darf er auch nach der Rückfrage beim Pi spielen.
  if(!unlocked&&SILENT){unlocked=true;audio.src=SILENT;audio.play().catch(()=>{});}
  q('#funk-mhz').value=m;lmsg='Stelle '+m.toFixed(3)+' MHz ein …';renderListen();
  try{const j=await post('/api/funk/listen',{mhz:m,force:q('#funk-force').checked});audio.src=j.url;lmsg='';audio.play().catch(err=>{if(err.name==='AbortError')return;lmsg='Ton lässt sich hier nicht abspielen ('+(err.name||'Fehler')+').';renderListen();});}catch(err){lmsg=err.message||'Ton lässt sich nicht abspielen.';}
  setTimeout(poll,1500);renderListen();}
 async function stopListen(){if(audio){audio.pause();audio.removeAttribute('src');audio.load();}lmsg='';try{await post('/api/funk/listen/stop',{});}catch(_){}setTimeout(poll,1500);}
 async function cancel(){try{await post('/api/funk/cancel',{});}catch(err){msg=err.message;}poll();}
 function mount(){boxes=null;msg='';lmsg='';suggestSig='';sensSig='';
  q('#funk-tabs').addEventListener('click',ev=>{const t=ev.target.closest('.funk-tab');if(!t||t.disabled)return;lband=t.dataset.band;try{sessionStorage.setItem('funk-band',lband);}catch(_){}renderSuggest();renderListen();});
  q('#funk-suggest').addEventListener('click',ev=>{const b=ev.target.closest('.funk-play');if(b)return listen(b.dataset.mhz);if(ev.target.closest('#funk-next'))step(1);if(ev.target.closest('#funk-prev'))step(-1);});q('#funk-start').addEventListener('click',start);q('#sens-start').addEventListener('click',startSensors);q('#funk-cancel').addEventListener('click',cancel);
  q('#funk-listen').addEventListener('click',()=>listen(q('#funk-mhz').value));q('#funk-listen-stop').addEventListener('click',stopListen);
  q('#funk-mhz').addEventListener('keydown',ev=>{if(ev.key==='Enter')listen(q('#funk-mhz').value);});
  q('#funk-out').addEventListener('click',ev=>{const b=ev.target.closest('.funk-play');if(b)listen(b.dataset.mhz);});poll();}
 function unmount(){clearTimeout(timer);timer=null;if(audio&&!audio.paused)stopListen();}
 return {page,mount,unmount};
})();
