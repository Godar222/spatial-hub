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
import webbrowser
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, jsonify, request, Response
from waitress import serve
import psutil
import qrcode
import pystray
from PIL import Image, ImageDraw

try:
    sys.coinit_flags = 0
except Exception:
    pass

try:
    from bleak import BleakScanner
    BLEAK_OK = True
except Exception:
    BLEAK_OK = False

try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    ZEROCONF_OK = True
except Exception:
    ZEROCONF_OK = False

import webview

APP_VERSION = "0.5.0"
PORT = 8765

ROOT = Path(os.environ.get("SPATIAL_HUB_ROOT", Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpatialHub"))
APPDIR = ROOT / "app"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
SNAPSHOTS = DATA / "snapshots"
for p in (DATA, LOGS, SNAPSHOTS):
    p.mkdir(parents=True, exist_ok=True)

LOG = LOGS / "app.log"
VERSION = APPDIR / "version.json"
MANIFEST = os.environ.get("SPATIAL_HUB_MANIFEST", "https://raw.githubusercontent.com/Godar222/spatial-hub/main/update/manifest.json")
LAUNCHER = os.environ.get("SPATIAL_HUB_LAUNCHER", "")

app = Flask(__name__)
lock = threading.RLock()
scan_lock = threading.Lock()
stop = threading.Event()

token = secrets.token_urlsafe(24)
paircode = f"{secrets.randbelow(1000000):06d}"
window = None

history = defaultdict(lambda: deque(maxlen=180))
events = deque(maxlen=500)

state = {
    "agents": {},
    "scan": {
        "lan": [],
        "wifi": [],
        "ble": [],
        "services": [],
        "last_scan": 0,
        "running": False,
        "errors": [],
        "duration_ms": 0,
    },
    "environment": {},
}

def log(x):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {x}\n")
    except Exception:
        pass

def event(kind, text, node_id=None):
    with lock:
        events.appendleft({"t": time.time(), "kind": kind, "text": str(text)[:240], "node_id": node_id})

def version():
    try:
        return json.loads(VERSION.read_text(encoding="utf-8")).get("version", APP_VERSION)
    except Exception:
        return APP_VERSION

def local_ip():
    for target in (("1.1.1.1", 80), ("8.8.8.8", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(target)
            x = s.getsockname()[0]
            s.close()
            if x and not x.startswith("127."):
                return x
        except Exception:
            pass
    return "127.0.0.1"

HOST = local_ip()

def cmd(s, timeout=12):
    try:
        p = subprocess.run(
            ["cmd.exe", "/d", "/s", "/c", f"chcp 65001>nul & {s}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.stdout.decode("utf-8", "replace")
    except Exception as e:
        log(f"cmd {s}: {e}")
        return ""

def stable_vec(key, radius=4.0):
    h = hashlib.sha256(str(key).encode("utf-8", "ignore")).digest()
    a = int.from_bytes(h[:4], "big") / 2**32 * math.tau
    e = ((h[4] / 255.0) - 0.5) * 0.7
    return math.cos(a) * radius, e * radius * 0.45, math.sin(a) * radius

def pos(key, kind, dist=None, special=None):
    if kind == "hub":
        return {"x": 0, "y": 0, "z": 0, "uncertainty": 0.1, "direction_known": True}
    if kind == "internet":
        return {"x": 0, "y": 2.0, "z": -8.2, "uncertainty": 0.0, "direction_known": False}
    if special == "gateway":
        return {"x": 0, "y": 0.25, "z": -4.1, "uncertainty": 2.4, "direction_known": False}
    if dist is None:
        radius = {"agent": 2.7, "lan": 5.4, "service": 4.8}.get(kind, 4.3)
    else:
        radius = max(1.1, min(6.5, 1.1 + math.log1p(max(0.15, dist)) * 1.65))
    x, y, z = stable_vec(key, radius)
    return {
        "x": x,
        "y": y,
        "z": z,
        "uncertainty": 3.0 if dist is None else max(0.8, min(4.5, 0.8 + dist * 0.8)),
        "direction_known": False,
    }

def rough(rssi, ref=-42, loss=2.7):
    try:
        return max(0.25, min(80, 10 ** ((ref - float(rssi)) / (10 * loss))))
    except Exception:
        return None

def hub():
    b = psutil.sensors_battery()
    m = psutil.virtual_memory()
    temps = {}
    try:
        for k, vals in (psutil.sensors_temperatures() or {}).items():
            if vals:
                temps[k] = vals[0].current
    except Exception:
        pass
    return {
        "id": "hub",
        "kind": "hub",
        "device_type": "computer",
        "name": socket.gethostname(),
        "online": True,
        "connection": "local",
        "position": pos("hub", "hub"),
        "telemetry": {
            "cpu": psutil.cpu_percent(None),
            "ram": m.percent,
            "battery": getattr(b, "percent", None),
            "temperatures_c": temps,
        },
        "services": [],
        "capabilities": ["hub", "scanner", "phone-agent-server"],
    }

def network_info():
    stats = psutil.net_if_stats()
    choices = []
    for name, addrs in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.address or a.address.startswith("127.") or not a.netmask:
                continue
            try:
                ip = ipaddress.ip_address(a.address)
                if not ip.is_private:
                    continue
                net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                score = (100 if any(k in name.lower() for k in ("wi-fi", "wifi", "wlan")) else 0) + (80 if a.address == HOST else 0)
                choices.append((score, name, a.address, net))
            except Exception:
                pass
    if not choices:
        return None, None, None
    _, n, ip, net = max(choices, key=lambda x: x[0])
    if net.num_addresses > 256:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return n, ip, net

def ping(ip, timeout_ms=220):
    try:
        return subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).returncode == 0
    except Exception:
        return False

def ping_ms(ip):
    started = time.perf_counter()
    ok = ping(ip, 500)
    return ok, round((time.perf_counter() - started) * 1000, 1)

def gateway():
    for line in cmd("route print -4", 5).splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0":
            try:
                ipaddress.ip_address(p[2])
                return p[2]
            except Exception:
                pass
    return None

def reverse_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""

def classify(name="", services=None, gateway_flag=False):
    t = (" ".join([name] + [str(x) for x in (services or [])])).lower()
    if gateway_flag:
        return "router"
    if any(x in t for x in ("googlecast", "chromecast", "mediarenderer", "airplay", "raop", "roku", "tv")):
        return "media"
    if any(x in t for x in ("printer", "_ipp", "_ipps", "_pdl-datastream")):
        return "printer"
    if any(x in t for x in ("homekit", "_hap.", "accessory")):
        return "smart-home"
    if any(x in t for x in ("iphone", "ipad", "android", "phone")):
        return "phone"
    if any(x in t for x in ("nas", "synology", "qnap")):
        return "storage"
    return "unknown"

def scan_lan():
    iface, own, net = network_info()
    if not net:
        return [], "Не найдена активная частная IPv4-сеть."
    hosts = [str(h) for h in net.hosts() if str(h) != own]
    with ThreadPoolExecutor(max_workers=64) as ex:
        for _ in as_completed([ex.submit(ping, h) for h in hosts]):
            pass

    rows = {}
    for line in cmd("arp -a", 5).splitlines():
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f-]{17})\s+", line)
        if m:
            rows[m.group(1)] = m.group(2).replace("-", ":").upper()

    gw = gateway()
    out = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        names = {ip: f for ip, f in ((ip, ex.submit(reverse_name, ip)) for ip in rows)}
        for ip, mac in rows.items():
            try:
                if ipaddress.ip_address(ip) not in net or ip == own:
                    continue
            except Exception:
                continue
            isgw = ip == gw
            host = ""
            try:
                host = names[ip].result(timeout=0.5)
            except Exception:
                pass
            label = "Router / gateway" if isgw else (host or f"LAN device {ip}")
            out.append({
                "id": f"lan:{mac or ip}",
                "kind": "lan",
                "device_type": "router" if isgw else classify(host),
                "name": label,
                "hostname": host,
                "ip": ip,
                "address": mac,
                "gateway": isgw,
                "connection": "LAN / ARP",
                "online": True,
                "position": pos(mac or ip, "lan", special="gateway" if isgw else None),
                "distance": {"available": False, "note": "LAN сообщает сетевую принадлежность, но не физическое расстояние."},
                "services": [],
                "capabilities": ["ping"] + (["wake-on-lan"] if mac else []),
            })
    if gw and not any(x["ip"] == gw for x in out):
        out.append({
            "id": f"lan:{gw}",
            "kind": "lan",
            "device_type": "router",
            "name": "Router / gateway",
            "hostname": "",
            "ip": gw,
            "address": "",
            "gateway": True,
            "connection": "Default route",
            "online": True,
            "position": pos(gw, "lan", special="gateway"),
            "distance": {"available": False, "note": "Шлюз найден из таблицы маршрутов."},
            "services": [],
            "capabilities": ["ping", "web-ui"],
        })
    return out, None

def parse_wifi_networks(text):
    out, ssid, cur = [], "", None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line, re.I)
        if m and not line.upper().startswith("BSSID"):
            ssid = m.group(1).strip()
            continue
        m = re.match(r"^BSSID\s+\d+\s*:\s*([0-9A-Fa-f:-]{11,})", line, re.I)
        if m:
            if cur:
                out.append(cur)
            cur = {"ssid": ssid, "bssid": m.group(1).replace("-", ":").upper(), "q": None, "channel": None, "radio": None}
            continue
        if cur:
            m = re.search(r":\s*(\d{1,3})\s*%\s*$", line)
            if m and cur["q"] is None:
                cur["q"] = max(0, min(100, int(m.group(1))))
                continue
            m = re.match(r"(?:Channel|Канал|Kanál)\s*:\s*(\d+)", line, re.I)
            if m:
                cur["channel"] = int(m.group(1))
                continue
            m = re.match(r"(?:Radio type|Тип радио|Typ rádia)\s*:\s*(.+)", line, re.I)
            if m:
                cur["radio"] = m.group(1).strip()
    if cur:
        out.append(cur)
    return out

def connected_wifi():
    text = cmd("netsh wlan show interfaces", 8)
    ssid = bssid = None
    q = channel = None
    radio = None
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^SSID\s*:", line, re.I):
            ssid = line.split(":", 1)[1].strip()
        elif re.match(r"^BSSID\s*:", line, re.I):
            bssid = line.split(":", 1)[1].strip().replace("-", ":").upper()
        else:
            m = re.search(r":\s*(\d{1,3})\s*%\s*$", line)
            if m and q is None:
                q = int(m.group(1))
            m = re.match(r"(?:Channel|Канал|Kanál)\s*:\s*(\d+)", line, re.I)
            if m:
                channel = int(m.group(1))
            m = re.match(r"(?:Radio type|Тип радио|Typ rádia)\s*:\s*(.+)", line, re.I)
            if m:
                radio = m.group(1).strip()
    return {"ssid": ssid, "bssid": bssid, "q": q, "channel": channel, "radio": radio}

def scan_wifi():
    raw = cmd("netsh wlan show networks mode=bssid", 12)
    items = parse_wifi_networks(raw)
    conn = connected_wifi()
    warning = None
    low = raw.lower()
    if ("location" in low and ("permission" in low or "access" in low)) or "местополож" in low:
        warning = "Windows требует разрешение Location для просмотра соседних Wi‑Fi сетей."
    if not items and conn.get("bssid"):
        items = [conn]

    out = []
    for i in items:
        rssi = (i["q"] / 2 - 100) if i.get("q") is not None else None
        d = rough(rssi) if rssi is not None else None
        connected = bool(conn.get("bssid") and i.get("bssid") == conn.get("bssid"))
        out.append({
            "id": f"wifi:{i['bssid']}",
            "kind": "wifi",
            "device_type": "access-point",
            "name": i.get("ssid") or "Hidden Wi‑Fi",
            "ssid": i.get("ssid") or "",
            "address": i["bssid"],
            "signal": i.get("q"),
            "rssi": round(rssi, 1) if rssi is not None else None,
            "channel": i.get("channel"),
            "radio": i.get("radio"),
            "connected": connected,
            "connection": "Wi‑Fi AP" + (" / connected" if connected else " / observed"),
            "online": True,
            "position": pos(i["bssid"], "wifi", d),
            "distance": {
                "available": d is not None,
                "approx_m": round(d, 1) if d is not None else None,
                "confidence": "very low",
                "note": "RSSI-оценка. Стены, антенны и мощность AP могут менять результат в несколько раз.",
            },
            "services": [],
            "capabilities": ["radio-observation"],
        })
    return out, (None if out else warning or "Wi‑Fi BSSID не получен."), warning, conn

async def ble_do():
    found = await BleakScanner.discover(timeout=4.5, return_adv=True)
    out = []
    if isinstance(found, dict):
        for v in found.values():
            try:
                dev, adv = v
                addr = getattr(dev, "address", "") or ""
                name = getattr(dev, "name", None) or getattr(adv, "local_name", None) or "BLE device"
                rssi = getattr(adv, "rssi", None)
                tx = getattr(adv, "tx_power", None)
                ref = float(tx) if tx is not None and -100 < float(tx) < 20 else -59
                d = rough(rssi, ref, 2.5) if rssi is not None else None
                out.append({
                    "id": f"ble:{addr or name}",
                    "kind": "ble",
                    "device_type": classify(name),
                    "name": name,
                    "address": addr,
                    "rssi": rssi,
                    "tx_power": tx,
                    "connection": "BLE advertisement",
                    "online": True,
                    "position": pos(addr or name, "ble", d),
                    "distance": {
                        "available": d is not None,
                        "approx_m": round(d, 1) if d is not None else None,
                        "confidence": "very low",
                        "note": "BLE RSSI показывает только грубую близость.",
                    },
                    "services": [],
                    "capabilities": ["ble-observed"],
                })
            except Exception:
                pass
    return out

def scan_ble():
    if not BLEAK_OK:
        return [], "Bluetooth library не загрузилась."
    try:
        return asyncio.run(ble_do()), None
    except Exception as e:
        return [], f"Bluetooth: {e}"

def scan_ssdp(timeout=1.6):
    results = []
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "MX: 1",
        "ST: ssdp:all",
        "", ""
    ]).encode("ascii")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.25)
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        seen = set()
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                break
            text = data.decode("utf-8", "replace")
            headers = {}
            for line in text.splitlines()[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            key = (addr[0], headers.get("usn", ""), headers.get("st", ""))
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "source": "ssdp",
                "ip": addr[0],
                "name": headers.get("server") or headers.get("st") or "SSDP service",
                "type": headers.get("st", ""),
                "location": headers.get("location", ""),
                "usn": headers.get("usn", ""),
                "server": headers.get("server", ""),
            })
    finally:
        s.close()
    return results

def scan_mdns(timeout=2.2):
    if not ZEROCONF_OK:
        return [], "mDNS/Bonjour library unavailable"
    types = [
        "_airplay._tcp.local.",
        "_raop._tcp.local.",
        "_googlecast._tcp.local.",
        "_http._tcp.local.",
        "_https._tcp.local.",
        "_ipp._tcp.local.",
        "_ipps._tcp.local.",
        "_printer._tcp.local.",
        "_hap._tcp.local.",
        "_companion-link._tcp.local.",
        "_spotify-connect._tcp.local.",
    ]
    found = []
    found_keys = set()
    zc = Zeroconf()

    def on_service(zc_obj, service_type, name, state_change):
        if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return
        try:
            info = zc_obj.get_service_info(service_type, name, timeout=600)
            if not info:
                return
            addresses = info.parsed_addresses()
            props = {}
            for k, v in (info.properties or {}).items():
                try:
                    kk = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
                    vv = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                    props[kk] = vv
                except Exception:
                    pass
            for ip in addresses:
                key = (ip, service_type, name)
                if key in found_keys:
                    continue
                found_keys.add(key)
                found.append({
                    "source": "mdns",
                    "ip": ip,
                    "name": name.rstrip("."),
                    "type": service_type,
                    "port": info.port,
                    "server": (info.server or "").rstrip("."),
                    "properties": props,
                })
        except Exception:
            pass

    try:
        browsers = [ServiceBrowser(zc, t, handlers=[on_service]) for t in types]
        time.sleep(timeout)
    finally:
        try:
            zc.close()
        except Exception:
            pass
    return found, None

def merge_services(lan, services):
    by_ip = {n.get("ip"): n for n in lan if n.get("ip")}
    for svc in services:
        ip = svc.get("ip")
        if ip in by_ip:
            node = by_ip[ip]
            node.setdefault("services", []).append(svc)
            texts = [x.get("type", "") + " " + x.get("name", "") for x in node["services"]]
            node["device_type"] = classify(node.get("hostname") or node.get("name", ""), texts, node.get("gateway", False))
            caps = set(node.get("capabilities") or [])
            if any("http" in (x.get("type", "").lower()) for x in node["services"]) or node.get("gateway"):
                caps.add("web-ui")
            if any("airplay" in (x.get("type", "").lower()) for x in node["services"]):
                caps.add("airplay-advertised")
            if any("googlecast" in (x.get("type", "").lower()) for x in node["services"]):
                caps.add("cast-advertised")
            if any("ipp" in (x.get("type", "").lower()) for x in node["services"]):
                caps.add("printing-advertised")
            node["capabilities"] = sorted(caps)
    return lan

def internet_status():
    ok, ms = ping_ms("1.1.1.1")
    return ok, ms

def push_history(nodes):
    t = time.time()
    for n in nodes:
        sample = {"t": t}
        if n.get("kind") == "wifi":
            sample.update({"signal": n.get("signal"), "rssi": n.get("rssi"), "distance": (n.get("distance") or {}).get("approx_m")})
        elif n.get("kind") == "ble":
            sample.update({"rssi": n.get("rssi"), "distance": (n.get("distance") or {}).get("approx_m")})
        elif n.get("kind") == "agent":
            s = n.get("sensors") or {}
            sample.update({"latency": s.get("latency_ms"), "battery": (s.get("battery") or {}).get("level")})
        else:
            continue
        history[n["id"]].append(sample)

def perform_scan():
    if not scan_lock.acquire(False):
        return
    started = time.perf_counter()
    try:
        with lock:
            state["scan"]["running"] = True
            state["scan"]["errors"] = []
        errors = []

        try:
            wifi, e, warn, conn = scan_wifi()
        except Exception as x:
            wifi, e, warn, conn = [], f"Wi‑Fi: {x}", None, {}
        if e:
            errors.append(e)
        elif warn:
            errors.append(warn)

        try:
            lan, e = scan_lan()
        except Exception as x:
            lan, e = [], f"LAN: {x}"
        if e:
            errors.append(e)

        try:
            ble, e = scan_ble()
        except Exception as x:
            ble, e = [], f"Bluetooth: {x}"
        if e:
            errors.append(e)

        services = []
        try:
            services.extend(scan_ssdp())
        except Exception as x:
            errors.append(f"SSDP: {x}")
        try:
            mdns, e = scan_mdns()
            services.extend(mdns)
            if e:
                errors.append(e)
        except Exception as x:
            errors.append(f"mDNS: {x}")

        lan = merge_services(lan, services)
        internet_ok, internet_ms = internet_status()
        gw = gateway()

        with lock:
            state["scan"].update({
                "lan": lan,
                "wifi": wifi,
                "ble": ble,
                "services": services,
                "last_scan": time.time(),
                "running": False,
                "errors": errors,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            })
            state["environment"] = {
                "host_ip": HOST,
                "gateway": gw,
                "internet_reachable": internet_ok,
                "internet_ping_ms": internet_ms,
                "connected_ssid": conn.get("ssid"),
                "connected_bssid": conn.get("bssid"),
                "interface": network_info()[0],
            }

        push_history(wifi + ble)
        event("scan", f"Discovery: LAN {len(lan)}, Wi‑Fi {len(wifi)}, BLE {len(ble)}, services {len(services)}")
    finally:
        with lock:
            state["scan"]["running"] = False
        scan_lock.release()

def agents_public():
    t = time.time()
    out = []
    with lock:
        for n in state["agents"].values():
            q = dict(n)
            q["online"] = t - q.get("last_seen", 0) < 9
            out.append(q)
    return out

def all_nodes():
    with lock:
        lan = list(state["scan"]["lan"])
        wifi = list(state["scan"]["wifi"])
        ble = list(state["scan"]["ble"])
        env = dict(state["environment"])
    nodes = [hub()] + lan + wifi + ble + agents_public()
    if env.get("gateway"):
        nodes.append({
            "id": "internet",
            "kind": "internet",
            "device_type": "internet",
            "name": "Internet",
            "online": bool(env.get("internet_reachable")),
            "connection": "WAN",
            "position": pos("internet", "internet"),
            "distance": {"available": False},
            "services": [],
            "capabilities": [],
            "telemetry": {"ping_ms": env.get("internet_ping_ms")},
        })
    return nodes

def topology_links(nodes):
    by_id = {n["id"]: n for n in nodes}
    links = []
    gw = next((n for n in nodes if n.get("kind") == "lan" and n.get("gateway")), None)
    connected_ap = next((n for n in nodes if n.get("kind") == "wifi" and n.get("connected")), None)

    if connected_ap:
        links.append({"a": "hub", "b": connected_ap["id"], "type": "wifi-association", "status": "confirmed", "label": "connected Wi‑Fi"})
    if gw:
        links.append({"a": "hub", "b": gw["id"], "type": "network-path", "status": "confirmed", "label": "default route"})
        if "internet" in by_id:
            links.append({"a": gw["id"], "b": "internet", "type": "wan", "status": "confirmed", "label": "WAN"})
        for n in nodes:
            if n.get("kind") == "lan" and n["id"] != gw["id"]:
                links.append({"a": gw["id"], "b": n["id"], "type": "lan-membership", "status": "confirmed", "label": "same LAN"})
        if connected_ap:
            links.append({"a": connected_ap["id"], "b": gw["id"], "type": "ap-uplink", "status": "inferred", "label": "probable uplink"})

    for n in nodes:
        if n.get("kind") == "ble":
            links.append({"a": "hub", "b": n["id"], "type": "ble-observed", "status": "observed", "label": "BLE"})
        elif n.get("kind") == "agent":
            links.append({"a": "hub", "b": n["id"], "type": "agent", "status": "confirmed", "label": "Spatial Node"})

    groups = defaultdict(list)
    for n in nodes:
        if n.get("kind") == "wifi" and n.get("ssid"):
            groups[n["ssid"]].append(n)
    for ssid, aps in groups.items():
        if len(aps) > 1:
            base = aps[0]
            for other in aps[1:]:
                links.append({"a": base["id"], "b": other["id"], "type": "same-ssid", "status": "inferred", "label": f"same SSID: {ssid}"})
    return links

def localonly():
    return request.remote_addr in ("127.0.0.1", "::1")

def html(name):
    return (APPDIR / name).read_text(encoding="utf-8")

def valid_private_ip(ip):
    try:
        x = ipaddress.ip_address(ip)
        return x.is_private and not x.is_loopback
    except Exception:
        return False

def find_node(node_id):
    return next((n for n in all_nodes() if n.get("id") == node_id), None)

def wake_on_lan(mac):
    raw = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(raw) != 12:
        raise ValueError("invalid MAC")
    packet = bytes.fromhex("FF" * 6 + raw * 16)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))
    finally:
        s.close()

@app.get("/")
def root():
    return Response(html("dashboard.html"), mimetype="text/html") if localonly() else Response("local only", 403)

@app.get("/node")
def nodepage():
    return Response(html("phone.html"), mimetype="text/html") if request.args.get("token") == token else Response("bad token", 403)

@app.get("/api/ping")
def apiping():
    return jsonify({"t": time.time()})

@app.get("/api/info")
def apiinfo():
    if not localonly():
        return Response("local only", 403)
    url = f"http://{HOST}:{PORT}/node?token={token}"
    im = qrcode.make(url)
    b = io.BytesIO()
    im.save(b, format="PNG")
    return jsonify({"code": paircode, "url": url, "qr": "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()})

@app.get("/api/state")
def apistate():
    if not localonly():
        return Response("local only", 403)
    nodes = all_nodes()
    with lock:
        sc = {
            "lan": list(state["scan"]["lan"]),
            "wifi": list(state["scan"]["wifi"]),
            "ble": list(state["scan"]["ble"]),
            "services": list(state["scan"]["services"]),
            "last_scan": state["scan"]["last_scan"],
            "running": state["scan"]["running"],
            "errors": list(state["scan"]["errors"]),
            "duration_ms": state["scan"]["duration_ms"],
        }
        env = dict(state["environment"])
    return jsonify({
        "version": version(),
        "nodes": nodes,
        "links": topology_links(nodes),
        "scan": sc,
        "agents": agents_public(),
        "environment": env,
        "events": list(events)[:120],
    })

@app.get("/api/history")
def apihistory():
    if not localonly():
        return Response("local only", 403)
    node_id = request.args.get("id", "")
    with lock:
        return jsonify({"id": node_id, "samples": list(history.get(node_id, []))})

@app.post("/api/scan")
def apiscan():
    if not localonly():
        return Response("local only", 403)
    threading.Thread(target=perform_scan, daemon=True).start()
    return jsonify({"ok": True})

@app.post("/api/snapshot")
def apisnapshot():
    if not localonly():
        return Response("local only", 403)
    nodes = all_nodes()
    data = {
        "created": time.time(),
        "version": version(),
        "nodes": nodes,
        "links": topology_links(nodes),
        "environment": state.get("environment", {}),
    }
    name = time.strftime("snapshot-%Y%m%d-%H%M%S.json")
    path = SNAPSHOTS / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    event("snapshot", f"Saved {name}")
    return jsonify({"ok": True, "file": str(path)})

@app.post("/api/action/ping")
def action_ping():
    if not localonly():
        return Response("local only", 403)
    d = request.get_json(silent=True) or {}
    node = find_node(str(d.get("id") or ""))
    if not node or not node.get("ip") or not valid_private_ip(node["ip"]):
        return jsonify({"error": "No private IP for this device"}), 400
    ok, ms = ping_ms(node["ip"])
    return jsonify({"ok": ok, "ms": ms})

@app.post("/api/action/wol")
def action_wol():
    if not localonly():
        return Response("local only", 403)
    d = request.get_json(silent=True) or {}
    node = find_node(str(d.get("id") or ""))
    if not node or not node.get("address"):
        return jsonify({"error": "No MAC address"}), 400
    try:
        wake_on_lan(node["address"])
        event("action", f"Wake-on-LAN sent to {node.get('name')}", node.get("id"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.post("/api/action/open-web")
def action_open_web():
    if not localonly():
        return Response("local only", 403)
    d = request.get_json(silent=True) or {}
    node = find_node(str(d.get("id") or ""))
    if not node or not node.get("ip") or not valid_private_ip(node["ip"]):
        return jsonify({"error": "No private IP"}), 400
    url = f"http://{node['ip']}/"
    try:
        webbrowser.open(url)
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/heartbeat")
def heartbeat():
    d = request.get_json(silent=True) or {}
    if d.get("token") != token:
        return jsonify({"error": "token"}), 403
    nid = str(d.get("node_id") or "")
    if not nid:
        return jsonify({"error": "id"}), 400
    sensors = d.get("sensors") or {}
    agent = {
        "id": nid,
        "kind": "agent",
        "device_type": "phone",
        "name": str(d.get("name") or "Phone")[:80],
        "sensors": sensors,
        "last_seen": time.time(),
        "connection": "Spatial Node",
        "online": True,
        "position": pos(nid, "agent"),
        "distance": {"available": False, "note": "Phone browser agent does not expose precise radio ranging."},
        "services": [],
        "capabilities": ["motion"] if sensors.get("motion") else [],
    }
    with lock:
        state["agents"][nid] = agent
    push_history([agent])
    return jsonify({"ok": True})

def server():
    serve(app, host="0.0.0.0", port=PORT, threads=12)

def scanner():
    time.sleep(0.7)
    while not stop.is_set():
        perform_scan()
        if stop.wait(45):
            break

def updater():
    while not stop.wait(1800):
        try:
            m = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(MANIFEST + "?t=" + str(int(time.time())), headers={"User-Agent": "SpatialHub"}),
                    timeout=10,
                ).read().decode()
            )
            if m.get("version") != version() and LAUNCHER:
                py = Path(sys.executable)
                w = py.with_name("pythonw.exe")
                py = w if w.exists() else py
                subprocess.Popen([str(py), LAUNCHER], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                os._exit(0)
        except Exception as e:
            log(e)

def icon():
    im = Image.new("RGB", (64, 64), "#0a0a0b")
    d = ImageDraw.Draw(im)
    d.ellipse((9, 9, 55, 55), fill="#17181c", outline="#5ac8fa", width=3)
    d.ellipse((24, 24, 40, 40), fill="#f5f5f7")
    return im

def tray():
    def show(i, x):
        try:
            window.show()
        except Exception:
            pass
    def scan(i, x):
        threading.Thread(target=perform_scan, daemon=True).start()
    def snapshot(i, x):
        try:
            nodes = all_nodes()
            data = {"created": time.time(), "nodes": nodes, "links": topology_links(nodes)}
            name = time.strftime("snapshot-%Y%m%d-%H%M%S.json")
            (SNAPSHOTS / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log(e)
    def quit_(i, x):
        stop.set()
        try:
            i.stop()
        except Exception:
            pass
        os._exit(0)
    try:
        pystray.Icon(
            "SpatialHub",
            icon(),
            "Spatial Hub",
            pystray.Menu(
                pystray.MenuItem("Open", show, default=True),
                pystray.MenuItem("Scan now", scan),
                pystray.MenuItem("Save snapshot", snapshot),
                pystray.MenuItem("Exit", quit_),
            ),
        ).run()
    except Exception as e:
        log(e)

def main():
    global window
    threading.Thread(target=server, daemon=True).start()
    time.sleep(0.4)
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=updater, daemon=True).start()
    threading.Thread(target=tray, daemon=True).start()
    window = webview.create_window(
        "Spatial Hub",
        f"http://127.0.0.1:{PORT}",
        width=1500,
        height=940,
        min_size=(1080, 700),
        background_color="#0a0a0b",
    )
    webview.start(debug=False)

if __name__ == "__main__":
    main()
