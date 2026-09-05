from __future__ import annotations
import asyncio, base64, hashlib, io, ipaddress, json, math, os, re, secrets, socket, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from flask import Flask, jsonify, request, Response
from waitress import serve
import psutil, qrcode, pystray
from PIL import Image, ImageDraw
try: sys.coinit_flags = 0
except Exception: pass
try:
    from bleak import BleakScanner
    BLEAK_OK=True
except Exception:
    BLEAK_OK=False
import webview

APP_VERSION="0.4.1"; PORT=8765
ROOT=Path(os.environ.get("SPATIAL_HUB_ROOT",Path(os.environ.get("LOCALAPPDATA",str(Path.home())))/"SpatialHub"))
APPDIR=ROOT/"app"; DATA=ROOT/"data"; LOGS=ROOT/"logs"
DATA.mkdir(parents=True,exist_ok=True); LOGS.mkdir(parents=True,exist_ok=True)
LOG=LOGS/"app.log"; VERSION=APPDIR/"version.json"
MANIFEST=os.environ.get("SPATIAL_HUB_MANIFEST","https://raw.githubusercontent.com/Godar222/spatial-hub/main/update/manifest.json")
LAUNCHER=os.environ.get("SPATIAL_HUB_LAUNCHER","")
app=Flask(__name__); lock=threading.RLock(); scan_lock=threading.Lock(); stop=threading.Event()
token=secrets.token_urlsafe(24); paircode=f"{secrets.randbelow(1000000):06d}"; window=None
state={"agents":{},"scan":{"lan":[],"wifi":[],"ble":[],"last_scan":0,"running":False,"errors":[]}}

def log(x):
    try:
        with LOG.open("a",encoding="utf-8") as f:f.write(f"[{time.strftime('%F %T')}] {x}\n")
    except Exception:pass

def version():
    try:return json.loads(VERSION.read_text(encoding="utf-8")).get("version",APP_VERSION)
    except Exception:return APP_VERSION

def local_ip():
    for target in (("1.1.1.1",80),("8.8.8.8",80)):
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(target);x=s.getsockname()[0];s.close()
            if x and not x.startswith("127."):return x
        except Exception:pass
    return "127.0.0.1"
HOST=local_ip()

def cmd(s,timeout=12):
    try:
        p=subprocess.run(["cmd.exe","/d","/s","/c",f"chcp 65001>nul & {s}"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return p.stdout.decode("utf-8","replace")
    except Exception as e:log(f"cmd {s}: {e}");return ""

def pos(key,kind,dist=None):
    if kind=="hub":return {"x":0,"y":0,"z":0,"uncertainty":.1}
    h=hashlib.sha256(str(key).encode()).digest();a=int.from_bytes(h[:4],"big")/2**32*math.tau
    r=({"agent":2.4,"lan":5.5}.get(kind,4.0) if dist is None else max(1.2,min(6.4,1.2+math.log1p(max(.15,dist))*1.7)))
    return {"x":math.cos(a)*r,"y":0,"z":math.sin(a)*r,"uncertainty":3 if dist is None else max(.8,min(4.5,.8+dist*.8))}

def rough(rssi,ref=-42,loss=2.7):
    try:return max(.25,min(80,10**((ref-float(rssi))/(10*loss))))
    except Exception:return None

def hub():
    b=psutil.sensors_battery();m=psutil.virtual_memory()
    return {"id":"hub","kind":"hub","name":socket.gethostname(),"online":True,"connection":"local","position":pos("hub","hub"),
            "telemetry":{"cpu":psutil.cpu_percent(None),"ram":m.percent,"battery":getattr(b,"percent",None)}}

def network_info():
    stats=psutil.net_if_stats();choices=[]
    for name,addrs in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:continue
        for a in addrs:
            if a.family!=socket.AF_INET or not a.address or a.address.startswith("127.") or not a.netmask:continue
            try:
                ip=ipaddress.ip_address(a.address)
                if not ip.is_private:continue
                net=ipaddress.ip_network(f"{a.address}/{a.netmask}",strict=False)
                score=(100 if any(k in name.lower() for k in ("wi-fi","wifi","wlan")) else 0)+(80 if a.address==HOST else 0)
                choices.append((score,name,a.address,net))
            except Exception:pass
    if not choices:return None,None,None
    _,n,ip,net=max(choices,key=lambda x:x[0])
    if net.num_addresses>256:net=ipaddress.ip_network(f"{ip}/24",strict=False)
    return n,ip,net

def ping(ip):
    try:return subprocess.run(["ping","-n","1","-w","180",ip],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=1,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).returncode==0
    except Exception:return False

def gateway():
    for line in cmd("route print -4",5).splitlines():
        p=line.split()
        if len(p)>=5 and p[0]=="0.0.0.0" and p[1]=="0.0.0.0":
            try:ipaddress.ip_address(p[2]);return p[2]
            except Exception:pass
    return None

def scan_lan():
    iface,own,net=network_info()
    if not net:return [],"Не найдена активная частная IPv4-сеть."
    hosts=[str(h) for h in net.hosts() if str(h)!=own]
    with ThreadPoolExecutor(max_workers=64) as ex:
        for _ in as_completed([ex.submit(ping,h) for h in hosts]):pass
    rows={}
    for line in cmd("arp -a",5).splitlines():
        m=re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f-]{17})\s+",line)
        if m:rows[m.group(1)]=m.group(2).replace("-",":").upper()
    gw=gateway();out=[]
    for ip,mac in rows.items():
        try:
            if ipaddress.ip_address(ip) not in net or ip==own:continue
        except Exception:continue
        isgw=ip==gw
        out.append({"id":f"lan:{mac}","kind":"lan","name":"Router / gateway" if isgw else f"LAN device {ip}","ip":ip,"address":mac,
                    "connection":"LAN / ARP","online":True,"position":pos(mac,"lan"),"distance":{"available":False,"note":"LAN не сообщает физическое расстояние."}})
    if gw and not any(x["ip"]==gw for x in out):
        out.append({"id":f"lan:{gw}","kind":"lan","name":"Router / gateway","ip":gw,"address":"","connection":"Default route","online":True,
                    "position":pos(gw,"lan"),"distance":{"available":False,"note":"Шлюз найден из таблицы маршрутов."}})
    return out,None

def parse_wifi_networks(text):
    out=[];ssid="";cur=None
    for raw in text.splitlines():
        line=raw.strip()
        m=re.match(r"^SSID\s+\d+\s*:\s*(.*)$",line,re.I)
        if m and not line.upper().startswith("BSSID"):ssid=m.group(1).strip();continue
        m=re.match(r"^BSSID\s+\d+\s*:\s*([0-9A-Fa-f:-]{11,})",line,re.I)
        if m:
            if cur:out.append(cur)
            cur={"ssid":ssid,"bssid":m.group(1).replace("-",":").upper(),"q":None};continue
        if cur:
            m=re.search(r":\s*(\d{1,3})\s*%\s*$",line)
            if m and cur["q"] is None:cur["q"]=max(0,min(100,int(m.group(1))))
    if cur:out.append(cur)
    return out

def connected_wifi():
    text=cmd("netsh wlan show interfaces",8);ssid=bssid=None;q=None
    for raw in text.splitlines():
        line=raw.strip()
        if re.match(r"^SSID\s*:",line,re.I):ssid=line.split(":",1)[1].strip()
        elif re.match(r"^BSSID\s*:",line,re.I):bssid=line.split(":",1)[1].strip().replace("-",":").upper()
        else:
            m=re.search(r":\s*(\d{1,3})\s*%\s*$",line)
            if m and q is None:q=int(m.group(1))
    return [{"ssid":ssid or "Connected Wi-Fi","bssid":bssid,"q":q}] if bssid else []

def scan_wifi():
    raw=cmd("netsh wlan show networks mode=bssid",12);items=parse_wifi_networks(raw);warning=None
    low=raw.lower()
    if ("location" in low and ("permission" in low or "access" in low)) or "местополож" in low:
        warning="Windows требует разрешение Location для просмотра соседних Wi‑Fi сетей. Включи Параметры → Конфиденциальность и безопасность → Расположение."
    if not items:items=connected_wifi()
    out=[]
    for i in items:
        rssi=(i["q"]/2-100) if i.get("q") is not None else None;d=rough(rssi) if rssi is not None else None
        out.append({"id":f"wifi:{i['bssid']}","kind":"wifi","name":i["ssid"],"address":i["bssid"],"signal":i.get("q"),
                    "rssi":round(rssi,1) if rssi is not None else None,"connection":"Wi-Fi AP","online":True,"position":pos(i["bssid"],"wifi",d),
                    "distance":{"available":d is not None,"approx_m":round(d,1) if d is not None else None,"confidence":"very low","note":"RSSI-оценка; ошибка может быть в несколько раз."}})
    return out,(None if out else warning or "Wi‑Fi BSSID не получен."),warning

async def ble_do():
    found=await BleakScanner.discover(timeout=4.5,return_adv=True);out=[]
    if isinstance(found,dict):
        for v in found.values():
            try:
                dev,adv=v;addr=getattr(dev,"address","") or "";name=getattr(dev,"name",None) or getattr(adv,"local_name",None) or "BLE device"
                rssi=getattr(adv,"rssi",None);tx=getattr(adv,"tx_power",None);ref=float(tx) if tx is not None and -100<float(tx)<20 else -59;d=rough(rssi,ref,2.5) if rssi is not None else None
                out.append({"id":f"ble:{addr or name}","kind":"ble","name":name,"address":addr,"rssi":rssi,"connection":"BLE advertisement","online":True,
                            "position":pos(addr or name,"ble",d),"distance":{"available":d is not None,"approx_m":round(d,1) if d is not None else None,"confidence":"very low","note":"BLE RSSI proximity only."}})
            except Exception:pass
    return out

def scan_ble():
    if not BLEAK_OK:return [],"Bluetooth library не загрузилась."
    try:return asyncio.run(ble_do()),None
    except Exception as e:return [],f"Bluetooth: {e}"

def perform_scan():
    if not scan_lock.acquire(False):return
    try:
        with lock:state["scan"]["running"]=True;state["scan"]["errors"]=[]
        errors=[]
        try:wifi,e,warn=scan_wifi()
        except Exception as x:wifi,e,warn=[],f"Wi‑Fi: {x}",None
        if e:errors.append(e)
        elif warn:errors.append(warn)
        try:lan,e=scan_lan()
        except Exception as x:lan,e=[],f"LAN: {x}"
        if e:errors.append(e)
        try:ble,e=scan_ble()
        except Exception as x:ble,e=[],f"Bluetooth: {x}"
        if e:errors.append(e)
        with lock:state["scan"].update({"lan":lan,"wifi":wifi,"ble":ble,"last_scan":time.time(),"running":False,"errors":errors})
    finally:
        with lock:state["scan"]["running"]=False
        scan_lock.release()

def nodes():
    t=time.time()
    with lock:
        agents=[]
        for n in state["agents"].values():
            q=dict(n);q["online"]=t-q.get("last_seen",0)<9;agents.append(q)
        return [hub()]+list(state["scan"]["lan"])+list(state["scan"]["wifi"])+list(state["scan"]["ble"])+agents

def localonly():return request.remote_addr in ("127.0.0.1","::1")
def html(name):return (APPDIR/name).read_text(encoding="utf-8")

@app.get("/")
def root():return Response(html("dashboard.html"),mimetype="text/html") if localonly() else Response("local only",403)
@app.get("/node")
def nodepage():return Response(html("phone.html"),mimetype="text/html") if request.args.get("token")==token else Response("bad token",403)
@app.get("/api/ping")
def apiping():return jsonify({"t":time.time()})
@app.get("/api/info")
def apiinfo():
    if not localonly():return Response("local only",403)
    url=f"http://{HOST}:{PORT}/node?token={token}";im=qrcode.make(url);b=io.BytesIO();im.save(b,format="PNG")
    return jsonify({"code":paircode,"url":url,"qr":"data:image/png;base64,"+base64.b64encode(b.getvalue()).decode()})
@app.get("/api/state")
def apistate():
    if not localonly():return Response("local only",403)
    with lock:
        sc={"lan":list(state["scan"]["lan"]),"wifi":list(state["scan"]["wifi"]),"ble":list(state["scan"]["ble"]),"last_scan":state["scan"]["last_scan"],"running":state["scan"]["running"],"errors":list(state["scan"]["errors"])}
        agents=[dict(x) for x in state["agents"].values()]
    return jsonify({"version":version(),"nodes":nodes(),"scan":sc,"agents":agents})
@app.post("/api/scan")
def apiscan():
    if not localonly():return Response("local only",403)
    threading.Thread(target=perform_scan,daemon=True).start();return jsonify({"ok":True})
@app.post("/api/heartbeat")
def heartbeat():
    d=request.get_json(silent=True) or {}
    if d.get("token")!=token:return jsonify({"error":"token"}),403
    nid=str(d.get("node_id") or "")
    if not nid:return jsonify({"error":"id"}),400
    with lock:state["agents"][nid]={"id":nid,"kind":"agent","name":str(d.get("name") or "Phone")[:80],"sensors":d.get("sensors") or {},"last_seen":time.time(),"connection":"Spatial Node","online":True,"position":pos(nid,"agent"),"distance":{"available":False,"note":"Phone browser agent has no radio ranging."}}
    return jsonify({"ok":True})

def server():serve(app,host="0.0.0.0",port=PORT,threads=10)
def scanner():
    time.sleep(.7)
    while not stop.is_set():
        perform_scan()
        if stop.wait(45):break

def updater():
    while not stop.wait(1800):
        try:
            m=json.loads(urllib.request.urlopen(urllib.request.Request(MANIFEST+"?t="+str(int(time.time())),headers={"User-Agent":"SpatialHub"}),timeout=10).read().decode())
            if m.get("version")!=version() and LAUNCHER:
                py=Path(sys.executable);w=py.with_name("pythonw.exe");py=w if w.exists() else py
                subprocess.Popen([str(py),LAUNCHER,"--update-and-start"],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0));os._exit(0)
        except Exception as e:log(e)

def icon():
    im=Image.new("RGB",(64,64),"#0b1724");d=ImageDraw.Draw(im);d.ellipse((12,12,52,52),outline="#67e8f9",width=4);d.ellipse((27,27,37,37),fill="white");return im

def tray():
    def show(i,x):
        try:window.show()
        except Exception:pass
    def scan(i,x):threading.Thread(target=perform_scan,daemon=True).start()
    def quit(i,x):
        stop.set()
        try:i.stop()
        except Exception:pass
        os._exit(0)
    try:pystray.Icon("SpatialHub",icon(),"Spatial Hub",pystray.Menu(pystray.MenuItem("Open",show,default=True),pystray.MenuItem("Scan now",scan),pystray.MenuItem("Exit",quit))).run()
    except Exception as e:log(e)

def main():
    global window
    threading.Thread(target=server,daemon=True).start();time.sleep(.4)
    threading.Thread(target=scanner,daemon=True).start();threading.Thread(target=updater,daemon=True).start();threading.Thread(target=tray,daemon=True).start()
    window=webview.create_window("Spatial Hub",f"http://127.0.0.1:{PORT}",width=1450,height=900,min_size=(980,650))
    webview.start(debug=False)
if __name__=="__main__":main()
