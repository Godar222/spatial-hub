from __future__ import annotations
import argparse,json,os,subprocess,sys,time,urllib.request
from pathlib import Path

RAW='https://raw.githubusercontent.com/Godar222/spatial-hub/main'
MANIFEST=RAW+'/update/manifest.json'
ROOT=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'SpatialHub'
APP=ROOT/'app'; LOGS=ROOT/'logs'; DATA=ROOT/'data'; VERSION=APP/'version.json'; LOG=LOGS/'launcher.log'
for p in (ROOT,APP,LOGS,DATA):p.mkdir(parents=True,exist_ok=True)

def log(s):
    try:
        with LOG.open('a',encoding='utf-8') as f:f.write(f"[{time.strftime('%F %T')}] {s}\n")
    except:pass

def fetch(url):
    req=urllib.request.Request(url+('?' if '?' not in url else '&')+'t='+str(int(time.time())),headers={'User-Agent':'SpatialHub-Updater','Cache-Control':'no-cache'})
    return urllib.request.urlopen(req,timeout=25).read()

def local_version():
    try:return json.loads(VERSION.read_text(encoding='utf-8')).get('version','0')
    except:return '0'

def deps(path,marker):
    mark=ROOT/'requirements.version'
    if mark.exists() and mark.read_text(encoding='utf-8').strip()==marker:return
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    p=subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--upgrade','-r',str(path)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=flags)
    log(p.stdout[-12000:])
    if p.returncode:raise RuntimeError('Dependency installation failed; see launcher.log')
    mark.write_text(marker,encoding='utf-8')

def update():
    m=json.loads(fetch(MANIFEST).decode('utf-8'));remote=str(m['version']);local=local_version();log(f'remote={remote} local={local}')
    if remote!=local or not (APP/'main.py').exists():
        for item in m.get('files',[]):
            rel=Path(item['path']);url=item.get('url') or RAW+'/payload/'+rel.as_posix();dest=APP/rel;dest.parent.mkdir(parents=True,exist_ok=True);tmp=dest.with_suffix(dest.suffix+'.new');tmp.write_bytes(fetch(url));os.replace(tmp,dest);log('updated '+rel.as_posix())
        VERSION.write_text(json.dumps({'version':remote,'installed_at':time.time()},indent=2),encoding='utf-8')
    req=APP/'requirements.txt'
    if req.exists():deps(req,str(m.get('requirements_version',remote)))
    return remote

def start():
    main=APP/'main.py'
    if not main.exists():raise RuntimeError('Application payload missing')
    env=os.environ.copy();env['SPATIAL_HUB_ROOT']=str(ROOT);env['SPATIAL_HUB_LAUNCHER']=str(Path(__file__).resolve());env['SPATIAL_HUB_MANIFEST']=MANIFEST
    py=Path(sys.executable);cand=py.with_name('pythonw.exe');py=cand if cand.exists() else py
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'DETACHED_PROCESS',0)
    subprocess.Popen([str(py),str(main)],cwd=str(APP),env=env,creationflags=flags,close_fds=True)

def main():
    ap=argparse.ArgumentParser(add_help=False);ap.add_argument('--update-only',action='store_true');ap.add_argument('--no-update',action='store_true');args,_=ap.parse_known_args()
    if not args.no_update:
        try:update()
        except Exception as e:
            log('update failed: '+str(e))
            if not (APP/'main.py').exists():raise
    if not args.update_only:start()

if __name__=='__main__':
    try:main()
    except Exception as e:
        log('FATAL '+str(e))
        try:
            import tkinter as tk
            from tkinter import messagebox
            r=tk.Tk();r.withdraw();messagebox.showerror('Spatial Hub',f'Не удалось запустить Spatial Hub.\n\n{e}\n\nЛог: {LOG}');r.destroy()
        except:pass
