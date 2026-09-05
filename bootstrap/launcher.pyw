# Spatial Hub bootstrap launcher for Windows
# This file is intentionally small and stable. It updates the application payload
# from the public GitHub repository and then starts the current local version.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_RAW = "https://raw.githubusercontent.com/Godar222/spatial-hub/main"
MANIFEST_URL = REPO_RAW + "/update/manifest.json"
LAUNCHER_URL = REPO_RAW + "/bootstrap/launcher.pyw"

ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpatialHub"
APP_DIR = ROOT / "app"
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
VERSION_FILE = APP_DIR / "version.json"
LOG_FILE = LOG_DIR / "launcher.log"

for p in (ROOT, APP_DIR, LOG_DIR, DATA_DIR):
    p.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url + ("&" if "?" in url else "?") + f"_={int(time.time())}",
        headers={"User-Agent": "SpatialHub-Updater/1.0", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_local_version() -> str:
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8")).get("version", "0")
    except Exception:
        return "0"


def install_requirements(requirements_path: Path, wanted_hash: str) -> None:
    marker = ROOT / "requirements.sha256"
    current = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    if current == wanted_hash:
        return
    log("Installing/updating Python dependencies.")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "-r", str(requirements_path)]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    log(p.stdout[-12000:])
    if p.returncode != 0:
        raise RuntimeError("Dependency installation failed. See launcher.log")
    marker.write_text(wanted_hash, encoding="utf-8")


def update_self(manifest: dict) -> None:
    wanted = manifest.get("launcher_sha256")
    if not wanted:
        return
    try:
        current = sha256(Path(__file__).read_bytes())
        if current == wanted:
            return
        data = fetch(LAUNCHER_URL)
        if sha256(data) != wanted:
            raise RuntimeError("Launcher checksum mismatch")
        tmp = Path(__file__).with_suffix(".new.pyw")
        tmp.write_bytes(data)
        os.replace(tmp, Path(__file__))
        log("Bootstrap launcher updated. New launcher will be used on next run.")
    except Exception as e:
        log(f"Launcher self-update skipped: {e}")


def update_app(manifest: dict) -> bool:
    target_version = str(manifest["version"])
    changed = read_local_version() != target_version
    for item in manifest.get("files", []):
        rel = Path(item["path"])
        url = item.get("url") or (REPO_RAW + "/payload/" + rel.as_posix())
        wanted = item["sha256"].lower()
        dest = APP_DIR / rel
        valid = False
        if dest.exists():
            try:
                valid = sha256(dest.read_bytes()) == wanted
            except Exception:
                valid = False
        if valid:
            continue
        log(f"Downloading {rel.as_posix()}")
        data = fetch(url)
        got = sha256(data)
        if got != wanted:
            raise RuntimeError(f"Checksum mismatch for {rel}: {got} != {wanted}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".download")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        changed = True
    req_hash = str(manifest.get("requirements_sha256", ""))
    req_path = APP_DIR / "requirements.txt"
    if req_hash and req_path.exists():
        install_requirements(req_path, req_hash)
    VERSION_FILE.write_text(json.dumps({"version": target_version, "installed_at": time.time(), "manifest_url": MANIFEST_URL}, indent=2), encoding="utf-8")
    return changed


def start_app() -> None:
    main = APP_DIR / "main.py"
    if not main.exists():
        raise RuntimeError("Spatial Hub application payload is not installed.")
    env = os.environ.copy()
    env["SPATIAL_HUB_ROOT"] = str(ROOT)
    env["SPATIAL_HUB_LAUNCHER"] = str(Path(__file__).resolve())
    env["SPATIAL_HUB_MANIFEST"] = MANIFEST_URL
    pythonw = Path(sys.executable)
    if pythonw.name.lower() == "python.exe":
        candidate = pythonw.with_name("pythonw.exe")
        if candidate.exists():
            pythonw = candidate
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(pythonw), str(main)], cwd=str(APP_DIR), env=env, creationflags=creationflags, close_fds=True)


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--update-only", action="store_true")
    ap.add_argument("--update-and-start", action="store_true")
    ap.add_argument("--no-update", action="store_true")
    args, _ = ap.parse_known_args()
    if not args.no_update:
        try:
            raw = fetch(MANIFEST_URL)
            manifest = json.loads(raw.decode("utf-8"))
            log(f"Remote version: {manifest.get('version')}; local: {read_local_version()}")
            update_app(manifest)
            update_self(manifest)
        except Exception as e:
            log(f"Update check failed: {e}")
            if not (APP_DIR / "main.py").exists():
                raise
    if args.update_only:
        return 0
    start_app()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL: {exc}")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk(); root.withdraw()
            messagebox.showerror("Spatial Hub", f"Spatial Hub could not start.\n\n{exc}\n\nLog:\n{LOG_FILE}")
            root.destroy()
        except Exception:
            pass
        raise
