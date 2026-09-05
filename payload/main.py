from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, jsonify, request, Response
from waitress import serve
import psutil
import qrcode
import pystray
from PIL import Image, ImageDraw
import webview

try:
    from bleak import BleakScanner
    BLEAK_OK = True
except Exception:
    BLEAK_OK = False

APP_VERSION = "0.4.0"
PORT = 8765
ROOT = Path(os.environ.get("SPATIAL_HUB_ROOT", Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpatialHub"))
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DATA.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / "state.json"
VERSION_FILE = ROOT / "app" / "version.json"
LOG_FILE = LOGS / "app.log"
MANIFEST = os.environ.get("SPATIAL_HUB_MANIFEST", "https://raw.githubusercontent.com/Godar222/spatial-hub/main/update/manifest.json")
LAUNCHER = os.environ.get("SPATIAL_HUB_LAUNCHER", "")

app = Flask(__name__)
lock = threading.RLock()
stop = threading.Event()
scan_lock = threading.Lock()
token = secrets.token_urlsafe(24)
code = f"{secrets.randbelow(1000000):06d}"
window_ref = None
tray_ref = None
state = {
    "agents": {},
    "events": deque(maxlen=400),
    "started": time.time(),
    "update": None,
    "scan": {"lan": [], "wifi": [], "ble": [], "last_scan": 0, "running": False, "errors": []},
}

def log(message):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {message}\\n")
    except Exception:
        pass

def current_version():
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8")).get("version", APP_VERSION)
    except Exception:
        return APP_VERSION

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

HOST = local_ip()

def event(text):
    with lock:
        state["events"].appendleft({"t": time.time(), "text": str(text)[:240]})

def save_agents():
    try:
        with lock:
            payload = {"agents": state["agents"], "events": list(state["events"])[:120]}
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"save: {exc}")

def load_agents():
    try:
        if not STATE_FILE.exists():
            return
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state["agents"] = payload.get("agents", {})
        for node in state["agents"].values():
            node["online"] = False
        for e in payload.get("events", []):
            state["events"].append(e)
    except Exception as exc:
        log(f"load: {exc}")

load_agents()

def stable_angle(key):
    h = hashlib.sha256(str(key).encode("utf-8", "ignore")).digest()
    return int.from_bytes(h[:4], "big") / 2**32 * math.tau

def stable_height(key):
    h = hashlib.md5(str(key).encode("utf-8", "ignore")).digest()
    return (h[0] / 255.0 - 0.5) * 1.1

def logical_position(key, kind, approx_m=None):
    angle = stable_angle(key)
    if approx_m is not None and kind in ("wifi", "ble"):
        radius = max(1.0, min(6.2, 1.2 + math.log1p(max(0.1, approx_m)) * 1.7))
        uncertainty = max(0.8, min(4.0, approx_m * 0.8 + 0.7))
    else:
        radius = {"hub": 0.0, "agent": 2.2, "lan": 5.7}.get(kind, 4.5)
        uncertainty = 0.15 if kind == "hub" else 3.0
    return {"x": math.cos(angle) * radius, "z": math.sin(angle) * radius, "y": 0.0 if kind == "hub" else stable_height(key), "uncertainty": uncertainty, "direction_known": kind == "hub"}

def hub_node():
    battery = psutil.sensors_battery()
    memory = psutil.virtual_memory()
    temps = {}
    try:
        for sensor, entries in (psutil.sensors_temperatures() or {}).items():
            if entries:
                temps[sensor] = entries[0].current
    except Exception:
        pass
    return {
        "id": "hub", "kind": "hub", "name": socket.gethostname(), "online": True,
        "last_seen": time.time(), "position": logical_position("hub", "hub"), "connection": "local",
        "telemetry": {"cpu_percent": psutil.cpu_percent(None), "ram_percent": memory.percent,
                      "battery_percent": getattr(battery, "percent", None), "temperatures_c": temps},
    }

def run_cmd(command, timeout=12):
    try:
        full = f"chcp 65001>nul & {command}"
        p = subprocess.run(["cmd", "/d", "/s", "/c", full], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.stdout
    except Exception as exc:
        log(f"command failed {command}: {exc}")
        return ""

def private_network():
    candidates = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or not a.address or a.address.startswith("127."):
                continue
            try:
                ip = ipaddress.ip_address(a.address)
                if not ip.is_private or not a.netmask:
                    continue
                net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                candidates.append((name, a.address, net))
            except Exception:
                pass
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: ("wi-fi" not in x[0].lower() and "wifi" not in x[0].lower() and "wlan" not in x[0].lower(), x[0]))
    name, ip, net = candidates[0]
    if net.num_addresses > 256:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return name, ip, net

def ping_one(ip):
    try:
        p = subprocess.run(["ping", "-n", "1", "-w", "220", str(ip)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=1.2,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode == 0
    except Exception:
        return False

def arp_entries():
    out = run_cmd("arp -a", timeout=5)
    result = {}
    for line in out.splitlines():
        m = re.search(r"(\\d+\\.\\d+\\.\\d+\\.\\d+)\\s+([0-9A-Fa-f-]{17})\\s+", line)
        if m:
            result[m.group(1)] = m.group(2).replace("-", ":").upper()
    return result

def default_gateway():
    out = run_cmd("route print -4", timeout=5)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            if re.fullmatch(r"\\d+\\.\\d+\\.\\d+\\.\\d+", parts[2]):
                return parts[2]
    return None

def scan_lan():
    name, own_ip, net = private_network()
    if not net:
        return [], "No private IPv4 network detected"
    hosts = [str(h) for h in net.hosts() if str(h) != own_ip]
    with ThreadPoolExecutor(max_workers=48) as executor:
        futures = [executor.submit(ping_one, ip) for ip in hosts]
        for _ in as_completed(futures):
            pass
    arp = arp_entries()
    gateway = default_gateway()
    devices = []
    for ip, mac in arp.items():
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except Exception:
            continue
        is_gateway = ip == gateway
        devices.append({
            "id": f"lan:{mac or ip}", "kind": "lan", "subkind": "gateway" if is_gateway else "device",
            "name": "Router / gateway" if is_gateway else f"LAN device {ip}", "ip": ip, "address": mac,
            "online": True, "last_seen": time.time(), "connection": "LAN-visible",
            "position": logical_position(mac or ip, "lan"),
            "distance": {"available": False, "note": "ARP/LAN discovery has no physical ranging information."},
        })
    return devices, None

def quality_to_rssi(quality):
    return float(quality) / 2.0 - 100.0

def rough_radio_distance(rssi_dbm, ref_dbm=-40.0, path_loss=2.7):
    try:
        d = 10 ** ((ref_dbm - float(rssi_dbm)) / (10.0 * path_loss))
        return max(0.25, min(80.0, d))
    except Exception:
        return None

def scan_wifi():
    out = run_cmd("netsh wlan show networks mode=bssid", timeout=12)
    if not out:
        return [], "Wi-Fi scan returned no data"
    devices, ssid, bssid, quality, channel, radio = [], "", None, None, None, None
    def flush():
        nonlocal bssid, quality, channel, radio
        if not bssid:
            return
        rssi = quality_to_rssi(quality) if quality is not None else None
        dist = rough_radio_distance(rssi) if rssi is not None else None
        devices.append({
            "id": f"wifi:{bssid}", "kind": "wifi", "name": ssid or "Hidden Wi-Fi", "address": bssid,
            "signal_percent": quality, "rssi_est_dbm": round(rssi, 1) if rssi is not None else None,
            "channel": channel, "radio": radio, "online": True, "last_seen": time.time(),
            "connection": "Wi-Fi access point / observed", "position": logical_position(bssid, "wifi", dist),
            "distance": {"available": dist is not None, "approx_m": round(dist, 1) if dist is not None else None,
                         "confidence": "very low",
                         "note": "Estimated from Windows signal quality. Direction is unknown; walls and AP transmit power can change the estimate by several times."},
        })
        bssid = quality = channel = radio = None
    for raw in out.splitlines():
        line = raw.strip()
        m = re.match(r"SSID\\s+\\d+\\s*:\\s*(.*)$", line, re.I)
        if m and not line.upper().startswith("BSSID"):
            flush(); ssid = m.group(1).strip(); continue
        m = re.match(r"BSSID\\s+\\d+\\s*:\\s*([0-9A-Fa-f:-]{11,})", line, re.I)
        if m:
            flush(); bssid = m.group(1).replace("-", ":").upper(); continue
        if bssid:
            m = re.match(r"(?:Signal|Сигнал|Signál)\\s*:\\s*(\\d+)\\s*%", line, re.I)
            if m: quality = int(m.group(1)); continue
            m = re.match(r"(?:Channel|Канал|Kanál)\\s*:\\s*(\\d+)", line, re.I)
            if m: channel = int(m.group(1)); continue
            m = re.match(r"(?:Radio type|Тип радио|Typ rádia)\\s*:\\s*(.+)", line, re.I)
            if m: radio = m.group(1).strip()
    flush()
    return devices, None

async def ble_scan_async():
    found = await BleakScanner.discover(timeout=5.0, return_adv=True)
    result = []
    if not isinstance(found, dict):
        return result
    for _, pair in found.items():
        try:
            dev, adv = pair
            address = getattr(dev, "address", "") or ""
            name = getattr(dev, "name", None) or getattr(adv, "local_name", None) or "BLE device"
            rssi = getattr(adv, "rssi", None)
            tx = getattr(adv, "tx_power", None)
            if rssi is None:
                continue
            ref = float(tx) if tx is not None and -100 < float(tx) < 20 else -59.0
            dist = rough_radio_distance(rssi, ref_dbm=ref, path_loss=2.5)
            result.append({
                "id": f"ble:{address}", "kind": "ble", "name": name, "address": address,
                "rssi_dbm": int(rssi), "tx_power_dbm": tx, "online": True, "last_seen": time.time(),
                "connection": "BLE advertising / discovered", "position": logical_position(address or name, "ble", dist),
                "distance": {"available": dist is not None, "approx_m": round(dist, 1) if dist is not None else None,
                             "confidence": "very low", "note": "BLE RSSI proximity estimate only. Not a measured distance."},
            })
        except Exception:
            continue
    return result

def scan_ble():
    if not BLEAK_OK:
        return [], "Bluetooth scanner dependency unavailable"
    try:
        return asyncio.run(ble_scan_async()), None
    except Exception as exc:
        return [], f"Bluetooth scan failed: {exc}"

def perform_scan():
    if not scan_lock.acquire(blocking=False):
        return
    try:
        with lock:
            state["scan"]["running"] = True
            state["scan"]["errors"] = []
        errors = []
        wifi, err = scan_wifi()
        if err: errors.append(err)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_lan = ex.submit(scan_lan)
            f_ble = ex.submit(scan_ble)
            lan, lan_err = f_lan.result()
            ble, ble_err = f_ble.result()
        if lan_err: errors.append(lan_err)
        if ble_err: errors.append(ble_err)
        with lock:
            old_counts = (len(state["scan"]["lan"]), len(state["scan"]["wifi"]), len(state["scan"]["ble"]))
            state["scan"].update({"lan": lan, "wifi": wifi, "ble": ble, "last_scan": time.time(), "errors": errors})
            new_counts = (len(lan), len(wifi), len(ble))
        if old_counts != new_counts:
            event(f"Discovery updated: LAN {new_counts[0]}, Wi-Fi {new_counts[1]}, BLE {new_counts[2]}")
    except Exception as exc:
        log(f"scan: {exc}")
        with lock:
            state["scan"]["errors"] = [str(exc)]
    finally:
        with lock:
            state["scan"]["running"] = False
        scan_lock.release()

def scan_loop():
    perform_scan()
    while not stop.wait(45):
        perform_scan()

DASH = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spatial Hub</title><style>
:root{color-scheme:dark;--bg:#070c13;--p:#0e1722;--c:#14202e;--line:#293b51;--txt:#eef6ff;--mut:#91a4ba;--cyan:#67e8f9;--green:#34d399;--amber:#fbbf24;--violet:#a78bfa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:13px/1.4 system-ui,Segoe UI,sans-serif;overflow:hidden}
header{height:58px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;border-bottom:1px solid var(--line);background:#09111b}h1{font-size:16px;margin:0}.mut{color:var(--mut);font-size:11px}.layout{display:grid;grid-template-columns:minmax(520px,1fr) 390px;height:calc(100vh - 58px)}.stage{position:relative;background:radial-gradient(circle at 50% 44%,#132237 0,#070c13 70%)}canvas{width:100%;height:100%;display:block}.badges{position:absolute;top:10px;left:10px;display:flex;gap:6px;flex-wrap:wrap}.badge{background:rgba(7,12,19,.88);border:1px solid var(--line);border-radius:999px;padding:5px 8px;color:var(--mut);font-size:11px}aside{background:var(--p);border-left:1px solid var(--line);overflow:auto;padding:11px}.tabs{display:flex;gap:5px;position:sticky;top:0;background:var(--p);padding-bottom:8px;z-index:2}.tab{border:1px solid #32465e;background:#111d2a;color:#cbd5e1;padding:7px 8px;border-radius:8px;cursor:pointer}.tab.active{background:#20354a;color:white}.card{background:var(--c);border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:9px}.row{display:flex;justify-content:space-between;gap:8px;align-items:center}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}.stat{background:#0b141f;border:1px solid #223449;border-radius:8px;padding:7px}.stat b{font-size:16px;display:block}.stat span{font-size:9px;color:var(--mut)}.item{width:100%;text-align:left;color:#fff;background:#0c1621;border:1px solid #26394f;border-radius:8px;padding:8px;margin-top:5px;cursor:pointer}.item:hover{background:#132235}.kind{font-size:9px;border-radius:999px;padding:2px 5px;background:#24364c;margin-right:5px}.dist{color:var(--amber)}button.action{background:#1d3349;border:1px solid #385675;color:#fff;padding:7px 9px;border-radius:8px;cursor:pointer}.qr{display:flex;gap:8px;align-items:center}.qr img{width:96px;background:#fff;padding:4px;border-radius:7px}.url{word-break:break-all;font-size:10px;color:var(--mut)}.panel{display:none}.panel.active{display:block}table{width:100%;border-collapse:collapse}td{padding:4px 0;border-bottom:1px solid rgba(148,163,184,.1);vertical-align:top}td:first-child{color:var(--mut);width:38%}.note{font-size:11px;color:#fde68a;background:#241c0c;border:1px solid #5c4820;border-radius:8px;padding:8px}
</style></head><body>
<header><div><h1>Spatial Hub Desktop</h1><div class="mut">LAN · Wi-Fi · Bluetooth · sensor agents</div></div><div class="row"><button class="action" id="rescan">Сканировать сейчас</button><span id="status" class="mut">starting…</span><b id="version"></b></div></header>
<div class="layout"><section class="stage"><canvas id="c"></canvas><div class="badges"><span class="badge" id="bLan">LAN 0</span><span class="badge" id="bWifi">Wi-Fi 0</span><span class="badge" id="bBle">BLE 0</span><span class="badge" id="bAgents">Agents 0</span></div></section><aside>
<div class="tabs"><button class="tab active" data-p="overview">Обзор</button><button class="tab" data-p="lan">LAN</button><button class="tab" data-p="wifi">Wi-Fi</button><button class="tab" data-p="ble">Bluetooth</button><button class="tab" data-p="phone">Телефон</button></div>
<div class="panel active" id="overview"><div class="card"><div class="stats"><div class="stat"><b id="nLan">0</b><span>LAN</span></div><div class="stat"><b id="nWifi">0</b><span>Wi-Fi AP</span></div><div class="stat"><b id="nBle">0</b><span>BLE</span></div><div class="stat"><b id="nAgents">0</b><span>Agents</span></div></div></div><div class="card"><b>Выбранное устройство</b><div id="details" class="mut" style="margin-top:6px">Нажми точку на карте или устройство в списке.</div></div><div class="note">Расстояния Wi-Fi/BLE — приблизительные оценки по уровню сигнала. Направление на устройство обычный адаптер ноутбука не измеряет, поэтому угол точки на 3D-карте условный.</div></div>
<div class="panel" id="lan"><div class="card"><b>Устройства локальной сети</b><div class="mut">Обнаружение через ping/ARP. Физическое расстояние из LAN определить нельзя.</div><div id="lanList"></div></div></div>
<div class="panel" id="wifi"><div class="card"><b>Роутеры и точки доступа рядом</b><div class="mut">Windows signal quality → грубая оценка RSSI → приблизительная дистанция.</div><div id="wifiList"></div></div></div>
<div class="panel" id="ble"><div class="card"><b>Bluetooth Low Energy</b><div class="mut">Показываются устройства, которые сейчас рекламируют BLE. Автоподключение без разрешения/поддерживаемого сервиса невозможно.</div><div id="bleList"></div></div></div>
<div class="panel" id="phone"><div class="card"><b>Подключить Spatial Node</b><div class="qr"><img id="qr"><div><div id="code" style="font-size:19px"></div><div id="url" class="url"></div></div></div></div><div class="card"><b>Подключённые агенты</b><div id="agentList"></div></div></div>
</aside></div>
<script>
const c=document.getElementById('c'),ctx=c.getContext('2d'),$=id=>document.getElementById(id);let S={nodes:[],p:[],yaw:-.72,pitch:.34,zoom:1,drag:false,lx:0,ly:0,dx:0,dy:0};const colors={hub:'#fff',lan:'#34d399',wifi:'#67e8f9',ble:'#a78bfa',agent:'#fbbf24'};
function esc(s){return String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}function resize(){let r=c.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);c.width=r.width*d;c.height=r.height*d;ctx.setTransform(d,0,0,d,0,0);draw()}addEventListener('resize',resize);
function tf(p){let X=p.x||0,Y=p.y||0,Z=p.z||0,cy=Math.cos(S.yaw),sy=Math.sin(S.yaw),cp=Math.cos(S.pitch),sp=Math.sin(S.pitch),x1=X*cy-Z*sy,z1=X*sy+Z*cy,y1=Y*cp-z1*sp,z2=Y*sp+z1*cp,scale=Math.min(c.clientWidth/14,c.clientHeight/9)*S.zoom*(10/Math.max(5,10+z2));return{x:c.clientWidth/2+x1*scale,y:c.clientHeight*.56-y1*scale,scale}}function line(a,b,col='rgba(148,163,184,.065)'){a=tf(a);b=tf(b);ctx.strokeStyle=col;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}
function draw(){ctx.clearRect(0,0,c.clientWidth,c.clientHeight);S.p=[];for(let i=-7;i<=7;i++){line({x:i,y:-1.3,z:-7},{x:i,y:-1.3,z:7});line({x:-7,y:-1.3,z:i},{x:7,y:-1.3,z:i})}let hub=S.nodes.find(n=>n.kind==='hub');if(hub)for(let n of S.nodes)if(n.kind!=='hub')line(hub.position,n.position,'rgba(103,232,249,.045)');for(let n of S.nodes){let p=tf(n.position||{}),u=(n.position||{}).uncertainty||0,r=n.kind==='hub'?9:6.5;if(u&&n.kind!=='lan'){ctx.strokeStyle=n.kind==='wifi'?'rgba(103,232,249,.13)':n.kind==='ble'?'rgba(167,139,250,.13)':'rgba(251,191,36,.12)';ctx.beginPath();ctx.arc(p.x,p.y,Math.max(13,u*p.scale*.18),0,Math.PI*2);ctx.stroke()}ctx.fillStyle=colors[n.kind]||'#94a3b8';ctx.beginPath();ctx.arc(p.x,p.y,r,0,Math.PI*2);ctx.fill();ctx.fillStyle='#dbeafe';ctx.font='11px system-ui';ctx.fillText((n.name||n.kind).slice(0,22),p.x+r+5,p.y+4);S.p.push({n,p,r})}}
function distText(n){return n.distance?.available&&n.distance.approx_m!=null?'≈ '+n.distance.approx_m+' м':'—'}function detail(n){let d=n.distance||{},t=n.telemetry||{},s=n.sensors||{};$('details').innerHTML=`<b>${esc(n.name)}</b><table><tr><td>Тип</td><td>${esc(n.kind)}</td></tr>${n.ip?`<tr><td>IP</td><td>${esc(n.ip)}</td></tr>`:''}${n.address?`<tr><td>Адрес</td><td>${esc(n.address)}</td></tr>`:''}<tr><td>Соединение</td><td>${esc(n.connection||'—')}</td></tr><tr><td>Расстояние</td><td class="dist">${distText(n)}</td></tr>${n.signal_percent!=null?`<tr><td>Wi-Fi сигнал</td><td>${n.signal_percent}% · ~${n.rssi_est_dbm} dBm</td></tr>`:''}${n.rssi_dbm!=null?`<tr><td>BLE RSSI</td><td>${n.rssi_dbm} dBm</td></tr>`:''}${n.channel!=null?`<tr><td>Канал</td><td>${n.channel}</td></tr>`:''}${t.cpu_percent!=null?`<tr><td>CPU</td><td>${Math.round(t.cpu_percent)}%</td></tr>`:''}${s.latency_ms!=null?`<tr><td>Latency</td><td>${Math.round(s.latency_ms)} ms</td></tr>`:''}<tr><td>Точность</td><td>${esc(d.note||'Положение на карте логическое, не физическая координата.')}</td></tr></table>`}
function item(n,extra=''){let b=document.createElement('button');b.className='item';b.innerHTML=`<span class="kind">${esc(n.kind.toUpperCase())}</span><b>${esc(n.name)}</b>${extra}<div class="mut">${n.ip?esc(n.ip)+' · ':''}${n.address?esc(n.address)+' · ':''}${esc(n.connection||'')}</div>`;b.onclick=()=>detail(n);return b}function fill(id,arr,extraFn){let box=$(id);box.innerHTML='';if(!arr.length){box.innerHTML='<div class="mut" style="margin-top:7px">Ничего не найдено.</div>';return}for(let n of arr)box.appendChild(item(n,extraFn?extraFn(n):''))}
async function poll(){try{let s=await(await fetch('/api/state')).json();S.nodes=s.nodes;$('version').textContent='v'+s.version;$('status').textContent=s.scan.running?'scanning…':'live · '+(s.scan.last_scan?new Date(s.scan.last_scan*1000).toLocaleTimeString():'waiting');let lan=s.scan.lan||[],wifi=s.scan.wifi||[],ble=s.scan.ble||[],agents=s.agents||[];$('nLan').textContent=lan.length;$('nWifi').textContent=wifi.length;$('nBle').textContent=ble.length;$('nAgents').textContent=agents.length;$('bLan').textContent='LAN '+lan.length;$('bWifi').textContent='Wi-Fi '+wifi.length;$('bBle').textContent='BLE '+ble.length;$('bAgents').textContent='Agents '+agents.length;fill('lanList',lan);fill('wifiList',wifi,n=>` <span class="dist">≈ ${n.distance?.approx_m??'?'} м</span>`);fill('bleList',ble,n=>` <span class="dist">≈ ${n.distance?.approx_m??'?'} м</span>`);fill('agentList',agents);draw()}catch(e){$('status').textContent='backend unavailable'}}async function info(){try{let s=await(await fetch('/api/info')).json();$('qr').src=s.qr;$('code').textContent=s.code;$('url').textContent=s.join_url}catch(e){}}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.p).classList.add('active')});$('rescan').onclick=async()=>{await fetch('/api/rescan',{method:'POST'});$('status').textContent='scanning…'};c.onpointerdown=e=>{S.drag=true;S.dx=S.lx=e.clientX;S.dy=S.ly=e.clientY;c.setPointerCapture?.(e.pointerId)};c.onpointermove=e=>{if(!S.drag)return;S.yaw+=(e.clientX-S.lx)*.008;S.pitch=Math.max(-1.05,Math.min(1.05,S.pitch+(e.clientY-S.ly)*.008));S.lx=e.clientX;S.ly=e.clientY;draw()};c.onpointerup=()=>S.drag=false;c.onclick=e=>{if(Math.hypot(e.clientX-S.dx,e.clientY-S.dy)>6)return;let r=c.getBoundingClientRect(),X=e.clientX-r.left,Y=e.clientY-r.top,best=null,bd=999;for(let q of S.p){let d=Math.hypot(X-q.p.x,Y-q.p.y);if(d<q.r+8&&d<bd){best=q.n;bd=d}}if(best)detail(best)};c.onwheel=e=>{e.preventDefault();S.zoom=Math.max(.5,Math.min(2.3,S.zoom*(e.deltaY>0?.9:1.1)));draw()};setInterval(poll,1700);info();poll();resize();
</script></body></html>'''

PHONE = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spatial Node</title><style>:root{color-scheme:dark}body{background:#081018;color:#eef6ff;font:15px system-ui;padding:16px}.card{max-width:520px;margin:0 auto 10px;background:#101b28;border:1px solid #2a3a50;border-radius:12px;padding:13px}button,input{width:100%;padding:11px;margin-top:8px;border-radius:8px;border:1px solid #35506b;background:#1c3248;color:#fff}.ok{color:#34d399}.bad{color:#fb7185}.mut{color:#9aacbf;font-size:12px}</style></head><body><div class="card"><h2>Spatial Node</h2><div class="mut">Телефон становится сенсорным агентом Spatial Hub.</div><input id="name" value="iPhone 16"><button id="join">Подключиться</button><button id="motion">Разрешить движение</button><button id="geo">Разрешить GPS</button><div id="status"></div></div><script>const q=new URLSearchParams(location.search),token=q.get('token')||'',$=x=>document.getElementById(x);let joined=false,sensors={};let nid='phone-'+Date.now()+'-'+Math.random().toString(36).slice(2);try{nid=localStorage.getItem('shnid')||nid;localStorage.setItem('shnid',nid)}catch(e){};$('motion').onclick=async()=>{try{if(typeof DeviceOrientationEvent!=='undefined'&&DeviceOrientationEvent.requestPermission){if(await DeviceOrientationEvent.requestPermission()!=='granted')throw Error('denied')}addEventListener('deviceorientation',e=>sensors.orientation={alpha:e.alpha,beta:e.beta,gamma:e.gamma});addEventListener('devicemotion',e=>{let a=e.accelerationIncludingGravity||{};sensors.motion={x:a.x,y:a.y,z:a.z}});$('motion').disabled=true}catch(e){}};$('geo').onclick=()=>navigator.geolocation?.watchPosition(p=>sensors.geo={lat:p.coords.latitude,lon:p.coords.longitude,accuracy:p.coords.accuracy},()=>{}, {enableHighAccuracy:true});async function hb(){if(!joined)return;let t=performance.now();try{await fetch('/api/ping');sensors.latency_ms=performance.now()-t;let r=await fetch('/api/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,node_id:nid,name:$('name').value,ua:navigator.userAgent,sensors})});if(!r.ok)throw Error('rejected');$('status').innerHTML='<span class="ok">подключено · отправка телеметрии</span>'}catch(e){$('status').innerHTML='<span class="bad">ошибка связи</span>'}};$('join').onclick=()=>{joined=true;$('join').disabled=true;hb()};setInterval(hb,1800);</script></body></html>'''

def local_only():
    return request.remote_addr in ("127.0.0.1", "::1")

@app.get("/")
def dashboard():
    if not local_only(): return Response("local only", 403)
    return Response(DASH, mimetype="text/html")

@app.get("/node")
def node_page():
    if request.args.get("token") != token: return Response("bad token", 403)
    return Response(PHONE, mimetype="text/html")

@app.get("/api/ping")
def api_ping(): return jsonify({"t": time.time()})

@app.get("/api/info")
def api_info():
    if not local_only(): return Response("local only", 403)
    url = f"http://{HOST}:{PORT}/node?token={token}"
    img = qrcode.make(url); bio = io.BytesIO(); img.save(bio, format="PNG")
    qr = "data:image/png;base64," + base64.b64encode(bio.getvalue()).decode()
    return jsonify({"code": code, "join_url": url, "qr": qr})

@app.post("/api/rescan")
def api_rescan():
    if not local_only(): return Response("local only", 403)
    threading.Thread(target=perform_scan, daemon=True).start()
    return jsonify({"ok": True})

@app.get("/api/state")
def api_state():
    if not local_only(): return Response("local only", 403)
    t = time.time()
    with lock:
        agents = []
        for n in state["agents"].values():
            x = dict(n); x["online"] = t - x.get("last_seen", 0) < 8; agents.append(x)
        scan = {k: list(state["scan"][k]) for k in ("lan", "wifi", "ble")}
        scan.update({"last_scan": state["scan"]["last_scan"], "running": state["scan"]["running"], "errors": list(state["scan"]["errors"])})
        nodes = [hub_node()] + scan["lan"] + scan["wifi"] + scan["ble"] + agents
        return jsonify({"version": current_version(), "nodes": nodes, "agents": agents, "scan": scan, "events": list(state["events"])[:100], "update": state["update"]})

@app.post("/api/heartbeat")
def api_heartbeat():
    d = request.get_json(silent=True) or {}
    if d.get("token") != token: return jsonify({"error": "token"}), 403
    nid = str(d.get("node_id", "")).strip()
    if not nid: return jsonify({"error": "id"}), 400
    with lock:
        first = nid not in state["agents"]
        node = {"id": nid, "kind": "agent", "name": str(d.get("name", "Phone"))[:80], "ua": str(d.get("ua", ""))[:400],
                "sensors": d.get("sensors") or {}, "remote_ip": request.remote_addr, "online": True, "last_seen": time.time(),
                "connection": "Spatial Node agent / Wi-Fi", "position": logical_position(nid, "agent"),
                "distance": {"available": False, "note": "The phone web agent currently supplies telemetry but not calibrated radio ranging."}}
        state["agents"][nid] = node
    if first:
        event(f"{node['name']} connected as Spatial Node"); save_agents()
    return jsonify({"ok": True})

def update_loop():
    while not stop.wait(600):
        try:
            req = urllib.request.Request(MANIFEST + "?t=" + str(int(time.time())), headers={"User-Agent": "SpatialHub"})
            remote = json.loads(urllib.request.urlopen(req, timeout=10).read().decode()).get("version")
            if remote and remote != current_version():
                state["update"] = remote; event(f"Update {remote} found; restarting through updater"); save_agents(); restart_update(); return
        except Exception as exc:
            log(f"update: {exc}")

def restart_update():
    if not LAUNCHER: return
    try:
        py = Path(sys.executable); candidate = py.with_name("pythonw.exe"); py = candidate if candidate.exists() else py
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([str(py), LAUNCHER, "--update-and-start"], creationflags=flags, close_fds=True)
        os._exit(0)
    except Exception as exc:
        log(f"restart update: {exc}")

def server_thread():
    serve(app, host="0.0.0.0", port=PORT, threads=12)

def make_icon():
    im = Image.new("RGB", (64, 64), "#0b1623"); d = ImageDraw.Draw(im)
    d.ellipse((8, 8, 56, 56), outline="#67e8f9", width=4); d.ellipse((25, 25, 39, 39), fill="#67e8f9")
    d.line((32, 8, 32, 25), fill="#67e8f9", width=3); d.line((32, 39, 32, 56), fill="#67e8f9", width=3)
    return im

def tray_loop():
    global tray_ref
    def open_app(icon=None, item=None):
        try:
            if window_ref: window_ref.show(); window_ref.restore()
        except Exception: pass
    def rescan(icon=None, item=None):
        threading.Thread(target=perform_scan, daemon=True).start()
    def exit_app(icon=None, item=None):
        stop.set()
        try:
            if tray_ref: tray_ref.stop()
        except Exception: pass
        try:
            if window_ref: window_ref.destroy()
        except Exception: pass
    tray_ref = pystray.Icon("SpatialHub", make_icon(), "Spatial Hub", menu=pystray.Menu(
        pystray.MenuItem("Open Spatial Hub", open_app, default=True),
        pystray.MenuItem("Scan now", rescan),
        pystray.MenuItem("Exit", exit_app)))
    tray_ref.run()

def on_closing():
    try:
        window_ref.hide()
        return False
    except Exception:
        return True

def main():
    global window_ref
    event("Spatial Hub Desktop started")
    threading.Thread(target=server_thread, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=tray_loop, daemon=True).start()
    time.sleep(0.7)
    window_ref = webview.create_window("Spatial Hub", f"http://127.0.0.1:{PORT}/", width=1280, height=820, min_size=(960, 620), background_color="#070c13")
    try: window_ref.events.closing += on_closing
    except Exception: pass
    webview.start(debug=False)
    stop.set(); save_agents()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise
