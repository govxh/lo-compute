import os, sys, io, json, time, base64, shutil, zipfile, subprocess, traceback, socket
from urllib.parse import quote
import requests

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL = os.environ["LO_CHANNEL_ID"]
WID = os.environ.get("LO_WORKER_ID", "w1")
RUN_ID = os.environ.get("LO_RUN_ID", "")
MAXMIN = int(os.environ.get("LO_MAX_MINUTES", "340"))
API = "https://discord.com/api/v10"
HDR = {"Authorization": f"Bot {TOKEN}"}
START = time.time()
LAST_ID = None

# v2: private storage (lo-storage) -- artifacts yahan jate hain, kabhi public repo par nahi
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "")
GH_OWNER = REPO_SLUG.split("/")[0] if "/" in REPO_SLUG else ""
STORAGE_TOKEN = os.environ.get("LO_STORAGE_TOKEN", "")
STORAGE_REPO = os.environ.get("LO_STORAGE_REPO", "lo-storage")
CKPT_DONE = False

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

# ---------------- command handlers ----------------
def h_shell(a):
    p = subprocess.run(["powershell", "-NoProfile", "-Command", a["cmd"]],
                       capture_output=True, text=True,
                       timeout=a.get("timeout", 600), cwd=a.get("cwd") or None)
    return p.returncode == 0, p.stdout, p.stderr

def h_python(a):
    with open("_lo_tmp.py", "w", encoding="utf-8") as f:
        f.write(a["code"])
    p = subprocess.run([sys.executable, "_lo_tmp.py"], capture_output=True,
                       text=True, timeout=a.get("timeout", 900))
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
        out.append(("D " if e.is_dir() else "F ") + e.name + " " +
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

def h_click(a):
    import pyautogui
    pyautogui.moveTo(a["x"], a["y"], duration=0.15)
    pyautogui.click(clicks=2 if a.get("double") else 1,
                    button=a.get("button", "left"))
    return True, f"clicked {a['x']},{a['y']}", ""

def h_type(a):
    import pyautogui
    pyautogui.write(a["text"], interval=a.get("interval", 0.02))
    return True, "typed", ""

def h_key(a):
    import pyautogui
    keys = [k.strip() for k in a["combo"].split("+")]
    pyautogui.hotkey(*keys)
    return True, f"key {a['combo']}", ""

def h_move(a):
    import pyautogui
    pyautogui.moveTo(a["x"], a["y"], duration=0.15)
    return True, "moved", ""

def h_open_app(a):
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      f"Start-Process '{a['target']}'"])
    time.sleep(a.get("wait", 6))
    return True, f"launched {a['target']}", ""

def h_install(a):
    mgr, pkg = a.get("manager", "winget"), a["package"]
    cmds = {
        "winget": f"winget install --id {pkg} --silent --accept-package-agreements --accept-source-agreements",
        "choco": f"choco install {pkg} -y --no-progress",
        "pip": f"{sys.executable} -m pip install {pkg}",
        "npm": f"npm install -g {pkg}",
    }
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmds[mgr]],
                       capture_output=True, text=True, timeout=1800)
    return p.returncode == 0, p.stdout, p.stderr

def h_upload(a):
    path = a["path"]
    size = os.path.getsize(path)
    if size > 9_000_000:
        return False, "", f"file too big for bus ({size}B). Use GH_UPLOAD."
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
        "storage": bool(STORAGE_TOKEN and GH_OWNER),
    }), ""

# ---------------- v2: private-repo storage (lo-storage) ----------------
def _gh(method, url, token, json_body=None, data=None, extra=None):
    h = {"Authorization": "token " + token, "User-Agent": "lo-worker"}
    if extra: h.update(extra)
    return requests.request(method, url, headers=h, json=json_body, data=data, timeout=180)

def gh_store_bytes(path, data, msg="lo artifact"):
    if not (STORAGE_TOKEN and GH_OWNER):
        return None, "storage token/owner not configured"
    if len(data) < 25 * 1024 * 1024:
        url = "https://api.github.com/repos/%s/%s/contents/%s" % (GH_OWNER, STORAGE_REPO, quote(path))
        r = _gh("GET", url, STORAGE_TOKEN)
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = {"message": msg, "content": base64.b64encode(data).decode()}
        if sha: body["sha"] = sha
        r = _gh("PUT", url, STORAGE_TOKEN, json_body=body)
        if r.status_code not in (200, 201):
            return None, "gh put %s: %s" % (r.status_code, r.text[:300])
        return "content:" + path, ""
    rel = _gh("GET", "https://api.github.com/repos/%s/%s/releases/tags/storage" % (GH_OWNER, STORAGE_REPO), STORAGE_TOKEN)
    if rel.status_code != 200:
        rel = _gh("POST", "https://api.github.com/repos/%s/%s/releases" % (GH_OWNER, STORAGE_REPO), STORAGE_TOKEN,
                  json_body={"tag_name": "storage", "name": "storage"})
    if rel.status_code not in (200, 201):
        return None, "release %s" % rel.status_code
    aid = rel.json()["id"]
    aname = quote(path.replace("/", "_"))
    url = "https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s" % (GH_OWNER, STORAGE_REPO, aid, aname)
    r = _gh("POST", url, STORAGE_TOKEN, data=data, extra={"Content-Type": "application/octet-stream"})
    if r.status_code not in (200, 201):
        return None, "asset %s: %s" % (r.status_code, r.text[:300])
    return "release:" + path, ""

def gh_load_bytes(path):
    if path.startswith("release:"):
        real = path[8:]
        rel = _gh("GET", "https://api.github.com/repos/%s/%s/releases/tags/storage" % (GH_OWNER, STORAGE_REPO), STORAGE_TOKEN)
        aid = rel.json()["id"]
        assets = _gh("GET", "https://api.github.com/repos/%s/%s/releases/%s/assets" % (GH_OWNER, STORAGE_REPO, aid), STORAGE_TOKEN).json()
        target = real.replace("/", "_")
        for a in assets:
            if a["name"] == target:
                r = requests.get(a["url"], headers={"Authorization": "token " + STORAGE_TOKEN,
                                                    "Accept": "application/octet-stream"}, timeout=300)
                r.raise_for_status()
                return r.content
        raise RuntimeError("asset not found: " + real)
    real = path[8:] if path.startswith("content:") else path
    url = "https://api.github.com/repos/%s/%s/contents/%s" % (GH_OWNER, STORAGE_REPO, quote(real))
    r = _gh("GET", url, STORAGE_TOKEN)
    if r.status_code != 200:
        raise RuntimeError("gh get %s" % r.status_code)
    return base64.b64decode(r.json()["content"])

def h_gh_upload(a):
    with open(a["path"], "rb") as f:
        data = f.read()
    dest = a.get("dest") or os.path.basename(a["path"])
    stored, err = gh_store_bytes("artifacts/%s/%s/%s" % (WID, RUN_ID, dest), data)
    return (stored is not None), (stored or ""), err

def h_gh_download(a):
    data = gh_load_bytes(a["path"])
    out = a.get("save_as") or a["path"].replace(":", "/").split("/")[-1]
    with open(out, "wb") as f:
        f.write(data)
    return True, "downloaded %d bytes -> %s" % (len(data), out), ""

def _zip_work():
    if os.path.isdir("work"):
        return "work", shutil.make_archive("_lo_ckpt", "zip", root_dir=os.getcwd(), base_dir="work")
    return ".", shutil.make_archive("_lo_ckpt", "zip", root_dir=os.getcwd())

def h_checkpoint(a):
    root, zpath = _zip_work()
    with open(zpath, "rb") as f:
        data = f.read()
    stored, err = gh_store_bytes("checkpoints/%s/%s.zip" % (WID, RUN_ID), data, msg="checkpoint " + WID)
    ok = stored is not None
    return ok, ("checkpoint %s (%d bytes, root=%s)" % (stored, len(data), root) if ok else ""), err

def h_restore(a):
    data = gh_load_bytes(a["path"])
    with open("_lo_restore.zip", "wb") as f:
        f.write(data)
    with zipfile.ZipFile("_lo_restore.zip") as z:
        z.extractall(os.getcwd())
    return True, "restored %d bytes" % len(data), ""

HANDLERS = {
    "SHELL": h_shell, "PYTHON": h_python, "WRITE_FILE": h_write_file,
    "READ_FILE": h_read_file, "LIST_DIR": h_list_dir,
    "SCREENSHOT": h_screenshot, "CLICK": h_click, "TYPE": h_type,
    "KEY": h_key, "MOVE": h_move, "OPEN_APP": h_open_app,
    "INSTALL": h_install, "UPLOAD": h_upload, "STATUS": h_status,
    "GH_UPLOAD": h_gh_upload, "GH_DOWNLOAD": h_gh_download,
    "CHECKPOINT": h_checkpoint, "RESTORE": h_restore,
}

# ---------------- main loop ----------------
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
    global CKPT_DONE
    r = requests.get(f"{API}/channels/{CHANNEL}/messages",
                     headers=HDR, params={"limit": 1}, timeout=30)
    if r.status_code == 200 and r.json():
        globals()["LAST_ID"] = r.json()[0]["id"]
    post(f"<< ONLINE {json.dumps({'w':WID,'run_id':RUN_ID,'minutes_left':minutes_left(),'role':os.environ.get('LO_ROLE','general'),'storage':bool(STORAGE_TOKEN and GH_OWNER)})}")
    last_hb = time.time()
    while True:
        if minutes_left() <= 30 and not CKPT_DONE and STORAGE_TOKEN and GH_OWNER:
            CKPT_DONE = True
            post("<< CHECKPOINTING " + json.dumps({"w": WID, "min_left": minutes_left()}))
            ok, out, err = h_checkpoint({})
            post("<< CHECKPOINT " + ("ok " if ok else "FAIL ") +
                 json.dumps({"w": WID, "out": out[-300:], "err": err[-200:]}))
        if minutes_left() <= 3:
            post(f"<< EXPIRING {json.dumps({'w':WID})}")
            return
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
