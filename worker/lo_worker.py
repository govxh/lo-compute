import os, sys, io, json, time, base64, subprocess, traceback, socket, zipfile
import requests

TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL = os.environ["LO_CHANNEL_ID"]
WID     = os.environ.get("LO_WORKER_ID", "w1")
RUN_ID  = os.environ.get("LO_RUN_ID", "")
MAXMIN  = int(os.environ.get("LO_MAX_MINUTES", "300"))
API     = "https://discord.com/api/v10"
HDR     = {"Authorization": f"Bot {TOKEN}"}
START   = time.time()
LAST_ID = None

# Storage Config
STORAGE_TOKEN = os.environ.get("STORAGE_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
STORAGE_OWNER = REPO.split("/")[0] if "/" in REPO else ""
STORAGE_REPO_NAME = "lo-storage"

def gh_api(method, path, **kwargs):
    url = f"https://api.github.com{path}"
    headers = {"Authorization": f"token {STORAGE_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    if "headers" in kwargs:
        headers.update(kwargs.pop("headers"))
    return requests.request(method, url, headers=headers, timeout=60, **kwargs)

def post(text):
    for i in range(0, max(len(text), 1), 1900):
        chunk = text[i:i+1900]
        r = requests.post(f"{API}/channels/{CHANNEL}/messages",
                          headers=HDR, json={"content": chunk}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.5)
            requests.post(f"{API}/channels/{CHANNEL}/messages",
                          headers=HDR, json={"content": chunk}, timeout=30)
        time.sleep(0.35)

def post_file(name, data, note=""):
    files = {"files[0]": (name, data)}
    payload = {"content": note[:1900]}
    r = requests.post(f"{API}/channels/{CHANNEL}/messages", headers=HDR,
                      data={"payload_json": json.dumps(payload)},
                      files=files, timeout=120)
    return r.status_code < 300

def reply(cid, ok, out="", err="", extra=None):
    body = {"id": cid, "w": WID, "ok": ok, "out": out[-3500:], "err": err[-1200:]}
    if extra: body.update(extra)
    post("<< " + json.dumps(body))

def minutes_left():
    return max(0, MAXMIN - int((time.time() - START) / 60))

def h_shell(a):
    timeout = a.get("timeout_sec") or a.get("timeout") or 600
    p = subprocess.run(["powershell", "-NoProfile", "-Command", a["cmd"]],
                       capture_output=True, text=True,
                       timeout=timeout, cwd=a.get("cwd") or None)
    return p.returncode == 0, p.stdout, p.stderr

def h_python(a):
    with open("_lo_tmp.py", "w", encoding="utf-8") as f:
        f.write(a["code"])
    timeout = a.get("timeout_sec") or a.get("timeout") or 900
    p = subprocess.run([sys.executable, "_lo_tmp.py"], capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode == 0, p.stdout, p.stderr

def h_write_file(a):
    data = base64.b64decode(a["content_b64"])
    os.makedirs(os.path.dirname(a["path"]) or ".", exist_ok=True)
    with open(a["path"], "wb") as f:
        f.write(data)
    return True, f"written {len(data)} bytes -> {a['path']}", ""

def h_read_file(a):
    n = a.get("max_bytes", 200000)
    with open(a["path"], "rb") as f:
        d = f.read(n)
    try:
        return True, d.decode("utf-8", "replace"), ""
    except Exception:
        return True, base64.b64encode(d).decode(), ""

def h_list_dir(a):
    out = []
    for e in os.scandir(a.get("path", ".")):
        out.append(("D " if e.is_dir() else "F ") + e.name + "  " +
                   (str(e.stat().st_size) if e.is_file() else ""))
    return True, "\n".join(out), ""

def h_screenshot(a):
    import pyautogui
    from PIL import Image
    img = pyautogui.screenshot()
    w, h = img.size
    target = a.get("width", 1280)
    if w > target:
        img = img.resize((target, int(h * target / w)), Image.LANCZOS)
    buf = io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
    post_file(f"shot_{a.get('_id','x')}.png", buf.read(),
              f"<< SHOT {a.get('_id','')} real={w}x{h} shown={img.size[0]}x{img.size[1]}")
    return True, f"screenshot sent real={w}x{h}", ""

def h_type(a):
    import pyautogui
    pyautogui.write(a["text"], interval=a.get("interval", 0.02))
    return True, "typed", ""

def h_key(a):
    import pyautogui
    combo = str(a.get("keys") or a.get("combo") or "").strip()
    if not combo:
        return False, "", "missing keys"
    parts = [p.strip().lower() for p in combo.replace("+", ",").split(",") if p.strip()]
    if len(parts) > 1:
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(parts[0])
    return True, f"pressed {combo}", ""

def h_scroll(a):
    """Scroll the active window. Positive amount = up, negative = down."""
    try:
        amt = int(a.get("amount", 0) or 0)
    except Exception:
        return False, "", "amount must be an integer"
    if amt == 0:
        return True, "nothing to scroll (amount=0)", ""
    try:
        import pyautogui
        pyautogui.scroll(amt)
        return True, f"scrolled {amt}", ""
    except Exception as e:
        return False, "", f"scroll failed: {e}"

def h_open_app(a):
    target = a.get("name") or a.get("target")
    if not target:
        return False, "", "missing app name"
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      f"Start-Process '{target}'"])
    time.sleep(a.get("wait", 6))
    return True, f"launched {target}", ""

def h_install(a):
    mgr = a.get("manager", "winget")
    pkgs = a.get("packages") or ([a["package"]] if a.get("package") else [])
    if isinstance(pkgs, str):
        pkgs = [pkgs]
    if not pkgs:
        return False, "", "missing packages"
    timeout = a.get("timeout_sec") or a.get("timeout") or 1800
    outs, errs, failed = [], [], []
    for pkg in pkgs:
        cmds = {
            "winget": f"winget install --id {pkg} --silent --accept-package-agreements --accept-source-agreements",
            "choco":  f"choco install {pkg} -y --no-progress",
            "pip":    f"{sys.executable} -m pip install {pkg}",
            "npm":    f"npm install -g {pkg}",
        }
        if mgr not in cmds:
            return False, "", f"unknown manager {mgr}"
        p = subprocess.run(["powershell", "-NoProfile", "-Command", cmds[mgr]],
                           capture_output=True, text=True, timeout=timeout)
        outs.append(f"== {pkg} ==\n" + p.stdout)
        if p.stderr:
            errs.append(f"== {pkg} ==\n" + p.stderr)
        if p.returncode != 0:
            failed.append(pkg)
    ok = not failed
    return ok, "\n".join(outs), ("failed: " + ", ".join(failed) + "\n" if failed else "") + "\n".join(errs)

def h_upload(a):
    path = a["path"]
    size = os.path.getsize(path)
    if size > 9_000_000:
        dest = os.path.basename(path)
        ok, out, err = h_gh_upload({"path": path, "dest": dest, "folder": "artifacts"})
        if ok:
            return True, f"uploaded large file to storage: {dest}", ""
        return False, "", f"large file upload failed: {err}"
    
    with open(path, "rb") as f:
        post_file(os.path.basename(path), f.read(),
                  f"<< FILE {a.get('_id','')} {os.path.basename(path)} {size}B")
    return True, f"uploaded {size} bytes", ""

def h_status(a):
    import psutil
    return True, json.dumps({
        "worker": WID, "run_id": RUN_ID, "minutes_left": minutes_left(),
        "cpu": psutil.cpu_percent(interval=0.5),
        "ram_pct": psutil.virtual_memory().percent,
        "disk_free_gb": round(psutil.disk_usage("C:\\").free / 1e9, 1),
        "host": socket.gethostname(), "cwd": os.getcwd(),
    }), ""

# --- PHASE 2 HANDLERS ---

def h_gh_upload(a):
    path = a["path"]
    dest = a.get("dest", os.path.basename(path))
    folder = a.get("folder", "artifacts")
    size = os.path.getsize(path)
    
    if size < 25_000_000:
        with open(path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode()
        sha = None
        r = gh_api("GET", f"/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/contents/{folder}/{dest}")
        if r.status_code == 200:
            sha = r.json().get("sha")
        body = {"message": f"upload {dest}", "content": content_b64}
        if sha: body["sha"] = sha
        r = gh_api("PUT", f"/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/contents/{folder}/{dest}", json=body)
        if r.status_code in (200, 201):
            return True, f"uploaded to {folder}/{dest} ({size}B)", ""
        return False, "", f"Contents API failed: {r.status_code} {r.text}"
    else:
        rel_id = None
        r = gh_api("GET", f"/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/releases/tags/storage")
        if r.status_code == 200:
            rel_id = r.json()["id"]
        else:
            r = gh_api("POST", f"/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/releases",
                       json={"tag_name": "storage", "name": "storage", "body": "lo-storage"})
            if r.status_code == 201:
                rel_id = r.json()["id"]
            else:
                return False, "", f"Release create failed: {r.text}"
        upload_url = f"https://uploads.github.com/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/releases/{rel_id}/assets?name={dest}"
        with open(path, "rb") as f:
            r = requests.post(upload_url,
                              headers={"Authorization": f"token {STORAGE_TOKEN}", "Content-Type": "application/octet-stream"},
                              data=f, timeout=300)
        if r.status_code == 201:
            return True, f"uploaded release asset {dest} ({size}B)", ""
        return False, "", f"Release upload failed: {r.status_code} {r.text}"

def h_gh_download(a):
    path = a["path"]
    save_as = a.get("save_as", os.path.basename(path))
    r = gh_api("GET", f"/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/contents/{path}")
    if r.status_code == 200:
        data = r.json()
        if data.get("encoding") == "base64":
            content = base64.b64decode(data["content"])
        else:
            r2 = requests.get(data["download_url"], headers={"Authorization": f"token {STORAGE_TOKEN}"})
            content = r2.content
        with open(save_as, "wb") as f:
            f.write(content)
        return True, f"downloaded {len(content)} bytes -> {save_as}", ""
    
    r = gh_api("GET", f"/repos/{STORAGE_OWNER}/{STORAGE_REPO_NAME}/releases/tags/storage")
    if r.status_code == 200:
        assets = r.json().get("assets", [])
        for asset in assets:
            if asset["name"] == path or asset["name"] == os.path.basename(path):
                r2 = requests.get(asset["url"],
                                  headers={"Authorization": f"token {STORAGE_TOKEN}", "Accept": "application/octet-stream"})
                if r2.status_code == 200:
                    with open(save_as, "wb") as f:
                        f.write(r2.content)
                    return True, f"downloaded asset {len(r2.content)} bytes -> {save_as}", ""
    return False, "", f"file not found in storage: {path}"

def h_checkpoint(a):
    folder = a.get("folder", "work")
    if not os.path.exists(folder): folder = "."
    zip_name = f"checkpoints/{WID}_{int(time.time())}.zip"
    local_zip = f"_lo_ckpt_{WID}.zip"
    with zipfile.ZipFile(local_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(folder):
            for file in files:
                fp = os.path.join(root, file)
                arcname = os.path.relpath(fp, folder)
                zf.write(fp, arcname)
    res_ok, res_out, res_err = h_gh_upload({"path": local_zip, "dest": os.path.basename(zip_name), "folder": "checkpoints"})
    os.remove(local_zip)
    if res_ok:
        return True, f"checkpoint saved to {zip_name}", ""
    return False, "", f"checkpoint upload failed: {res_err}"

def h_restore(a):
    path = a["path"]
    save_as = "_lo_restore.zip"
    ok, out, err = h_gh_download({"path": path, "save_as": save_as})
    if not ok: return False, "", err
    folder = a.get("folder", "work")
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(save_as, "r") as zf:
        zf.extractall(folder)
    os.remove(save_as)
    return True, f"restored to {folder}", ""

def h_download_url(a):
    url = a["url"]
    save_as = a.get("save_as", os.path.basename(url) or "downloaded_file")
    r = requests.get(url, timeout=300)
    if r.status_code == 200:
        with open(save_as, "wb") as f:
            f.write(r.content)
        return True, f"downloaded {len(r.content)} bytes -> {save_as}", ""
    return False, "", f"download failed: {r.status_code}"

def h_zip(a):
    src = a["src"]
    dest = a.get("dest", f"{src}.zip")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(src):
            for root, dirs, files in os.walk(src):
                for file in files:
                    fp = os.path.join(root, file)
                    arcname = os.path.relpath(fp, os.path.dirname(src))
                    zf.write(fp, arcname)
        else:
            zf.write(src, os.path.basename(src))
    return True, f"zipped to {dest}", ""

def h_unzip(a):
    src = a["src"]
    dest = a.get("dest", ".")
    with zipfile.ZipFile(src, "r") as zf:
        zf.extractall(dest)
    return True, f"unzipped to {dest}", ""

def h_git(a):
    cmd = a["cmd"]
    p = subprocess.run(["git"] + cmd.split(), capture_output=True, text=True, timeout=1800)
    return p.returncode == 0, p.stdout, p.stderr

HANDLERS = {
    "SHELL": h_shell, "PYTHON": h_python, "WRITE_FILE": h_write_file,
    "READ_FILE": h_read_file, "LIST_DIR": h_list_dir,
    "SCREENSHOT": h_screenshot, "TYPE": h_type,
    "KEY": h_key, "SCROLL": h_scroll, "OPEN_APP": h_open_app,
    "INSTALL": h_install, "UPLOAD": h_upload, "STATUS": h_status,
    "DOWNLOAD_URL": h_download_url, "ZIP": h_zip, "UNZIP": h_unzip,
    "GIT": h_git, "CHECKPOINT": h_checkpoint, "RESTORE": h_restore,
    "GH_UPLOAD": h_gh_upload, "GH_DOWNLOAD": h_gh_download,
}

def _sysinfo():
    """Best-effort cpu/ram strings for the ONLINE announcement."""
    try:
        import psutil
        cpu = psutil.cpu_count(logical=True) or 0
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        return f"{cpu} cores", f"{ram_gb} GB"
    except Exception:
        return "?", "?"

def fetch_new():
    global LAST_ID
    params = {"limit": 25}
    if LAST_ID: params["after"] = LAST_ID
    r = requests.get(f"{API}/channels/{CHANNEL}/messages",
                     headers=HDR, params=params, timeout=30)
    if r.status_code != 200:
        return []
    msgs = list(reversed(r.json()))
    if msgs: LAST_ID = msgs[-1]["id"]
    return [m for m in msgs if (m.get("content") or "").startswith(">>")]

def main():
    r = requests.get(f"{API}/channels/{CHANNEL}/messages",
                     headers=HDR, params={"limit": 1}, timeout=30)
    if r.status_code == 200 and r.json():
        globals()["LAST_ID"] = r.json()[0]["id"]
    _cpu, _ram = _sysinfo()
    post(f"<< ONLINE {json.dumps({'w':WID,'run_id':RUN_ID,'minutes_left':minutes_left(),'role':os.environ.get('LO_ROLE','general'),'cpu':_cpu,'ram':_ram})}")
    last_hb = time.time()
    while True:
        if minutes_left() <= 3:
            post(f"<< EXPIRING {json.dumps({'w':WID})}")
            return
            
        if minutes_left() <= 30 and not getattr(main, "ckpt_done", False):
            post(f"<< AUTO_CHECKPOINT {json.dumps({'w':WID})}")
            try:
                ok, out, err = h_checkpoint({})
                if ok:
                    main.ckpt_done = True
            except Exception:
                pass

        try:
            for m in fetch_new():
                raw = m["content"][2:].strip()
                try:
                    c = json.loads(raw)
                except Exception:
                    continue
                if c.get("w") not in (None, WID, "all"):
                    continue
                cid = c.get("id", "?")
                cmd = (c.get("cmd") or "").upper()
                if cmd == "STOP":
                    reply(cid, True, "stopping"); return
                if cmd == "PING":
                    reply(cid, True, "pong"); continue
                fn = HANDLERS.get(cmd)
                if not fn:
                    reply(cid, False, "", f"unknown cmd {cmd}"); continue
                args = c.get("args", {}) or {}
                args["_id"] = cid
                t0 = time.time()
                try:
                    ok, out, err = fn(args)
                except Exception:
                    ok, out, err = False, "", traceback.format_exc()
                reply(cid, ok, out, err,
                      {"ms": int((time.time() - t0) * 1000),
                       "min_left": minutes_left()})
            if time.time() - last_hb > 120:
                post(f"<< HB {json.dumps({'w':WID,'min_left':minutes_left()})}")
                last_hb = time.time()
        except Exception:
            traceback.print_exc()
        time.sleep(2)

if __name__ == "__main__":
    main()
