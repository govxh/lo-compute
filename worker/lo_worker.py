import os, sys, io, json, time, base64, subprocess, traceback, socket
import requests

TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL = os.environ["LO_CHANNEL_ID"]
WID     = os.environ.get("LO_WORKER_ID", "w1")
RUN_ID  = os.environ.get("LO_RUN_ID", "")
MAXMIN  = int(os.environ.get("LO_MAX_MINUTES", "340"))
API     = "https://discord.com/api/v10"
HDR     = {"Authorization": f"Bot {TOKEN}"}
START   = time.time()
LAST_ID = None

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
        "choco":  f"choco install {pkg} -y --no-progress",
        "pip":    f"{sys.executable} -m pip install {pkg}",
        "npm":    f"npm install -g {pkg}",
    }
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmds[mgr]],
                       capture_output=True, text=True, timeout=1800)
    return p.returncode == 0, p.stdout, p.stderr

def h_upload(a):
    path = a["path"]
    size = os.path.getsize(path)
    if size > 9_000_000:
        return False, "", f"file too big for bus ({size}B). Use release upload."
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

HANDLERS = {
    "SHELL": h_shell, "PYTHON": h_python, "WRITE_FILE": h_write_file,
    "READ_FILE": h_read_file, "LIST_DIR": h_list_dir,
    "SCREENSHOT": h_screenshot, "CLICK": h_click, "TYPE": h_type,
    "KEY": h_key, "MOVE": h_move, "OPEN_APP": h_open_app,
    "INSTALL": h_install, "UPLOAD": h_upload, "STATUS": h_status,
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
    # seed LAST_ID so purane commands dobara na chalein
    r = requests.get(f"{API}/channels/{CHANNEL}/messages",
                     headers=HDR, params={"limit": 1}, timeout=30)
    if r.status_code == 200 and r.json():
        globals()["LAST_ID"] = r.json()[0]["id"]

    post(f"<< ONLINE {json.dumps({'w':WID,'run_id':RUN_ID,'minutes_left':minutes_left(),'role':os.environ.get('LO_ROLE','general')})}")
    last_hb = time.time()

    while True:
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
