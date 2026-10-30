#!/usr/bin/env python3
"""
Steam Download Tracker
- Scans drives for Steam libraries (folders named 'steamapps')
- Reads appmanifest_*.acf files and displays downloads in a GUI with thumbnails
- Supports searching by name/app ID and filtering active downloads
- Can schedule a shutdown after downloads finish

Requirements: Python 3.8+ and pip install pillow psutil websocket-client
(Pillow = thumbnails, psutil = network activity, websocket-client = live Steam data;
all optional)

Live data: Steam must be started with CEF debugging enabled (create an empty
'.cef-enable-remote-debugging' file in the Steam folder, or use the
-cef-enable-debugging launch option). The default port is 8080 and can be changed
with the STEAM_CDP_PORT environment variable.
"""
import os
import re
import json
import sys
import time
import string
import queue
import threading
import subprocess
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

JS_HOOK = """(function(){
  function plain(o){
    if (!o || typeof o !== 'object') return o;
    var r = {}, seen = {}, p = o;
    while (p && p !== Object.prototype && p !== Array.prototype) {
      Object.getOwnPropertyNames(p).forEach(function(k){
        if (seen[k] || k === 'constructor') return;
        seen[k] = 1;
        try { var v = o[k]; if (typeof v !== 'function') r[k] = v; } catch (e) {}
      });
      p = Object.getPrototypeOf(p);
    }
    return r;
  }
  if (!window.__dlTrk3) {
    var st = window.__dlTrk3 = {items: [], overview: null, d: null, ni: 0, no: 0, err: null};
    SteamClient.Downloads.RegisterForDownloadItems(function(d, items){
      st.ni++; st.d = d;
      try { st.items = Array.prototype.slice.call(items || []).map(plain); } catch (e) { st.err = String(e); }
    });
    SteamClient.Downloads.RegisterForDownloadOverview(function(o){ st.no++; st.overview = plain(o); });
  }
  return JSON.stringify(window.__dlTrk3);
})()"""

POLL_MS = 1000          # refresh interval
IDLE_GRACE = 30         # seconds without active downloads before starting the timer
THUMB = (150, 70)
CACHE = Path.home() / ".steam_download_tracker"
CACHE.mkdir(exist_ok=True)

KV = re.compile(r'"(\w+)"\s+"([^"]*)"')
SKIP = {"windows", "program files", "program files (x86)", "programdata", "users",
        "$recycle.bin", "system volume information", "appdata", "node_modules"}
THUMB_URLS = [
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{}/header.jpg",
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{}/header.jpg",
]


# ----------------------------------------------------------------- utilities
def fb(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def ft(s):
    h, r = divmod(int(s), 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# --------------------------------------------------------- library discovery
def default_steam_dirs():
    c = []
    if sys.platform == "win32":
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            p = os.environ.get(env)
            if p:
                c.append(Path(p) / "Steam")
    else:
        h = Path.home()
        c += [h / ".local/share/Steam", h / ".steam/steam",
              h / "Library/Application Support/Steam",
              h / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    return [p for p in c if (p / "steamapps").is_dir()]


def libs_from_vdf(steam):
    out = []
    try:
        txt = (steam / "steamapps" / "libraryfolders.vdf").read_text(errors="ignore")
    except OSError:
        return out
    for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
        out.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps")
    return out


def subdirs(p):
    try:
        return [Path(e.path) for e in os.scandir(p) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def drive_roots():
    if sys.platform == "win32":
        return [Path(f"{l}:\\") for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")]
    roots = subdirs("/mnt") + subdirs("/media")
    for u in subdirs("/media") + subdirs("/run/media"):
        roots += subdirs(u)
    return roots


def scan_drives():
    found = set()

    def walk(root, depth):
        if (root / "steamapps").is_dir():
            found.add(root / "steamapps")
        if depth == 0:
            return
        for d in subdirs(root):
            if d.name.lower() in SKIP or d.name.startswith("."):
                continue
            walk(d, depth - 1)

    for r in drive_roots():
        walk(r, 2)
    for s in default_steam_dirs():
        found.add(s / "steamapps")
        found.update(libs_from_vdf(s))
    return {p for p in found if p.is_dir()}


# ------------------------------------------------------------------ manifests
def parse_manifest(path):
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    d = {}
    for k, v in KV.findall(txt):
        d.setdefault(k, v)
    try:
        appid = int(d["appid"])
    except (KeyError, ValueError):
        return None

    def i(k):
        try:
            return int(d.get(k, 0) or 0)
        except ValueError:
            return 0

    a = dict(appid=appid, name=d.get("name", f"App {appid}"), lib=str(path.parent), installdir=d.get("installdir", ""),
             dl=i("BytesDownloaded"), dl_t=i("BytesToDownload"),
             st=i("BytesStaged"), st_t=i("BytesToStage"),
             flags=i("StateFlags"), size=i("SizeOnDisk"))
    a["active"] = (a["dl_t"] > 0 and a["dl"] < a["dl_t"]) or \
                  (a["st_t"] > 0 and a["st"] < a["st_t"])
    return a


def probe_dirs(lib, appid, installdir):
    """Return staged bytes, game-folder bytes, and the most recent modification time."""
    lib = Path(lib)
    newest = 0.0

    def walk(base):
        nonlocal newest
        total = 0
        for root, _, files in os.walk(base):
            for n in files:
                try:
                    st = os.stat(os.path.join(root, n))
                except OSError:
                    continue
                total += st.st_size
                newest = max(newest, st.st_mtime)
        return total

    stage = walk(lib / "downloading" / str(appid)) + walk(lib / "temp" / str(appid))
    try:
        for f in (lib / "downloading").glob(f"state_{appid}_*.patch"):
            newest = max(newest, f.stat().st_mtime)
    except OSError:
        pass
    game = walk(lib / "common" / installdir) if installdir else 0
    return stage, game, newest


# ----------------------------------------------- Steam CEF debugging controls
MARKER = ".cef-enable-remote-debugging"
NOWIN = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def flatten_items(raw):
    """Flatten items like [{remote_client_id, item_data:[{appid,...}]}] or flat app ID entries."""
    if isinstance(raw, dict):
        raw = list(raw.values())
    out = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("item_data"), list):
            out += [x for x in e["item_data"] if isinstance(x, dict)]
        else:
            out.append(e)
    return out


def ov_progress(ov):
    """Return downloaded and written byte progress from the overview.
    progress[2] is data download progress; progress[3] is file update progress."""
    pr = ov.get("progress") or []

    def g(i):
        e = pr[i] if i < len(pr) and isinstance(pr[i], dict) else {}
        return _int(e.get("bytes_in_progress")), _int(e.get("bytes_total"))

    return g(2), g(3)


def steam_root():
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return Path(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
    d = default_steam_dirs()
    return d[0] if d else None


def steam_cmd(root):
    """Return the base command used to launch Steam (except on macOS)."""
    if sys.platform == "win32":
        exe = root / "steam.exe" if root else None
        return [str(exe)] if exe and exe.exists() else ["steam.exe"]
    if root and ".var/app/com.valvesoftware.Steam" in str(root).replace("\\", "/"):
        return ["flatpak", "run", "com.valvesoftware.Steam"]
    return ["steam"]


def steam_running():
    """Return True or False, or None if the status cannot be determined."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True,
                                 text=True, timeout=10, creationflags=NOWIN).stdout
            return "steam.exe" in out.lower()
        name = "steam_osx" if sys.platform == "darwin" else "steam"
        return subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return None


def enable_steam_debug(log):
    """Create the marker, gracefully close Steam, and relaunch it with CEF debugging."""
    root = steam_root()
    if root is None:
        log("Steam installation folder not found")
        return False
    try:
        (root / MARKER).touch()
        log(f"Created {MARKER}")
    except OSError as e:
        log(f"Could not create marker ({e}); trying the launch option anyway")
    cmd = steam_cmd(root)
    if steam_running() is not False:
        log("Closing Steam (downloads will pause and should resume afterward)…")
        try:
            if sys.platform == "darwin":
                subprocess.run(["osascript", "-e", 'quit app "Steam"'], timeout=30)
            else:
                subprocess.run(cmd + ["-shutdown"], timeout=30, creationflags=NOWIN)
        except Exception as e:
            log(f"Could not close Steam: {e}")
            return False
        for _ in range(60):
            time.sleep(1)
            r = steam_running()
            if r is None:
                time.sleep(10)
                break
            if not r:
                break
        else:
            log("Steam did not close. Close it manually and try again.")
            return False
        time.sleep(2)
    log("Starting Steam with -cef-enable-debugging…")
    args = (["open", "-a", "Steam", "--args"] if sys.platform == "darwin" else cmd) + ["-cef-enable-debugging"]
    kw = {"creationflags": 0x00000008 | 0x00000200} if sys.platform == "win32" else {"start_new_session": True}
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kw)
    except Exception as e:
        log(f"Could not start Steam: {e}")
        return False
    log("Steam started. Waiting for the debugging port…")
    return True


# ---------------------------------------------------------------------- GUI
class Card(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=8, relief="groove", borderwidth=2)
        self.img = tk.Label(self, text="…", width=18, height=4)
        self.img.grid(row=0, column=0, rowspan=3, padx=(0, 10))
        self.title = ttk.Label(self, font=("TkDefaultFont", 10, "bold"))
        self.title.grid(row=0, column=1, sticky="w")
        self.bar = ttk.Progressbar(self, maximum=1000)
        self.bar.grid(row=1, column=1, sticky="ew", pady=4)
        self.info = ttk.Label(self, wraplength=560)
        self.info.grid(row=2, column=1, sticky="w")
        self.columnconfigure(1, weight=1)

    def set_image(self, photo):
        if photo:
            self.img.config(image=photo, text="", width=THUMB[0], height=THUMB[1])

    def update_data(self, a, spd, lv=None, running=False):
        self.title.config(text=f"{a['name']}  (#{a['appid']})")
        if a["dl_t"] > 0:
            pct = a["dl"] / a["dl_t"]
        elif a["st_t"] > 0:
            pct = a["st"] / a["st_t"]
        else:
            pct = 1.0 if a["flags"] & 4 else 0.0
        self.bar["value"] = min(pct, 1) * 1000
        if a.get("live") and a["active"]:
            state = "DOWNLOADING" if a["running"] else ("paused" if a["paused"] else "queued")
            parts = [state + " (live)", f"{fb(a['dl'])} / {fb(a['dl_t'])} ({pct * 100:.1f}%)"]
            if a.get("net"):
                parts.append(f"network {fb(a['net'])}/s")
            if a.get("disc"):
                parts.append(f"disk {fb(a['disc'])}/s")
            if a["st_t"]:
                parts.append(f"writing {fb(a['st'])} / {fb(a['st_t'])}")
            if a.get("eta"):
                parts.append("ETA " + ft(a["eta"]))
            self.info.config(text="  \u2022  ".join(parts))
        elif a["active"]:
            state = "DOWNLOADING" if running else ("paused" if a["flags"] & 512 else "idle / queued")
            parts = [state,
                     f"last checkpoint: {fb(a['dl'])} / {fb(a['dl_t'])} ({pct * 100:.1f}%)"]
            if spd > 0:
                parts.append(f"{fb(spd)}/s written to disk")
            if lv:
                age = time.time() - max(lv["changed_at"], lv["newest"])
                parts.append(f"[file activity {int(age)}s ago]")
            self.info.config(text="  \u2022  ".join(parts))
        else:
            state = "Completed" if a["flags"] & 4 else "Queued / incomplete"
            self.info.config(text=f"{state}  \u2022  {fb(a['size'])} on disk")

class Tracker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Steam Download Tracker")
        self.geometry("820x660")
        ttk.Style(self).theme_use("clam")
        self.libs, self.parsed = set(), {}
        self.cards, self.layout = {}, []
        self.speed, self.photos, self.pending = {}, {}, set()
        self.q = queue.Queue()
        self.live, self.targets, self.net_prev = {}, {}, None
        self.cdp, self.cdp_state = None, "inactive"
        self.dbg_busy, self.dbg_wait_until, self.steam_dir = False, 0.0, steam_root()
        self.scheduled = False     # True when we have scheduled a Windows shutdown
        self.seen_active, self.idle_since, self.deadline = False, None, None
        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        for s in default_steam_dirs():
            self.libs.add(s / "steamapps")
            self.libs.update(libs_from_vdf(s))
        self.scan_async()
        threading.Thread(target=self.probe_loop, daemon=True).start()
        threading.Thread(target=self.cdp_loop, daemon=True).start()
        self.after(POLL_MS, self.tick)

    # ---- user interface
    def build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(top, textvariable=self.search, width=28).pack(side="left", padx=6)
        self.only_active = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Active only", variable=self.only_active).pack(side="left", padx=6)
        ttk.Button(top, text="Scan drives", command=self.scan_async).pack(side="left", padx=4)
        ttk.Button(top, text="Add folder…", command=self.add_folder).pack(side="left")
        dbg = ttk.Frame(self, padding=(8, 0, 8, 4))
        dbg.pack(fill="x")
        self.dbg_var = tk.StringVar(value="Steam debug: checking…")
        ttk.Label(dbg, textvariable=self.dbg_var).pack(side="left")
        self.off_btn = ttk.Button(dbg, text="Disable debug", command=self.on_disable_debug)
        self.off_btn.pack(side="right")
        ttk.Button(dbg, text="Live data dump", command=self.on_dump).pack(side="right", padx=4)
        self.dbg_btn = ttk.Button(dbg, text="Enable Steam debug", command=self.on_enable_debug)
        self.dbg_btn.pack(side="right", padx=4)
        self.status = tk.StringVar(value="Starting…")
        ttk.Label(self, textvariable=self.status, padding=(8, 0)).pack(fill="x")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, pady=6)
        self.canvas = tk.Canvas(mid, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.win, width=e.width))
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        bot = ttk.Frame(self, padding=8)
        bot.pack(fill="x")
        self.shut = tk.BooleanVar(value=False)
        ttk.Checkbutton(bot, text="Shut down PC after downloads finish, with a delay of", variable=self.shut).pack(side="left")
        self.delay = tk.IntVar(value=1)
        ttk.Spinbox(bot, from_=0, to=240, width=4, textvariable=self.delay).pack(side="left", padx=4)
        ttk.Label(bot, text="min").pack(side="left")
        ttk.Button(bot, text="Cancel", command=self.cancel_shutdown).pack(side="right")
        self.shut_lbl = ttk.Label(bot, text="")
        self.shut_lbl.pack(side="right", padx=10)

    def add_folder(self):
        d = filedialog.askdirectory(title="Select a Steam library or steamapps folder")
        if not d:
            return
        p = Path(d)
        if p.name.lower() != "steamapps" and (p / "steamapps").is_dir():
            p = p / "steamapps"
        self.libs.add(p)

    # ---- scanning and thumbnails (thread)
    def scan_async(self):
        self.status.set("Scanning drives…")
        threading.Thread(target=lambda: self.q.put(("libs", scan_drives())), daemon=True).start()

    def get_photo(self, appid):
        if appid in self.photos:
            return self.photos[appid]
        p = CACHE / f"{appid}.jpg"
        if Image and p.exists():
            try:
                im = Image.open(p).convert("RGB").resize(THUMB)
                self.photos[appid] = ImageTk.PhotoImage(im)
                return self.photos[appid]
            except Exception:
                pass
        return None

    def request_thumb(self, appid):
        if not Image or appid in self.pending or (CACHE / f"{appid}.jpg").exists():
            return
        self.pending.add(appid)

        def work():
            for url in THUMB_URLS:
                try:
                    req = urllib.request.Request(url.format(appid), headers={"User-Agent": "Mozilla/5.0"})
                    (CACHE / f"{appid}.jpg").write_bytes(urllib.request.urlopen(req, timeout=10).read())
                    break
                except Exception:
                    continue
            self.q.put(("thumb", appid))

        threading.Thread(target=work, daemon=True).start()

    def drain(self):
        while True:
            try:
                kind, val = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "libs":
                self.libs |= val
            elif kind == "dbgmsg":
                self.dbg_var.set(val)
            elif kind == "dbgdone":
                self.dbg_busy = False
                if val:
                    self.dbg_wait_until = time.time() + 120
            elif kind == "thumb":
                self.pending.discard(val)
                ph = self.get_photo(val)
                for (lib, aid), c in self.cards.items():
                    if aid == val:
                        c.set_image(ph)

    # ---- file activity monitoring (thread)
    def probe_loop(self):
        while True:
            for key, (lib, appid, inst) in list(self.targets.items()):
                try:
                    stage, game, newest = probe_dirs(lib, appid, inst)
                    size = stage + game
                except Exception:
                    continue
                now = time.time()
                old = self.live.get(key)
                if old is None:
                    self.live[key] = dict(size=size, stage=stage, newest=newest, t=now, changed_at=0.0, spd=0.0)
                elif size != old["size"] or newest != old["newest"]:
                    spd = old["spd"]
                    if size > old["size"]:
                        spd = (size - old["size"]) / max(now - old["t"], 0.001)
                    self.live[key] = dict(size=size, stage=stage, newest=newest, t=now, changed_at=now, spd=spd)
                else:
                    old["t"] = now
                    if now - old["changed_at"] > 10:
                        old["spd"] = 0.0
            time.sleep(2)

    def on_dump(self):
        txt = json.dumps(self.cdp[1] if self.cdp else None, indent=2, default=str)
        try:
            (CACHE / "live_dump.json").write_text(txt, encoding="utf-8")
        except OSError:
            pass
        win = tk.Toplevel(self)
        win.title("Live data dump (copy and paste it into chat)")
        win.geometry("680x520")
        bar = ttk.Frame(win, padding=6)
        bar.pack(fill="x")

        def copy():
            self.clipboard_clear()
            self.clipboard_append(txt)
            note.config(text="Copied to clipboard")

        def open_dir():
            try:
                if sys.platform == "win32":
                    os.startfile(CACHE)
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(CACHE)])
                else:
                    subprocess.run(["xdg-open", str(CACHE)])
            except Exception as e:
                note.config(text=f"Error: {e}")

        ttk.Button(bar, text="Copy all", command=copy).pack(side="left")
        ttk.Button(bar, text="Open file folder", command=open_dir).pack(side="left", padx=6)
        note = ttk.Label(bar, text=str(CACHE / "live_dump.json"))
        note.pack(side="left", padx=6)
        box = tk.Text(win, wrap="none")
        box.insert("1.0", txt)
        box.pack(fill="both", expand=True)

    # ---- enabling and disabling Steam debugging
    def on_enable_debug(self):
        if websocket is None:
            messagebox.showinfo("Missing dependency", "Install this package first:  pip install websocket-client")
            return
        if not messagebox.askyesno(
                "Enable Steam debug",
                "Steam must be restarted with CEF debugging enabled to read live data.\n\n"
                "Steam will be closed and reopened. Active downloads will pause and should resume automatically.\n\n"
                "Continue?"):
            return
        self.dbg_busy = True
        self.dbg_btn.config(state="disabled")

        def work():
            ok = enable_steam_debug(lambda m: self.q.put(("dbgmsg", m)))
            self.q.put(("dbgdone", ok))

        threading.Thread(target=work, daemon=True).start()

    def on_disable_debug(self):
        try:
            (self.steam_dir / MARKER).unlink()
            messagebox.showinfo("Debug disabled",
                                "Marker removed. Steam will no longer open the debugging port after its next restart.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ---- live data from Steam via the Chrome DevTools Protocol (thread)
    def cdp_loop(self):
        port = int(os.environ.get("STEAM_CDP_PORT", "8080"))
        while True:
            try:
                if websocket is None:
                    self.cdp_state = "install websocket-client"
                    time.sleep(10)
                    continue
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3))
                t = next(x for x in targets
                         if x.get("title") == "SharedJSContext" or "SharedJSContext" in x.get("url", ""))
                ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
                self.cdp_state = "connected"
                n = 0
                while True:
                    n += 1
                    ws.send(json.dumps({"id": n, "method": "Runtime.evaluate",
                                        "params": {"expression": JS_HOOK, "returnByValue": True}}))
                    while True:
                        r = json.loads(ws.recv())
                        if r.get("id") == n:
                            break
                    val = r.get("result", {}).get("result", {}).get("value")
                    if val:
                        self.cdp = (time.time(), json.loads(val))
                    time.sleep(1)
            except Exception:
                self.cdp = None
                self.cdp_state = "unreachable"
                time.sleep(3)

    @staticmethod
    def apply_live(a, it, ov):
        done = bool(it.get("completed"))
        a.update(live=True, paused=bool(it.get("paused")),
                 running=bool(it.get("active")) and not it.get("paused"), active=not done)
        if "total_bytes" in it:             # Legacy flat format
            a.update(dl=_int(it.get("downloaded_bytes")), dl_t=_int(it.get("total_bytes")))
        if done:
            a["flags"] |= 4
        if ov and _int(ov.get("update_appid")) == a["appid"]:
            (dl, dl_t), (st, st_t) = ov_progress(ov)
            if not dl_t:                    # Legacy overview format
                dl, dl_t = _int(ov.get("update_bytes_downloaded")), _int(ov.get("update_bytes_to_download"))
                st, st_t = _int(ov.get("update_bytes_staged")), _int(ov.get("update_bytes_to_stage"))
            if dl_t:
                a.update(dl=dl, dl_t=dl_t)
            if st_t:
                a.update(st=st, st_t=st_t)
            eta = _int(ov.get("overall_estimated_time_remaining_sec"))
            if eta <= 0:
                eta = _int(ov.get("update_seconds_remaining"))
            a.update(net=ov.get("update_network_bytes_per_second") or 0,
                     disc=ov.get("update_disc_bytes_per_second") or 0, eta=max(eta, 0))
            a["paused"] = a["paused"] or bool(ov.get("paused"))
            a["running"] = ov.get("update_state") in ("Starting", "Updating") and not a["paused"]
        return a

    def merge_live(self, apps):
        snap = self.cdp
        if not snap or time.time() - snap[0] > 5:
            return apps
        data = snap[1]
        items = {_int(it.get("appid")): it for it in flatten_items(data.get("items")) if _int(it.get("appid"))}
        ov = data.get("overview") or {}
        ua = _int(ov.get("update_appid"))
        upd = ov.get("update_state") in ("Starting", "Updating")
        if ua and ua not in items:      # Recover active downloads missing from the item list using the overview
            (dl, tot), _ = ov_progress(ov)
            if not tot:
                dl, tot = _int(ov.get("update_bytes_downloaded")), _int(ov.get("update_bytes_to_download"))
            if tot and (dl < tot or upd):
                items[ua] = {"appid": ua, "active": upd, "paused": bool(ov.get("paused")), "completed": False}
        out, seen = [], set()
        for a in apps:
            it = items.get(a["appid"])
            if it:
                was_active = a["active"]
                a = self.apply_live(dict(a), it, ov)
                if it.get("active") and ua != a["appid"] and not was_active:
                    a["active"], a["running"] = False, False       # Item list is stale; the manifest says the download finished
                seen.add(a["appid"])
            out.append(a)
        for appid, it in items.items():      # Downloads without a manifest (for example, just-started downloads)
            if appid not in seen and not it.get("completed"):
                base = dict(appid=appid, name=f"App {appid}", lib="live", installdir="", flags=0,
                            size=0, dl=0, dl_t=0, st=0, st_t=0, active=False)
                out.append(self.apply_live(base, it, ov))
        return out

    # ---- main loop
    def tick(self):
        try:
            self.drain()
            self.refresh()
        except Exception as e:
            self.status.set(f"Error: {e}")
        self.after(POLL_MS, self.tick)

    def refresh(self):
        now = time.time()
        apps, seen = [], set()
        for lib in list(self.libs):
            try:
                files = list(lib.glob("appmanifest_*.acf"))
            except OSError:
                continue
            for f in files:
                try:
                    stt = f.stat()
                except OSError:
                    continue
                sig = (stt.st_mtime_ns, stt.st_size)
                c = self.parsed.get(f)
                if not c or c[0] != sig:
                    c = (sig, parse_manifest(f))
                    self.parsed[f] = c
                if c[1]:
                    apps.append(c[1])
                    seen.add(f)
        for f in [f for f in self.parsed if f not in seen]:
            del self.parsed[f]

        apps = self.merge_live(apps)
        q = self.search.get().strip().lower()
        visible = []
        for a in apps:
            key = (a["lib"], a["appid"])
            # Update speed only when the manifest changes.
            t0, b0, s0 = self.speed.get(key, (now, a["dl"], 0.0))
            if a["dl"] > b0 and now > t0:
                s0 = (a["dl"] - b0) / (now - t0) if s0 == 0 else 0.6 * s0 + 0.4 * (a["dl"] - b0) / (now - t0)
                t0, b0 = now, a["dl"]
            elif a["dl"] < b0:
                t0, b0, s0 = now, a["dl"], 0.0
            if now - t0 > 15 or not a["active"]:
                s0 = 0.0
            self.speed[key] = (t0, b0, s0)

            card = self.cards.get(key)
            if not card:
                card = self.cards[key] = Card(self.inner)
                card.set_image(self.get_photo(a["appid"]))
                self.request_thumb(a["appid"])
            if a["active"]:
                self.targets[key] = (a["lib"], a["appid"], a["installdir"])
            else:
                self.targets.pop(key, None)
            lv = self.live.get(key)
            running = bool(lv) and now - max(lv["changed_at"], lv["newest"]) < 12
            card.update_data(a, lv["spd"] if lv and lv["spd"] > 0 else s0, lv, running)
            if (not q or q in a["name"].lower() or q == str(a["appid"])) and \
                    (a["active"] or not self.only_active.get()):
                visible.append((not a["active"], a["name"].lower(), key))

        for key in [k for k in self.cards if k not in {(a["lib"], a["appid"]) for a in apps}]:
            self.cards.pop(key).destroy()

        order = [k for _, _, k in sorted(visible)]
        if order != self.layout:
            for c in self.cards.values():
                c.pack_forget()
            for k in order:
                self.cards[k].pack(fill="x", padx=6, pady=3)
            self.layout = order

        n_act = sum(1 for a in apps if a["active"])
        net = ""
        if psutil:
            c = psutil.net_io_counters().bytes_recv
            if self.net_prev:
                net = f"  •  Network: {fb((c - self.net_prev[1]) / max(now - self.net_prev[0], 0.001))}/s"
            self.net_prev = (now, c)
        self.status.set(f"{len(self.libs)} libraries  •  {len(apps)} games  •  {n_act} active downloads{net}  \u2022  Steam live: {self.cdp_state if not (self.cdp and now - self.cdp[0] < 5) else 'ON'}")
        live_on = bool(self.cdp and now - self.cdp[0] < 5)
        if not self.dbg_busy:
            waiting = now < self.dbg_wait_until
            if live_on:
                d = self.cdp[1]
                its = flatten_items(d.get("items"))
                k0 = len(its[0]) if its else 0
                self.dbg_var.set(f"Steam debug: active (items {len(its)} [fields {k0}], "
                                 f"overview fields {len(d.get('overview') or {})}, "
                                 f"callback {d.get('ni')}/{d.get('no')})")
            elif waiting:
                self.dbg_var.set("Waiting for Steam to open the debugging port…")
            else:
                self.dbg_var.set(f"Steam debug: inactive ({self.cdp_state})")
            self.dbg_btn.config(state="disabled" if (live_on or waiting) else "normal")
        marker = bool(self.steam_dir) and (self.steam_dir / MARKER).exists()
        self.off_btn.config(state="normal" if marker else "disabled")
        self.shutdown_logic(sum(1 for a in apps if a["active"] and not a.get("paused")))

    # ---- shutdown
    def shutdown_logic(self, n_active):
        if not self.shut.get():
            if self.scheduled:
                self.abort_native()
            self.seen_active, self.idle_since, self.deadline = False, None, None
            self.shut_lbl.config(text="")
            return
        now = time.time()
        if n_active:                        # A download starts or resumes; cancel any pending shutdown.
            if self.scheduled:
                self.abort_native()
            self.seen_active, self.idle_since, self.deadline = True, None, None
            self.shut_lbl.config(text=f"Armed: {n_active} active downloads")
            return
        if not self.seen_active:
            self.shut_lbl.config(text="Armed: waiting for a download…")
            return
        if self.idle_since is None:
            self.idle_since = now
        if self.deadline is None:
            if now - self.idle_since < IDLE_GRACE:
                self.shut_lbl.config(text="Downloads finished; checking the queue…")
                return
            secs = max(0, self.delay.get()) * 60
            if sys.platform == "win32":
                secs = max(secs, 30)        # Windows displays a warning only when the timer is greater than zero.
                self.schedule_native(secs)  # If this fails, use the internal countdown.
            self.deadline = now + secs
        left = self.deadline - now
        if left > 0:
            via = " (scheduled by Windows)" if self.scheduled else ""
            self.shut_lbl.config(text=f"Shutting down in {ft(left)}{via}")
        elif self.scheduled:
            self.shut_lbl.config(text="Shutting down…")
        else:
            self.do_shutdown()

    def schedule_native(self, secs):
        """On Windows, 'shutdown /s /t N' shows a system warning and countdown."""
        try:
            r = subprocess.run(["shutdown", "/s", "/t", str(int(secs)), "/c",
                                "Steam downloads complete: shutdown scheduled"],
                               capture_output=True, timeout=15, creationflags=NOWIN)
            self.scheduled = r.returncode == 0
        except Exception:
            self.scheduled = False
        return self.scheduled

    def abort_native(self):
        try:
            subprocess.run(["shutdown", "/a"], capture_output=True, timeout=15, creationflags=NOWIN)
        except Exception:
            pass
        self.scheduled = False
        self.deadline = None

    def cancel_shutdown(self):
        self.shut.set(False)
        if self.scheduled:
            self.abort_native()
        self.deadline = None
        self.shut_lbl.config(text="Shutdown canceled")

    def on_close(self):
        if self.scheduled and messagebox.askyesno(
                "Shutdown scheduled",
                "A shutdown is scheduled.\n\nYes = cancel it and close the tracker\n"
                "No = close the tracker and let Windows shut down the PC"):
            self.abort_native()
        self.destroy()

    def do_shutdown(self):
        """Shut down immediately (Linux/macOS, or if scheduling failed on Windows)."""
        self.shut.set(False)
        self.deadline = None
        try:
            if sys.platform == "win32":
                subprocess.run(["shutdown", "/s", "/t", "0"], creationflags=NOWIN)
            elif sys.platform == "darwin":
                subprocess.run(["osascript", "-e", 'tell app "System Events" to shut down'])
            else:
                subprocess.run(["systemctl", "poweroff"])
        except Exception as e:
            self.shut_lbl.config(text=f"Shutdown error: {e}")


if __name__ == "__main__":
    Tracker().mainloop()
