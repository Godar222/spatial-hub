from __future__ import annotations
import asyncio
import json
import secrets
import threading
import time

from flask import Response, jsonify, request
from bleak import BleakClient

import core_v050 as core

APP_VERSION = "0.6.0"
TRUST_FILE = core.DATA / "trusted_devices.json"
PAIR_FILE = core.DATA / "pairing.json"
trust_lock = threading.RLock()
conn_lock = threading.RLock()
trusted = {}
connections = {}

def log(msg):
    try:
        core.log("v060 " + str(msg))
    except Exception:
        pass

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, value):
    try:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(e)

def load_pairing():
    d = load_json(PAIR_FILE, {})
    if d.get("token") and d.get("code"):
        return str(d["token"]), str(d["code"])
    token = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(1000000):06d}"
    save_json(PAIR_FILE, {"token": token, "code": code})
    return token, code

core.token, core.paircode = load_pairing()
x = load_json(TRUST_FILE, {})
trusted = x if isinstance(x, dict) else {}

def save_trusted():
    with trust_lock:
        save_json(TRUST_FILE, trusted)

def conn_set(node_id, state, detail="", capabilities=None):
    with conn_lock:
        connections[node_id] = {
            "state": state,
            "detail": str(detail)[:240],
            "updated": time.time(),
            "capabilities": list(capabilities or []),
        }

def enrich_one(node):
    n = dict(node)
    nid = n.get("id")
    with trust_lock:
        rec = dict(trusted.get(nid) or {})
    with conn_lock:
        con = dict(connections.get(nid) or {})
    n["trusted"] = bool(rec)
    n["auto_connect"] = bool(rec.get("auto_connect", False))
    n["connection_state"] = con.get("state") or ("connected" if n.get("kind") == "hub" else "discovered")
    n["connection_detail"] = con.get("detail", "")
    caps = set(n.get("capabilities") or [])
    if rec:
        caps.add("trusted")
    if n.get("kind") == "agent":
        caps.add("agent-channel")
    elif n.get("kind") == "lan" and rec:
        caps.add("trusted-lan")
    elif n.get("kind") == "ble" and rec:
        caps.add("ble-gatt-inspect")
    n["capabilities"] = sorted(caps)
    return n

orig_agents_public = core.agents_public
def agents_public_v060():
    return [enrich_one(n) for n in orig_agents_public()]
core.agents_public = agents_public_v060

orig_all_nodes = core.all_nodes
def all_nodes_v060():
    return [enrich_one(n) for n in orig_all_nodes()]
core.all_nodes = all_nodes_v060

def find_node(node_id):
    return next((n for n in core.all_nodes() if n.get("id") == node_id), None)

def trust_record(n):
    return {
        "id": n.get("id"),
        "name": n.get("name"),
        "kind": n.get("kind"),
        "ip": n.get("ip"),
        "address": n.get("address"),
        "auto_connect": True,
        "trusted_at": time.time(),
    }

async def ble_inspect(address):
    if not address:
        return {"ok": False, "error": "No BLE address"}
    try:
        client = BleakClient(address, timeout=6.0)
        await client.connect()
        try:
            services = []
            for svc in client.services:
                chars = []
                for ch in svc.characteristics:
                    chars.append({"uuid": ch.uuid, "properties": list(ch.properties or [])})
                services.append({"uuid": svc.uuid, "description": getattr(svc, "description", ""), "characteristics": chars})
            return {"ok": True, "service_count": len(services), "services": services[:40]}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": str(e)}

def safe_probe(n, manual=False):
    if not n:
        return {"ok": False, "error": "Device not found"}
    nid = n.get("id")
    with trust_lock:
        if nid not in trusted:
            return {"ok": False, "error": "Trust this device first"}

    kind = n.get("kind")
    if kind == "agent":
        online = bool(n.get("online"))
        conn_set(nid, "connected" if online else "offline",
                 "Spatial Node secure channel" if online else "Waiting for agent")
        return {"ok": online, "state": "connected" if online else "offline"}

    if kind == "lan":
        ip = n.get("ip")
        if not ip or not core.valid_private_ip(ip):
            conn_set(nid, "unavailable", "No private LAN IP")
            return {"ok": False, "error": "No private LAN IP"}
        ok, ms = core.ping_ms(ip)
        services = list(n.get("services") or [])
        detail = f"LAN reachable · {ms} ms" if ok else "No LAN response"
        if services:
            detail += f" · {len(services)} advertised service(s)"
        conn_set(nid, "ready" if ok else "offline", detail, n.get("capabilities", []))
        return {"ok": ok, "state": "ready" if ok else "offline", "ms": ms,
                "services": services, "capabilities": n.get("capabilities", [])}

    if kind == "wifi":
        if n.get("connected"):
            conn_set(nid, "connected", "Windows is associated with this AP")
            return {"ok": True, "state": "connected"}
        conn_set(nid, "nearby", "Observed AP; Windows credentials are required to join it")
        return {"ok": False, "state": "nearby", "error": "Wi-Fi credentials/pairing required"}

    if kind == "ble":
        if not manual:
            conn_set(nid, "nearby", "Trusted BLE device detected; explicit GATT inspection available")
            return {"ok": True, "state": "nearby"}
        result = asyncio.run(ble_inspect(n.get("address")))
        if result.get("ok"):
            conn_set(nid, "ready", f"GATT reachable · {result.get('service_count', 0)} service(s)", ["ble-gatt"])
        else:
            conn_set(nid, "pairing-required", result.get("error", "BLE connection failed"))
        return result

    return {"ok": False, "error": "No supported connection protocol"}

def auto_connector():
    time.sleep(3)
    while not core.stop.is_set():
        try:
            current = {n.get("id"): n for n in core.all_nodes()}
            with trust_lock:
                records = list(trusted.values())
            for rec in records:
                if not rec.get("auto_connect", True):
                    continue
                node = current.get(rec.get("id"))
                if node is None:
                    conn_set(rec.get("id"), "offline", "Trusted device is not currently detected")
                    continue
                safe_probe(node, manual=False)
        except Exception as e:
            log("auto connector " + str(e))
        if core.stop.wait(12):
            break

orig_root = core.app.view_functions["root"]
def root_v060():
    resp = orig_root()
    try:
        if getattr(resp, "status_code", 200) == 200:
            text = resp.get_data(as_text=True)
            text = text.replace("</body>", '<script src="/proximity.js"></script></body>')
            return Response(text, mimetype="text/html")
    except Exception:
        pass
    return resp
core.app.view_functions["root"] = root_v060

@core.app.get("/proximity.js")
def proximity_js():
    if not core.localonly():
        return Response("local only", 403)
    return Response((core.APPDIR / "proximity.js").read_text(encoding="utf-8"),
                    mimetype="application/javascript")

orig_nodepage = core.app.view_functions["nodepage"]
def nodepage_v060():
    resp = orig_nodepage()
    try:
        if getattr(resp, "status_code", 200) == 200:
            text = resp.get_data(as_text=True)
            extra = '<script>document.getElementById("join")?.addEventListener("click",()=>{try{localStorage.setItem("sh-auto","1")}catch(e){}});setTimeout(()=>{try{if(localStorage.getItem("sh-auto")==="1"&&!document.getElementById("join")?.disabled)document.getElementById("join")?.click()}catch(e){}},500);</script>'
            text = text.replace("</body>", extra + "</body>")
            return Response(text, mimetype="text/html")
    except Exception:
        pass
    return resp
core.app.view_functions["nodepage"] = nodepage_v060

@core.app.get("/api/trusted")
def api_trusted_v060():
    if not core.localonly():
        return Response("local only", 403)
    with trust_lock:
        return jsonify({"devices": list(trusted.values())})

@core.app.post("/api/trust")
def api_trust_v060():
    if not core.localonly():
        return Response("local only", 403)
    d = request.get_json(silent=True) or {}
    node_id = str(d.get("id") or "")
    wanted = bool(d.get("trusted", True))
    n = find_node(node_id)
    if wanted and not n:
        return jsonify({"error": "Device is not currently visible"}), 404
    with trust_lock:
        if wanted:
            rec = trust_record(n)
            rec["auto_connect"] = bool(d.get("auto_connect", True))
            trusted[node_id] = rec
        else:
            trusted.pop(node_id, None)
    if not wanted:
        with conn_lock:
            connections.pop(node_id, None)
    save_trusted()
    core.event("trust", f"{'Trusted' if wanted else 'Forgot'}: {(n or {}).get('name') or node_id}", node_id)
    result = safe_probe(n, manual=False) if wanted else {"ok": True}
    return jsonify({"ok": True, "trusted": wanted, "connection": result})

@core.app.post("/api/action/connect")
def api_connect_v060():
    if not core.localonly():
        return Response("local only", 403)
    d = request.get_json(silent=True) or {}
    n = find_node(str(d.get("id") or ""))
    if not n:
        return jsonify({"error": "Device not found"}), 404
    result = safe_probe(n, manual=True)
    return jsonify(result), (200 if result.get("ok") else 409)

orig_heartbeat = core.app.view_functions["heartbeat"]
def heartbeat_v060():
    d = request.get_json(silent=True) or {}
    resp = orig_heartbeat()
    try:
        status = resp[1] if isinstance(resp, tuple) else getattr(resp, "status_code", 200)
        if status == 200 and d.get("node_id"):
            nid = str(d["node_id"])
            n = find_node(nid)
            if n:
                with trust_lock:
                    if nid not in trusted:
                        trusted[nid] = trust_record(n)
                        save_trusted()
                        core.event("trust", f"Trusted after QR pairing: {n.get('name')}", nid)
                conn_set(nid, "connected", "Spatial Node heartbeat")
    except Exception as e:
        log(e)
    return resp
core.app.view_functions["heartbeat"] = heartbeat_v060

def main():
    threading.Thread(target=auto_connector, daemon=True).start()
    core.main()

if __name__ == "__main__":
    main()
