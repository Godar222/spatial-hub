(()=>{const $=id=>document.getElementById(id);
function ensureUI(){
 const seg=document.querySelector('.segment');
 if(seg&&!seg.querySelector('[data-tab="nearby"]')){
   const b=document.createElement('button');b.dataset.tab='nearby';b.textContent='Nearby';
   seg.insertBefore(b,seg.children[1]||null);
   b.onclick=()=>{document.querySelectorAll('.segment button').forEach(x=>x.classList.remove('active'));b.classList.add('active');S.tab='nearby';renderList()};
 }
 const countsBox=document.querySelector('.counts');
 if(countsBox&&!$('trustedN')){
   const d=document.createElement('div');d.className='stat';d.innerHTML='<b id="trustedN">0</b><small>Trusted</small>';countsBox.appendChild(d);
   countsBox.style.gridTemplateColumns='repeat(5,1fr)';
 }
 if(!document.getElementById('v060style')){
   const st=document.createElement('style');st.id='v060style';
   st.textContent='.trustgood{color:#30d158}.trustneed{color:#ff9f0a}.connstate{margin-top:6px;padding:7px 9px;border-radius:10px;background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.08)}';
   document.head.appendChild(st);
 }
}
const oldFiltered=filtered;
filtered=function(){
 if(S.tab!=='nearby')return oldFiltered();
 let arr=S.nodes.filter(n=>['wifi','ble','agent'].includes(n.kind)&&S.layers[n.kind]!==false);
 let q=$('search').value.trim().toLowerCase();if(q)arr=arr.filter(n=>`${n.name||''} ${n.ip||''} ${n.address||''}`.toLowerCase().includes(q));
 return arr.sort((a,b)=>(a.distance?.approx_m??999)-(b.distance?.approx_m??999))
};
renderList=function(){
 ensureUI();let arr=filtered();$('phoneCard').style.display=S.tab==='agent'?'block':'none';$('list').innerHTML='';
 if(!arr.length){$('list').innerHTML='<div class="empty">Ничего не найдено</div>';return}
 arr.forEach(n=>{let b=document.createElement('button');b.className='item '+(S.selected===n.id?'selected':'');let meta=n.ip||n.address||n.connection||'';let right=n.distance?.available?'≈'+n.distance.approx_m+'m':'';let ts=n.trusted?`Trusted · ${n.connection_state||'ready'}`:'Needs pairing';b.innerHTML=`<div class="row"><b>${ESC(n.name)}</b><span class="muted">${ESC(right)}</span></div><div class="muted">${ESC(meta)} · ${ESC(ts)}</div>`;b.onclick=()=>selectNode(n);b.ondblclick=()=>{S.focus=n.id;selectNode(n)};$('list').appendChild(b)})
};
const oldCounts=counts;
counts=function(){oldCounts();ensureUI();if($('trustedN'))$('trustedN').textContent=S.nodes.filter(n=>n.trusted).length};
selectNode=function(n){
 S.selected=n.id;let d=n.distance||{},sig=n.kind==='wifi'&&n.signal!=null?`${n.signal}% · RSSI≈${n.rssi} dBm`:n.kind==='ble'&&n.rssi!=null?`${n.rssi} dBm`:'—';
 let inspect=n.kind==='lan'&&n.ip?`<button class="btn" onclick="inspectIP(${JSON.stringify(n.ip)})">Inspect services</button>`:'';
 let conn=n.connection_state||'discovered',trustLabel=n.trusted?'Forget':'Trust + Auto-connect';
 $('detail').innerHTML=`<div class="row"><div><b>${ESC(n.name)}</b><div class="muted">${ESC(n.kind)} · ${ESC(n.connection||'')}</div></div><span class="badge ${n.trusted?'trustgood':'trustneed'}">${n.trusted?'Trusted':'Needs pairing'}</span></div>
 <div class="detailGrid" style="margin-top:10px"><div>IP</div><div>${ESC(n.ip||'—')}</div><div>Address</div><div>${ESC(n.address||'—')}</div><div>Signal</div><div>${ESC(sig)}</div><div>Distance</div><div>${d.available?'≈ '+ESC(d.approx_m)+' m':'not measured'}</div><div>Confidence</div><div>${ESC(confidence(n))}</div></div>
 <div class="connstate"><b>Connection</b><div class="muted">${ESC(conn)}${n.connection_detail?' · '+ESC(n.connection_detail):''}</div></div>
 <div class="warn" style="margin-top:7px">${ESC(d.note||'')}</div>${spark(n.id)}
 <div id="services"></div><div class="actions"><button class="btn" onclick="toggleFocus(${JSON.stringify(n.id)})">${S.focus===n.id?'Exit focus':'Focus'}</button><button class="btn ${n.trusted?'':'primary'}" onclick="trustDevice(${JSON.stringify(n.id)},${!n.trusted})">${trustLabel}</button><button class="btn" onclick="connectDevice(${JSON.stringify(n.id)})" ${n.trusted?'':'disabled'}>Connect / Inspect</button>${inspect}</div>
 <div class="muted" style="margin-top:8px">Auto-connect uses only protocols the device exposes. It never bypasses Wi-Fi/Bluetooth pairing, passwords, or OS permissions. Spatial Nodes can reconnect automatically after the first pairing.</div>`;
 renderList();draw()
};
window.trustDevice=async(id,wanted)=>{try{let r=await fetch('/api/trust',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,trusted:wanted,auto_connect:true})}),x=await r.json();if(!r.ok)throw Error(x.error||'Trust failed');await poll()}catch(e){alert(e.message)}};
window.connectDevice=async id=>{try{let r=await fetch('/api/action/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}),x=await r.json();if(!r.ok)throw Error(x.error||x.state||'Connection unavailable');await poll()}catch(e){alert(e.message)}};
ensureUI();
})();