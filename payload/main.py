from __future__ import annotations

import base64, io, json, math, os, secrets, socket, threading, time, webbrowser, subprocess, sys, urllib.request
from collections import deque
from pathlib import Path
from flask import Flask, jsonify, request, Response
from waitress import serve
import psutil, qrcode, pystray
from PIL import Image, ImageDraw

PORT=8765
ROOT=Path(os.environ.get('SPATIAL_HUB_ROOT', Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'SpatialHub'))
DATA=ROOT/'data'; LOGS=ROOT/'logs'; DATA.mkdir(parents=True,exist_ok=True); LOGS.mkdir(parents=True,exist_ok=True)
STATE_FILE=DATA/'state.json'; VERSION_FILE=ROOT/'app'/'version.json'; LOG_FILE=LOGS/'app.log'
MANIFEST=os.environ.get('SPATIAL_HUB_MANIFEST','https://raw.githubusercontent.com/Godar222/spatial-hub/main/update/manifest.json')
LAUNCHER=os.environ.get('SPATIAL_HUB_LAUNCHER','')
app=Flask(__name__); lock=threading.RLock(); stop=threading.Event(); token=secrets.token_urlsafe(24); code=f'{secrets.randbelow(1000000):06d}'
state={'nodes':{},'events':deque(maxlen=300),'started':time.time(),'update':None}

def log(s):
    try:
        with LOG_FILE.open('a',encoding='utf-8') as f:f.write(f"[{time.strftime('%F %T')}] {s}\n")
    except:pass

def ver():
    try:return json.loads(VERSION_FILE.read_text(encoding='utf-8')).get('version','dev')
    except:return 'dev'

def lip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));x=s.getsockname()[0];s.close();return x
    except:return '127.0.0.1'
HOST=lip()

def event(text):
    with lock:state['events'].appendleft({'t':time.time(),'text':text})

def save():
    try:
        with lock:d={'nodes':state['nodes'],'events':list(state['events'])[:100]}
        STATE_FILE.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as e:log(e)

def load():
    try:
        if STATE_FILE.exists():
            d=json.loads(STATE_FILE.read_text(encoding='utf-8'));state['nodes']=d.get('nodes',{})
            for n in state['nodes'].values():n['online']=False
            for e in d.get('events',[]):state['events'].append(e)
    except Exception as e:log(e)
load()

def pos(key,kind='node'):
    import hashlib
    h=hashlib.sha256(key.encode()).digest();a=int.from_bytes(h[:4],'big')/2**32*math.tau
    r={'hub':0,'phone':2.1,'wifi':4.0,'ble':3.2,'lan':5.0}.get(kind,4.2)
    return {'x':math.cos(a)*r,'z':math.sin(a)*r,'y':(h[4]/255-.5)*.8,'uncertainty':0.2 if kind=='hub' else 2.5}

def hub():
    b=psutil.sensors_battery();m=psutil.virtual_memory()
    return {'id':'hub','kind':'hub','name':socket.gethostname(),'online':True,'last_seen':time.time(),'position':pos('hub','hub'),'telemetry':{'cpu':psutil.cpu_percent(None),'ram':m.percent,'battery':getattr(b,'percent',None)}}

DASH=r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spatial Hub</title><style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#07101a;color:#eef6ff;font:14px system-ui}.top{padding:14px 18px;border-bottom:1px solid #26384e;display:flex;justify-content:space-between}.layout{display:grid;grid-template-columns:1fr 340px;min-height:700px}canvas{width:100%;height:700px;background:radial-gradient(circle,#122033,#07101a 70%)}aside{padding:12px;background:#0e1722;border-left:1px solid #26384e}.card{background:#14202e;border:1px solid #2a3b50;border-radius:10px;padding:11px;margin-bottom:10px}.muted{color:#95a7bc;font-size:12px}.item{width:100%;text-align:left;margin-top:5px;padding:8px;border:1px solid #2e4258;border-radius:8px;background:#0d1722;color:white}.qr{display:flex;gap:10px;align-items:center}.qr img{width:105px;background:#fff;padding:5px;border-radius:7px}.warn{color:#fbbf24}@media(max-width:850px){.layout{grid-template-columns:1fr}canvas{height:500px}}</style></head><body><div class="top"><div><b>Spatial Hub</b><div class="muted">Windows background spatial hub</div></div><div id="v"></div></div><div class="layout"><canvas id="c"></canvas><aside><div class="card"><b>Подключить телефон</b><div class="qr"><img id="qr"><div><div id="code" style="font-size:20px"></div><div id="url" class="muted"></div></div></div></div><div class="card"><b>Устройства</b><div id="nodes"></div></div><div class="card"><b>Выбранное</b><div id="details" class="muted">Нажми на точку.</div></div><div class="card"><b>История</b><div id="events"></div></div><div class="card warn">Положение вероятностное. Обычный Wi‑Fi/Bluetooth не дают сантиметровые XYZ-координаты.</div></aside></div><script>const c=document.getElementById('c'),x=c.getContext('2d'),$=id=>document.getElementById(id);let S={nodes:[],p:[],yaw:-.7,pitch:.35,zoom:1,drag:false,lx:0,ly:0,sel:null};function rs(){let r=c.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);c.width=r.width*d;c.height=r.height*d;x.setTransform(d,0,0,d,0,0);draw()}addEventListener('resize',rs);function tf(p){let X=p.x||0,Y=p.y||0,Z=p.z||0,cy=Math.cos(S.yaw),sy=Math.sin(S.yaw),cp=Math.cos(S.pitch),sp=Math.sin(S.pitch),x1=X*cy-Z*sy,z1=X*sy+Z*cy,y1=Y*cp-z1*sp,z2=Y*sp+z1*cp,sc=Math.min(c.clientWidth/12,c.clientHeight/8)*S.zoom*(9/Math.max(5,9+z2));return{x:c.clientWidth/2+x1*sc,y:c.clientHeight*.57-y1*sc,sc}}function draw(){x.clearRect(0,0,c.clientWidth,c.clientHeight);S.p=[];for(let i=-6;i<=6;i++){line({x:i,y:-1.3,z:-6},{x:i,y:-1.3,z:6});line({x:-6,y:-1.3,z:i},{x:6,y:-1.3,z:i})}for(let n of S.nodes){let p=tf(n.position||{}),u=(n.position||{}).uncertainty||0;if(u){x.strokeStyle='rgba(103,232,249,.15)';x.beginPath();x.arc(p.x,p.y,Math.max(16,u*p.sc*.22),0,Math.PI*2);x.stroke()}x.fillStyle=n.kind==='hub'?'#fff':n.online===false?'#64748b':'#67e8f9';x.beginPath();x.arc(p.x,p.y,n.kind==='hub'?9:7,0,Math.PI*2);x.fill();x.fillStyle='#dbeafe';x.fillText(n.name||n.kind,p.x+12,p.y+4);S.p.push({n,p})}}function line(a,b){a=tf(a);b=tf(b);x.strokeStyle='rgba(148,163,184,.07)';x.beginPath();x.moveTo(a.x,a.y);x.lineTo(b.x,b.y);x.stroke()}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function detail(n){let s=n.sensors||{},t=n.telemetry||{};$('details').innerHTML=`<b>${esc(n.name)}</b><div>Status: ${n.online===false?'offline':'online'}</div><div>Battery: ${s.battery?.level!=null?Math.round(s.battery.level*100)+'%':t.battery!=null?Math.round(t.battery)+'%':'—'}</div><div>Latency: ${s.latency_ms!=null?Math.round(s.latency_ms)+' ms':'—'}</div><div>CPU: ${t.cpu!=null?Math.round(t.cpu)+'%':'—'}</div>`}c.onpointerdown=e=>{S.drag=true;S.lx=e.clientX;S.ly=e.clientY};c.onpointermove=e=>{if(!S.drag)return;S.yaw+=(e.clientX-S.lx)*.008;S.pitch+=(e.clientY-S.ly)*.008;S.lx=e.clientX;S.ly=e.clientY;draw()};c.onpointerup=()=>S.drag=false;c.onclick=e=>{let r=c.getBoundingClientRect(),a=e.clientX-r.left,b=e.clientY-r.top,best=null,d=999;for(let q of S.p){let z=Math.hypot(a-q.p.x,b-q.p.y);if(z<14&&z<d){best=q.n;d=z}}if(best)detail(best)};c.onwheel=e=>{e.preventDefault();S.zoom*=e.deltaY>0?.9:1.1;S.zoom=Math.max(.5,Math.min(2.2,S.zoom));draw()};async function poll(){try{let r=await fetch('/api/state'),s=await r.json();S.nodes=s.nodes;$('v').textContent='v'+s.version;$('nodes').innerHTML='';for(let n of S.nodes){let b=document.createElement('button');b.className='item';b.textContent=n.name+' · '+(n.online===false?'offline':'online');b.onclick=()=>detail(n);$('nodes').appendChild(b)}$('events').innerHTML=(s.events||[]).slice(0,30).map(e=>`<div class="muted">${new Date(e.t*1000).toLocaleTimeString()} · ${esc(e.text)}</div>`).join('');draw()}catch(e){}}async function info(){let s=await(await fetch('/api/info')).json();$('qr').src=s.qr;$('code').textContent=s.code;$('url').textContent=s.join_url}setInterval(poll,1600);info();poll();rs();</script></body></html>'''

PHONE=r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spatial Node</title><style>:root{color-scheme:dark}body{background:#081018;color:#eef6ff;font:15px system-ui;padding:16px}.card{max-width:520px;margin:0 auto 10px;background:#101b28;border:1px solid #2a3a50;border-radius:12px;padding:13px}button,input{width:100%;padding:11px;margin-top:8px;border-radius:8px;border:1px solid #35506b;background:#1c3248;color:#fff}.ok{color:#34d399}.bad{color:#fb7185}</style></head><body><div class="card"><h2>Spatial Node</h2><input id="name" value="iPhone 16"><button id="join">Подключиться</button><button id="motion">Разрешить движение</button><button id="geo">Разрешить GPS</button><div id="status"></div></div><script>const q=new URLSearchParams(location.search),token=q.get('token')||'', $=x=>document.getElementById(x);let joined=false,sensors={};let nid='phone-'+Date.now()+'-'+Math.random().toString(36).slice(2);try{nid=localStorage.getItem('shnid')||nid;localStorage.setItem('shnid',nid)}catch(e){};$('motion').onclick=async()=>{try{if(DeviceOrientationEvent?.requestPermission){if(await DeviceOrientationEvent.requestPermission()!=='granted')throw 0}addEventListener('deviceorientation',e=>sensors.orientation={alpha:e.alpha,beta:e.beta,gamma:e.gamma});addEventListener('devicemotion',e=>{let a=e.accelerationIncludingGravity||{};sensors.motion={x:a.x,y:a.y,z:a.z}});$('motion').disabled=true}catch(e){}};$('geo').onclick=()=>navigator.geolocation?.watchPosition(p=>sensors.geo={lat:p.coords.latitude,lon:p.coords.longitude,accuracy:p.coords.accuracy},()=>{}, {enableHighAccuracy:true});async function hb(){if(!joined)return;let t=performance.now();try{await fetch('/api/ping');sensors.latency_ms=performance.now()-t;let r=await fetch('/api/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,node_id:nid,name:$('name').value,ua:navigator.userAgent,sensors})});if(!r.ok)throw 0;$('status').innerHTML='<span class="ok">подключено</span>'}catch(e){$('status').innerHTML='<span class="bad">ошибка связи</span>'}};$('join').onclick=()=>{joined=true;$('join').disabled=true;hb()};setInterval(hb,1800);</script></body></html>'''

@app.get('/')
def dashboard():
    if request.remote_addr not in ('127.0.0.1','::1'):return Response('local only',403)
    return Response(DASH,mimetype='text/html')
@app.get('/node')
def node():
    if request.args.get('token')!=token:return Response('bad token',403)
    return Response(PHONE,mimetype='text/html')
@app.get('/api/ping')
def ping():return jsonify({'t':time.time()})
@app.get('/api/info')
def info():
    if request.remote_addr not in ('127.0.0.1','::1'):return Response('local only',403)
    url=f'http://{HOST}:{PORT}/node?token={token}';img=qrcode.make(url);bio=io.BytesIO();img.save(bio,format='PNG');qr='data:image/png;base64,'+base64.b64encode(bio.getvalue()).decode()
    return jsonify({'code':code,'join_url':url,'qr':qr})
@app.get('/api/state')
def api_state():
    if request.remote_addr not in ('127.0.0.1','::1'):return Response('local only',403)
    with lock:
        state['nodes']['hub']=hub();t=time.time();arr=[]
        for n in state['nodes'].values():
            x=dict(n);x['online']=True if x['id']=='hub' else t-x.get('last_seen',0)<8;arr.append(x)
        return jsonify({'version':ver(),'nodes':arr,'events':list(state['events'])[:100],'update':state['update']})
@app.post('/api/heartbeat')
def hb():
    d=request.get_json(silent=True) or {}
    if d.get('token')!=token:return jsonify({'error':'token'}),403
    nid=str(d.get('node_id',''))
    if not nid:return jsonify({'error':'id'}),400
    with lock:
        first=nid not in state['nodes'];n={'id':nid,'kind':'phone','name':str(d.get('name','Phone'))[:80],'ua':str(d.get('ua',''))[:400],'sensors':d.get('sensors') or {},'remote_ip':request.remote_addr,'online':True,'last_seen':time.time()};n['position']=pos(nid,'phone');state['nodes'][nid]=n
    if first:event(n['name']+' connected; map updated immediately')
    return jsonify({'ok':True})

def update_loop():
    while not stop.wait(1800):
        try:
            req=urllib.request.Request(MANIFEST+'?t='+str(int(time.time())),headers={'User-Agent':'SpatialHub'});remote=json.loads(urllib.request.urlopen(req,timeout=10).read().decode()).get('version')
            if remote and remote!=ver():
                state['update']=remote;event('Update '+remote+' found; restarting through updater');save();restart_update();return
        except Exception as e:log('update '+str(e))

def restart_update():
    if not LAUNCHER:return
    try:
        py=Path(sys.executable);cand=py.with_name('pythonw.exe');py=cand if cand.exists() else py
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'DETACHED_PROCESS',0);subprocess.Popen([str(py),LAUNCHER,'--update-and-start'],creationflags=flags,close_fds=True);os._exit(0)
    except Exception as e:log(e)

def icon_image():
    im=Image.new('RGBA',(64,64),(8,16,24,255));d=ImageDraw.Draw(im);d.ellipse((10,10,54,54),outline=(103,232,249,255),width=5);d.ellipse((25,25,39,39),fill=(103,232,249,255));return im

def tray():
    def openui(*_):webbrowser.open(f'http://127.0.0.1:{PORT}')
    def upd(*_):restart_update()
    def quit(icon,*_):save();icon.stop();os._exit(0)
    pystray.Icon('SpatialHub',icon_image(),'Spatial Hub '+ver(),pystray.Menu(pystray.MenuItem('Open Spatial Hub',openui,default=True),pystray.MenuItem('Install updates now',upd),pystray.MenuItem('Exit',quit))).run()

def main():
    s=socket.socket();busy=s.connect_ex(('127.0.0.1',PORT))==0;s.close()
    if busy:webbrowser.open(f'http://127.0.0.1:{PORT}');return
    event('Spatial Hub '+ver()+' started');threading.Thread(target=lambda:serve(app,host='0.0.0.0',port=PORT,threads=8),daemon=True).start();threading.Thread(target=update_loop,daemon=True).start();time.sleep(.8);webbrowser.open(f'http://127.0.0.1:{PORT}');tray()
if __name__=='__main__':main()
