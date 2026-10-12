"""Movie Server - stream a movie folder to your phone over Wi-Fi.

Usage:  python movie_server.py ["E:\\Movies"] [port]

Open the address it prints on your phone. Movies play in VLC, thumbnails are
random frames grabbed with ffmpeg, and the last position is remembered.
Thumbnails and watch progress are stored in ./movie_server_data next to this file.
"""
import hashlib
import json
import mimetypes
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else r"E:\Movies")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "movie_server_data")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
STATE_FILE = os.path.join(DATA_DIR, "library.json")

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".ts"}
CHUNK = 512 * 1024
MIN_BYTES_FOR_PROGRESS = 8 * 1024 * 1024  # ignore tiny reads (VLC probing headers/index)
REWIND_SECONDS = 20  # resume a little earlier than the estimated stop point
WATCHED_AT = 0.94
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp4", ".m4v")


# ---------------------------------------------------------------- state ----

lock = threading.RLock()
library = {}  # id -> parsed file info
state = {}  # id -> {duration, hdr, height, thumb, pos_bytes, watched, last_played}
dirty = False


def load_state():
    global state
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}


def save_state():
    global dirty
    with lock:
        if not dirty:
            return
        data = json.dumps(state, indent=1)
        dirty = False
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, STATE_FILE)


def mark_dirty():
    global dirty
    dirty = True


def saver():
    while True:
        time.sleep(5)
        try:
            save_state()
        except OSError as e:
            log(f"Could not save state: {e}")


# --------------------------------------------------------- name parsing ----

LANGS = {
    "hindi": "Hindi", "hin": "Hindi", "english": "English", "eng": "English",
    "tamil": "Tamil", "tam": "Tamil", "telugu": "Telugu", "tel": "Telugu",
    "malayalam": "Malayalam", "mal": "Malayalam", "kannada": "Kannada",
    "kan": "Kannada", "marathi": "Marathi", "mar": "Marathi",
    "bengali": "Bengali", "ben": "Bengali", "japanese": "Japanese",
    "korean": "Korean",
}


def parse_name(stem):
    s = re.sub(r"\.(mkv|mp4|avi|mov|m4v|webm)\b.*$", "", stem, flags=re.I)
    order = None
    m = re.match(r"^\s*(\d{1,2})\.\s*(.+)", s)
    if m:
        order, s = int(m[1]), m[2]

    s = s.replace("_ ", ": ").replace("_", " ")
    if s.count(".") >= 2 and s.count(" ") < 2:
        s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()

    year = None
    ep = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", s, re.I)
    ym = next((m for m in re.finditer(r"(?<!\d)(19[3-9]\d|20[0-4]\d)(?!\d)", s) if m.start() > 0), None)
    if ep:
        cut = ep.start()
    elif ym:
        cut, year = ym.start(), int(ym[1])
    else:
        q = re.search(r"\b(2160p|1080p|720p|480p|4k|web-?dl|webrip|hdrip|bluray|x26[45]|hevc)\b|[\[{]", s, re.I)
        cut = q.start() if q and q.start() > 0 else len(s)
    title, tail = s[:cut], s[cut:]

    title = re.sub(r"[(\[{][^)\]}]*[)\]}]", "", title)
    title = re.sub(r"[(\[{].*$", "", title)
    title = re.sub(r"\b(?:[A-Z] )+[A-Z]\b", lambda m: m[0].replace(" ", ""), title)  # K G F -> KGF
    title = re.sub(r"\s+", " ", title).strip(" -.:~")
    code = None
    if ep:
        code = f"S{int(ep[1]):02d}E{int(ep[2]):02d}"
        title = f"{title} {code}" if title else code

    low = s.lower()
    quality = next((label for pat, label in [
        (r"2160p|\b4k\b", "4K"), (r"1080p", "1080p"), (r"720p", "720p"), (r"480p", "480p"),
    ] if re.search(pat, low)), None)
    hdr = bool(re.search(r"\bhdr|\bdv\b|dolby.?vision", low))
    langs = []
    for word in re.findall(r"[a-z]+", tail.lower()):
        lang = LANGS.get(word)
        if lang and lang not in langs:
            langs.append(lang)
    return {"title": title or stem, "year": year, "order": order, "ep": code,
            "quality": quality, "hdr_name": hdr, "langs": langs}


def clean_folder(name):
    name = re.sub(r"\s*\(\d+\)$", "", name).strip()
    if name.isupper() and len(name) > 4:
        name = name.title()
    return name


def migrate_id(old, new):
    """Carry state over from the older path-based ids."""
    with lock:
        if old == new or old not in state or new in state:
            return
        state[new] = state.pop(old)
        mark_dirty()
    try:
        os.replace(os.path.join(THUMB_DIR, old + ".jpg"), os.path.join(THUMB_DIR, new + ".jpg"))
    except OSError:
        pass


def scan():
    found = {}
    for dirpath, dirnames, files in os.walk(ROOT):
        dirnames.sort()
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() not in VIDEO_EXT:
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, ROOT)
            try:
                st = os.stat(full)
            except OSError:
                continue
            parts = rel.split(os.sep)
            # id from name + size, so moving a file between folders keeps its progress and thumbnail
            mid = hashlib.sha1(f"{f}|{st.st_size}".encode("utf-8")).hexdigest()[:12]
            migrate_id(hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12], mid)
            info = parse_name(os.path.splitext(f)[0])
            info.update(
                id=mid, rel=rel, full=full, name=f, size=st.st_size, added=st.st_mtime,
                collection=clean_folder(parts[0]) if len(parts) > 1 else "Movies",
                sub=parse_name(parts[1])["title"] if len(parts) > 2 else None,
                group="/".join(parts[:2]) if len(parts) > 2 else None,
            )
            found[mid] = info
    with lock:
        library.clear()
        library.update(found)
        need = [mid for mid in found if mid not in failed and (
            not state.get(mid, {}).get("thumb") or not state.get(mid, {}).get("duration"))]
    for mid in need:
        enqueue(mid)


# ------------------------------------------------- ffmpeg: probe & thumbs ----

work_q = queue.Queue()
queued = set()
force_thumb = set()
failed = set()  # files ffmpeg could not read; not retried until restart

TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")


def enqueue(mid, new_thumb=False):
    with lock:
        if new_thumb:
            force_thumb.add(mid)
        if mid in queued:
            return
        queued.add(mid)
    work_q.put(mid)


def probe(path):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration:stream=color_transfer,height", "-of", "json", path],
        capture_output=True, timeout=120, creationflags=NO_WINDOW)
    d = json.loads(r.stdout or b"{}")
    stream = (d.get("streams") or [{}])[0]
    return {
        "duration": float(d.get("format", {}).get("duration") or 0),
        "hdr": stream.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        "height": stream.get("height") or 0,
    }


def make_thumb(mid, path, duration, hdr):
    at = random.uniform(0.10, 0.70) * duration if duration else 60
    out = os.path.join(THUMB_DIR, mid + ".jpg")
    tmp = os.path.join(THUMB_DIR, mid + ".tmp.jpg")
    for prefix in ([TONEMAP, ""] if hdr else [""]):
        cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{at:.1f}", "-i", path, "-an", "-sn",
               "-vf", prefix + "scale=960:-2,thumbnail=40", "-frames:v", "1", "-q:v", "4", tmp]
        r = subprocess.run(cmd, capture_output=True, timeout=180, creationflags=NO_WINDOW)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return True
    return False


def worker():
    while True:
        mid = work_q.get()
        try:
            with lock:
                info = library.get(mid)
                st = state.setdefault(mid, {})
                redo = mid in force_thumb
                force_thumb.discard(mid)
            if not info or not FFMPEG or not FFPROBE:
                continue
            if not st.get("duration"):
                st.update(probe(info["full"]))
                mark_dirty()
            if redo or not st.get("thumb"):
                if make_thumb(mid, info["full"], st.get("duration"), st.get("hdr")):
                    st["thumb"] = st.get("thumb", 0) + 1
                    mark_dirty()
                else:
                    failed.add(mid)
            if not st.get("duration"):
                failed.add(mid)
        except Exception as e:  # keep the worker alive on odd files
            failed.add(mid)
            log(f"Skipped {info and info['rel']}: {e}")
        finally:
            with lock:
                queued.discard(mid)


# ------------------------------------------------------------- progress ----

def note_progress(mid, size, offset):
    with lock:
        st = state.setdefault(mid, {})
        st["last_played"] = time.time()
        if offset / size >= WATCHED_AT:
            st["watched"], st["pos_bytes"] = True, 0
        else:
            st["watched"], st["pos_bytes"] = False, offset
        mark_dirty()


def movie_json(info):
    st = state.get(info["id"], {})
    dur = st.get("duration") or 0
    pos = 0
    if st.get("pos_bytes") and dur and not st.get("watched"):
        pos = max(0, st["pos_bytes"] / info["size"] * dur - REWIND_SECONDS)
        if pos < 60:
            pos = 0
    quality = info["quality"]
    h = st.get("height") or 0
    if not quality and h:
        quality = "4K" if h >= 1800 else "1080p" if h >= 1000 else "720p" if h >= 700 else "SD"
    return {
        "id": info["id"], "title": info["title"], "year": info["year"], "order": info["order"],
        "collection": info["collection"], "sub": info["sub"], "group": info["group"],
        "ep": info["ep"], "rel": info["rel"],
        "quality": quality, "hdr": bool(st.get("hdr") or info["hdr_name"]), "langs": info["langs"],
        "size": info["size"], "added": info["added"], "duration": round(dur),
        "position": round(pos), "watched": bool(st.get("watched")),
        "last_played": st.get("last_played", 0), "thumb": st.get("thumb", 0),
        "url": "/v/" + info["id"] + "/" + urllib.parse.quote(info["name"]),
    }


# ----------------------------------------------------------------- http ----

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # client went away

    def do_GET(self):
        self.route(head=False)

    def do_HEAD(self):
        self.route(head=True)

    def route(self, head):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send_bytes(PAGE.encode(), "text/html; charset=utf-8", head)
        if path == "/api/library":
            scan()
            with lock:
                movies = [movie_json(i) for i in library.values()]
                pending = len(queued)
            body = json.dumps({"movies": movies, "pending": pending, "ffmpeg": bool(FFMPEG)})
            return self.send_bytes(body.encode(), "application/json", head)
        m = re.fullmatch(r"/thumb/([0-9a-f]{12})\.jpg", path)
        if m:
            f = os.path.join(THUMB_DIR, m[1] + ".jpg")
            if os.path.isfile(f):
                with open(f, "rb") as fh:
                    return self.send_bytes(fh.read(), "image/jpeg", head, cache=True)
            return self.send_error(404)
        m = re.fullmatch(r"/v/([0-9a-f]{12})/.*", path)
        if m:
            return self.stream(m[1], head)
        self.send_error(404)

    def do_POST(self):
        m = re.fullmatch(r"/api/movie/([0-9a-f]{12})/(\w+)", urllib.parse.urlparse(self.path).path)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if not m or m[1] not in library:
            return self.send_error(404)
        mid, action = m[1], m[2]
        with lock:
            st = state.setdefault(mid, {})
            if action == "watched":
                st["watched"], st["pos_bytes"] = True, 0
            elif action == "unwatched":
                st["watched"], st["pos_bytes"] = False, 0
            elif action == "played":
                st["last_played"] = time.time()
            elif action != "thumb":
                return self.send_error(400)
            mark_dirty()
        if action == "thumb":
            enqueue(mid, new_thumb=True)
        self.send_bytes(b'{"ok":true}', "application/json", False)

    def send_bytes(self, body, ctype, head, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000" if cache else "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def stream(self, mid, head):
        info = library.get(mid)
        if not info or not os.path.isfile(info["full"]):
            return self.send_error(404)
        size = os.path.getsize(info["full"])
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].strip().partition("-")
            if s:
                start = int(s)
                end = int(e) if e else end
            else:
                start = max(0, size - int(e))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(info["full"])[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if head:
            return

        counts = start < size * 0.97  # reads near the end are usually VLC fetching the index
        sent, last_note = 0, 0.0
        try:
            with open(info["full"], "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    data = fh.read(min(CHUNK, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    sent += len(data)
                    left -= len(data)
                    if counts and sent >= MIN_BYTES_FOR_PROGRESS and time.time() - last_note > 3:
                        note_progress(mid, size, start + sent)
                        last_note = time.time()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass  # player seeked or closed
        finally:
            if counts and sent >= MIN_BYTES_FOR_PROGRESS:
                note_progress(mid, size, start + sent)
            self.close_connection = True

    def log_message(self, fmt, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b10">
<title>Cinema</title>
<style>
:root{
  --bg:#0b0b10;--panel:#16161e;--panel2:#1f1f2a;--line:#262633;
  --text:#f3f3f6;--muted:#9b9bab;--accent:#ff8a3d;--accent2:#ff4d6d;
  --grad:linear-gradient(135deg,var(--accent),var(--accent2));
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,Inter,sans-serif}
body{min-height:100vh;overflow-x:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
a{color:inherit;text-decoration:none}

/* top bar */
.top{position:fixed;inset:0 0 auto 0;z-index:20;display:flex;align-items:center;gap:12px;
  padding:12px 16px;padding-top:max(12px,env(safe-area-inset-top));
  background:linear-gradient(#0b0b10ee,#0b0b1000);transition:background .2s}
.top.solid{background:#0b0b10e6;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border-bottom:1px solid var(--line)}
.brand{font-weight:800;font-size:20px;letter-spacing:-.02em;display:flex;align-items:center;gap:8px;white-space:nowrap}
.brand i{width:26px;height:26px;border-radius:8px;background:var(--grad);display:grid;place-items:center;font-style:normal;font-size:12px}
.search{margin-left:auto;flex:0 1 320px;position:relative}
.search input{width:100%;padding:10px 14px 10px 36px;border-radius:999px;border:1px solid var(--line);
  background:#ffffff10;color:var(--text);font-size:15px;outline:none}
.search input:focus{border-color:#ffffff40;background:#ffffff18}
.search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);opacity:.6}

/* hero */
.hero{position:relative;height:min(68vh,560px);min-height:380px;display:flex;align-items:flex-end;overflow:hidden}
.hero .bg{position:absolute;inset:0;background-size:cover;background-position:center;transform:scale(1.04);
  transition:opacity .6s}
.hero::after{content:"";position:absolute;inset:0;
  background:linear-gradient(0deg,var(--bg) 2%,#0b0b10cc 30%,#0b0b1033 65%,#0b0b1088),
             linear-gradient(90deg,#0b0b10cc,#0b0b1000 70%)}
.hero .in{position:relative;z-index:1;padding:0 16px 28px;max-width:720px}
.kicker{font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
.hero h1{margin:8px 0 10px;font-size:clamp(30px,7vw,58px);line-height:1;letter-spacing:-.03em;font-weight:800}
.meta{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;color:var(--muted);font-size:14px}
.meta b{color:var(--text);font-weight:600}
.pill{font-size:11px;font-weight:700;padding:3px 7px;border-radius:6px;border:1px solid #ffffff30;color:var(--text);letter-spacing:.03em}
.pill.hot{border:0;background:var(--grad)}
.btns{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;padding:12px 20px;border-radius:12px;font-weight:700;font-size:15px;
  background:#ffffff1c;border:1px solid #ffffff22;transition:transform .15s,background .15s}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--grad);border:0;box-shadow:0 8px 28px #ff5a4f40}
.btn:hover{background:#ffffff2a}.btn.primary:hover{filter:brightness(1.08)}
.hbar{margin-top:14px;height:4px;width:min(320px,70vw);background:#ffffff26;border-radius:4px;overflow:hidden}
.hbar i{display:block;height:100%;background:var(--grad)}

/* rows */
main{padding-bottom:40px}
.row{margin-top:26px}
.rhead{display:flex;align-items:center;margin:0 16px 4px}
.row h2{margin:0;font-size:18px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:baseline;gap:8px}
.row h2 span{font-size:13px;color:var(--muted);font-weight:500}
.arrows{margin-left:auto;display:flex;gap:6px}
.arrows[hidden]{display:none}
.arrows button{width:34px;height:34px;border-radius:50%;background:#ffffff14;border:1px solid var(--line);
  display:grid;place-items:center;font-size:18px;line-height:1;transition:background .15s,opacity .15s}
.arrows button:hover{background:#ffffff2a}
.arrows button:disabled{opacity:.3;cursor:default}
.scroller{display:grid;grid-auto-flow:column;grid-auto-columns:clamp(168px,44vw,290px);gap:12px;
  overflow-x:auto;overflow-y:hidden;padding:14px 16px 10px;scroll-snap-type:x proximity;scroll-padding:0 16px;
  scrollbar-width:none;-webkit-overflow-scrolling:touch;touch-action:pan-x pan-y;overscroll-behavior-x:contain}
.scroller::-webkit-scrollbar{display:none}
.scroller.drag{scroll-snap-type:none;cursor:grabbing;user-select:none}
.scroller.drag .card{pointer-events:none}
@media (hover:hover) and (pointer:fine){
  .scroller{scrollbar-width:thin;scrollbar-color:#ffffff2e transparent;cursor:grab}
  .scroller::-webkit-scrollbar{display:block;height:6px}
  .scroller::-webkit-scrollbar-thumb{background:#ffffff2e;border-radius:6px}
  .scroller::-webkit-scrollbar-track{background:transparent}
}
@media (hover:none){.arrows{display:none}}
.row.wide .scroller{grid-auto-columns:clamp(240px,72vw,380px)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:18px 12px;padding:4px 16px}

.card{text-align:left;scroll-snap-align:start;display:block;width:100%}
.th{position:relative;aspect-ratio:16/9;border-radius:12px;overflow:hidden;background:var(--panel2);
  box-shadow:0 0 0 1px #ffffff0d inset;transition:transform .2s}
.card:hover .th{transform:translateY(-3px)}
.card:active .th{transform:scale(.98)}
.th img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .4s}
.th img.ok{opacity:1}
.ph{position:absolute;inset:0;display:grid;place-items:center;padding:10px;text-align:center;
  font-weight:800;font-size:15px;letter-spacing:-.01em;color:#ffffffd0}
.ph small{display:block;font-size:11px;font-weight:500;opacity:.7;margin-top:4px}
.th .badge{position:absolute;top:8px;right:8px;z-index:1}
.th .done{position:absolute;top:8px;left:8px;z-index:1;width:22px;height:22px;border-radius:50%;
  background:#000000a0;display:grid;place-items:center;font-size:12px}
.th .bar{position:absolute;left:0;right:0;bottom:0;height:4px;background:#00000080;z-index:1}
.th .bar i{display:block;height:100%;background:var(--grad)}
.th .play{position:absolute;inset:0;display:grid;place-items:center;opacity:0;transition:opacity .2s;background:#0005;z-index:1}
.card:hover .th .play{opacity:1}
.th .play b{width:44px;height:44px;border-radius:50%;background:#ffffffe0;color:#111;display:grid;place-items:center}
.ct{margin-top:8px;font-size:14px;font-weight:600;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cm{margin-top:3px;font-size:12px;color:var(--muted)}
.empty{color:var(--muted);padding:40px 16px;text-align:center}

/* sheet */
.sheet{position:fixed;inset:0;z-index:50;display:flex;align-items:flex-end;justify-content:center}
.sheet[hidden]{display:none}
.sheet .back{position:absolute;inset:0;background:#000000a8;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);animation:fade .2s}
.panel{position:relative;width:100%;max-width:640px;max-height:92vh;overflow:auto;background:var(--panel);
  border-radius:20px 20px 0 0;animation:up .28s cubic-bezier(.2,.9,.3,1);padding-bottom:env(safe-area-inset-bottom)}
@media (min-width:720px){.sheet{align-items:center}.panel{border-radius:20px}}
@keyframes up{from{transform:translateY(40px);opacity:0}}
@keyframes fade{from{opacity:0}}
.shero{position:relative;aspect-ratio:16/9;background:var(--panel2);overflow:hidden}
.shero img{width:100%;height:100%;object-fit:cover;display:block}
.shero::after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,var(--panel),#16161e00 55%)}
.x{position:absolute;top:12px;right:12px;z-index:2;width:36px;height:36px;border-radius:50%;background:#000000a0;
  display:grid;place-items:center;font-size:18px}
.sbody{padding:0 20px 24px;margin-top:-34px;position:relative;z-index:1}
.sbody h3{margin:0 0 8px;font-size:26px;letter-spacing:-.02em;line-height:1.1}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.chip{font-size:12px;padding:4px 10px;border-radius:999px;background:#ffffff12;color:#d8d8e2}
.sbtns{display:flex;flex-direction:column;gap:10px;margin-top:20px}
.sbtns .btn{justify-content:center;padding:14px}
.minor{display:flex;gap:8px;margin-top:12px}
.minor button{flex:1;padding:11px;border-radius:12px;background:#ffffff0d;border:1px solid var(--line);font-size:13px;font-weight:600;color:#d8d8e2}
.path{margin-top:16px;font-size:11px;color:#6f6f80;word-break:break-all}
.sprog{margin-top:14px;font-size:13px;color:var(--muted)}
.sprog .hbar{width:100%;margin-top:6px}

/* folder cards */
.card.folder .th{box-shadow:0 0 0 1px #ffffff14 inset,0 -6px 0 -3px #34344a,0 -11px 0 -6px #25253a}
.card.folder .th::after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,#000000b0,#0000 55%)}
.count{position:absolute;left:8px;bottom:9px;z-index:1;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;
  padding:4px 9px;border-radius:8px;background:#000000a0;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
.count svg{opacity:.9}

/* folder page */
.fview{position:fixed;inset:0;z-index:40;background:var(--bg);overflow-y:auto;overscroll-behavior:contain;
  animation:slide .25s cubic-bezier(.2,.9,.3,1)}
.fview[hidden]{display:none}
@keyframes slide{from{transform:translateX(28px);opacity:0}}
.fhero{position:relative;height:min(44vh,380px);min-height:230px;background-size:cover;background-position:center}
.fhero::after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,var(--bg) 3%,#0b0b10b0 45%,#0b0b1050)}
.fback{position:absolute;z-index:2;top:max(12px,env(safe-area-inset-top));left:12px;height:38px;padding:0 16px 0 10px;
  border-radius:999px;background:#000000a0;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  display:flex;align-items:center;gap:4px;font-weight:600;font-size:14px}
.fback b{font-size:24px;line-height:1;margin-top:-3px}
.fbody{position:relative;z-index:1;margin-top:-96px;padding:0 16px 48px}
.fbody h1{margin:6px 0 10px;font-size:clamp(28px,6vw,46px);letter-spacing:-.03em;line-height:1.05}
.list{margin-top:24px;display:grid;gap:10px}
.item{display:flex;gap:12px;align-items:center;padding:8px;border-radius:14px;background:var(--panel);
  border:1px solid var(--line);cursor:pointer;transition:background .15s}
.item:hover{background:var(--panel2)}
.item .th{width:42%;max-width:210px;flex:none;border-radius:10px}
.item .ph{font-size:12px}
.item .info{flex:1;min-width:0}
.item .it{font-weight:700;font-size:15px;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.item .cm{margin-top:4px}
.ipl{flex:none;width:40px;height:40px;margin-right:4px;border-radius:50%;background:#ffffff14;display:grid;place-items:center;font-size:13px;transition:background .15s}
.ipl:hover{background:var(--grad)}
@media (min-width:900px){.fbody{padding:0 40px 48px;max-width:1100px;margin:-96px auto 0}.list{grid-template-columns:1fr 1fr}}

.toast{position:fixed;left:50%;bottom:max(18px,env(safe-area-inset-bottom));transform:translateX(-50%);z-index:30;
  background:#1f1f2af0;border:1px solid var(--line);padding:9px 16px;border-radius:999px;font-size:13px;color:#cfcfda;
  display:flex;gap:8px;align-items:center;backdrop-filter:blur(10px)}
.toast[hidden]{display:none}
.spin{width:12px;height:12px;border-radius:50%;border:2px solid #ffffff40;border-top-color:var(--accent);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (min-width:900px){.top,.hero .in{padding-left:40px;padding-right:40px}
  .rhead{margin-left:40px;margin-right:40px}.scroller{padding-left:40px;padding-right:40px;scroll-padding:0 40px}.grid{padding:4px 40px}}
</style>
</head><body>
<header class="top" id="top">
  <div class="brand"><i>&#9654;</i>Cinema</div>
  <label class="search">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input id="q" type="search" placeholder="Search movies" autocomplete="off">
  </label>
</header>
<section class="hero" id="hero"></section>
<main id="main"></main>
<div class="fview" id="folder" hidden></div>
<div class="sheet" id="sheet" hidden><div class="back" data-close></div><div class="panel" id="panel"></div></div>
<div class="toast" id="toast" hidden><div class="spin"></div><span></span></div>

<script>
const UA = navigator.userAgent, IOS = /iPhone|iPad|iPod/.test(UA) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1), ANDROID = /Android/.test(UA);
let LIB = [], BY = {}, heroId = null, polling = false;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dur = s => { if (!s) return ''; const t = Math.round(s/60), h = Math.floor(t/60), m = t%60; return h ? `${h}h ${String(m).padStart(2,'0')}m` : `${m}m`; };
const clock = s => { s = Math.round(s); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60; return (h ? h+':'+String(m).padStart(2,'0') : m) + ':' + String(x).padStart(2,'0'); };
const gb = b => (b/1024**3).toFixed(1) + ' GB';
const pct = m => m.duration && m.position ? Math.min(100, m.position/m.duration*100) : 0;
const thumb = m => m.thumb ? `/thumb/${m.id}.jpg?v=${m.thumb}` : '';
const hue = id => parseInt(id.slice(0,4), 16) % 360;
const ph = m => `background:linear-gradient(135deg,hsl(${hue(m.id)} 45% 26%),hsl(${(hue(m.id)+50)%360} 50% 12%))`;
const inProgress = m => m.position > 0 && !m.watched;

function playHref(m, fromStart) {
  const url = location.origin + m.url;
  const pos = fromStart ? 0 : Math.floor((m.position || 0) * 1000);
  if (ANDROID) return 'intent:' + url.replace(/^https?:/, '') + '#Intent;scheme=http;package=org.videolan.vlc;type=video/*;S.title='
    + encodeURIComponent(m.title) + ';l.position=' + pos + (fromStart ? ';B.from_start=true' : '') + ';end';
  if (IOS) return 'vlc-x-callback://x-callback-url/stream?url=' + encodeURIComponent(url);
  return url;
}
function play(m, fromStart) {
  navigator.sendBeacon(`/api/movie/${m.id}/played`, '');
  if (!ANDROID && !IOS) window.open(playHref(m, fromStart), '_blank'); else location.href = playHref(m, fromStart);
}

function metaLine(m, sep = ' · ') {
  return [m.year, m.duration && dur(m.duration), m.quality].filter(Boolean).join(sep);
}

function thumbBox(m, extra = '') {
  const p = pct(m);
  return `<div class="th" style="${ph(m)}">
      <div class="ph">${esc(m.title)}${m.year ? `<small>${m.year}</small>` : ''}</div>
      <img data-id="${m.id}" alt="" loading="lazy" ${m.thumb ? `src="${thumb(m)}"` : ''} onload="this.classList.add('ok')">
      ${m.quality === '4K' || m.hdr ? `<span class="pill hot badge">${m.quality === '4K' ? '4K' : 'HDR'}</span>` : ''}
      ${m.watched ? '<span class="done">&#10003;</span>' : ''}
      ${p ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}${extra}
    </div>`;
}

function card(m) {
  return `<button class="card" data-open="${m.id}">
    ${thumbBox(m, '<div class="play"><b>&#9654;</b></div>')}
    <div class="ct">${esc(m.title)}</div>
    <div class="cm" data-meta="${m.id}">${inProgress(m) ? dur(m.duration - m.position) + ' left' : esc(metaLine(m))}</div>
  </button>`;
}

// ---- folders inside folders (e.g. "Fallout Season 1") ----
const folderItems = key => LIB.filter(m => m.group === key);
const shortName = m => m.ep ? `Episode ${parseInt(m.ep.slice(4), 10)}` : m.title;
const nextUp = items => items.filter(inProgress).sort((a, b) => b.last_played - a.last_played)[0] || items.find(m => !m.watched) || items[0];
const FOLDER_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

function folderStats(items) {
  return {
    cover: items.find(m => m.thumb) || items[0],
    watched: items.filter(m => m.watched).length,
    total: items.reduce((s, m) => s + (m.duration || 0), 0),
    noun: items.every(m => m.ep) ? 'episodes' : 'videos',
  };
}

function groupList(items) {   // collapse items that share a sub-folder into one folder entry
  const out = [], seen = new Set();
  for (const m of items) {
    if (!m.group) out.push(m);
    else if (!seen.has(m.group)) { seen.add(m.group); out.push({folder: m.group}); }
  }
  return out;
}

function folderCard(key) {
  const items = folderItems(key), f = items[0], {cover, watched, total, noun} = folderStats(items);
  const up = items.filter(inProgress).sort((a, b) => b.last_played - a.last_played)[0];
  return `<button class="card folder" data-folder="${esc(key)}">
    <div class="th" style="${ph(cover)}">
      <div class="ph">${esc(f.sub)}</div>
      <img data-id="${cover.id}" alt="" loading="lazy" ${cover.thumb ? `src="${thumb(cover)}"` : ''} onload="this.classList.add('ok')">
      <span class="count">${FOLDER_ICON}${items.length} ${noun}</span>
      ${watched ? `<div class="bar"><i style="width:${watched / items.length * 100}%"></i></div>` : ''}
    </div>
    <div class="ct">${esc(f.sub)}</div>
    <div class="cm">${up ? 'Continue &middot; ' + esc(shortName(up)) : watched ? `${watched} of ${items.length} watched` : (dur(total) || '')}</div>
  </button>`;
}

function row(title, entries, wide) {
  if (!entries.length) return '';
  const n = entries.reduce((s, x) => s + (x.folder ? folderItems(x.folder).length : 1), 0);
  return `<section class="row${wide ? ' wide' : ''}">
    <div class="rhead"><h2>${esc(title)} <span>${n}</span></h2>
      <div class="arrows" hidden><button data-dir="-1" aria-label="Scroll left">&#8249;</button><button data-dir="1" aria-label="Scroll right">&#8250;</button></div></div>
    <div class="scroller">${entries.map(x => x.folder ? folderCard(x.folder) : card(x)).join('')}</div></section>`;
}

let openFolderKey = null;
function renderFolder(key) {
  const items = folderItems(key);
  if (!items.length) return hideFolder();
  const f = items[0], {cover, watched, total, noun} = folderStats(items), up = nextUp(items);
  const el = document.getElementById('folder'), keep = el.hidden ? 0 : el.scrollTop;
  const playAttr = m => (inProgress(m) ? 'data-play' : 'data-start') + `="${m.id}"`;
  el.innerHTML = `
    <div class="fhero" style="${cover.thumb ? `background-image:url('${thumb(cover)}')` : ph(cover)}">
      <button class="fback" data-back><b>&#8249;</b> Back</button></div>
    <div class="fbody">
      <div class="kicker">${esc(f.collection)}</div>
      <h1>${esc(f.sub)}</h1>
      <div class="meta"><b>${items.length} ${noun}</b>${total ? `<span>${dur(total)}</span>` : ''}${watched ? `<span>${watched} of ${items.length} watched</span>` : ''}</div>
      <div class="btns"><button class="btn primary" ${playAttr(up)}>&#9654; ${inProgress(up) ? 'Resume' : up.watched ? 'Watch again' : 'Play'} ${esc(shortName(up))}</button></div>
      <div class="list">${items.map(m => `
        <div class="item" data-open="${m.id}" role="button" tabindex="0">
          ${thumbBox(m)}
          <div class="info"><div class="it">${esc(shortName(m))}</div>
            <div class="cm">${esc([m.ep || m.year, m.duration && (inProgress(m) ? dur(m.duration - m.position) + ' left' : dur(m.duration)), m.ep ? null : m.quality].filter(Boolean).join(' · '))}</div></div>
          <button class="ipl" ${playAttr(m)} aria-label="Play">&#9654;</button>
        </div>`).join('')}</div>
    </div>`;
  el.hidden = false;
  el.scrollTop = keep;
  syncOverflow();
}
function hideFolder() { document.getElementById('folder').hidden = true; openFolderKey = null; syncOverflow(); }
function syncOverflow() {
  const open = !document.getElementById('sheet').hidden || !document.getElementById('folder').hidden;
  document.body.style.overflow = open ? 'hidden' : '';
}

function updateArrows(sc) {
  const a = sc.parentElement.querySelector('.arrows'); if (!a) return;
  const max = sc.scrollWidth - sc.clientWidth;
  a.hidden = max <= 4;
  a.children[0].disabled = sc.scrollLeft <= 4;
  a.children[1].disabled = sc.scrollLeft >= max - 4;
}

function wireScrollers() {
  document.querySelectorAll('.scroller').forEach(sc => {
    updateArrows(sc);
    sc.addEventListener('scroll', () => updateArrows(sc), {passive: true});
  });
}
addEventListener('resize', () => document.querySelectorAll('.scroller').forEach(updateArrows));

// click-and-drag rows with a mouse
let drag = null;
document.addEventListener('pointerdown', e => {
  const sc = e.target.closest('.scroller');
  if (sc && e.pointerType === 'mouse' && e.button === 0) drag = {sc, x0: e.clientX, s0: sc.scrollLeft, moved: false};
});
addEventListener('pointermove', e => {
  if (!drag) return;
  const dx = e.clientX - drag.x0;
  if (!drag.moved && Math.abs(dx) > 6) { drag.moved = true; drag.sc.classList.add('drag'); }
  if (drag.moved) drag.sc.scrollLeft = drag.s0 - dx;
});
addEventListener('pointerup', () => {
  if (drag && drag.moved) { const sc = drag.sc; setTimeout(() => sc.classList.remove('drag'), 0); }
  drag = null;
});

function renderHero() {
  const cont = LIB.filter(inProgress).sort((a, b) => b.last_played - a.last_played);
  let m = cont[0];
  if (!m) {
    if (!heroId || !BY[heroId]) {
      const pool = LIB.filter(x => !x.watched && x.thumb); const any = pool.length ? pool : LIB;
      heroId = any.length ? any[Math.floor(Math.random() * any.length)].id : null;
    }
    m = BY[heroId];
  }
  const el = document.getElementById('hero');
  if (!m) { el.innerHTML = '<div class="in"><h1>No movies found</h1></div>'; return; }
  const p = pct(m);
  el.innerHTML = `<div class="bg" style="${m.thumb ? `background-image:url('${thumb(m)}')` : ph(m)}"></div>
    <div class="in">
      <div class="kicker">${inProgress(m) ? 'Continue watching' : 'Tonight\'s pick'}</div>
      <h1>${esc(m.title)}</h1>
      <div class="meta">${m.year ? `<b>${m.year}</b>` : ''}${m.duration ? `<span>${dur(m.duration)}</span>` : ''}
        ${m.quality ? `<span class="pill">${m.quality}</span>` : ''}${m.hdr ? '<span class="pill">HDR</span>' : ''}
        ${m.langs.length ? `<span>${esc(m.langs.slice(0, 3).join(', '))}${m.langs.length > 3 ? ' +' + (m.langs.length - 3) : ''}</span>` : ''}</div>
      ${p ? `<div class="hbar"><i style="width:${p}%"></i></div>` : ''}
      <div class="btns">
        <button class="btn primary" data-play="${m.id}">&#9654; ${inProgress(m) ? 'Resume ' + clock(m.position) : 'Play'}</button>
        <button class="btn" data-open="${m.id}">Details</button>
      </div>
    </div>`;
}

function render() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const main = document.getElementById('main'), hero = document.getElementById('hero');
  if (q) {
    hero.style.display = 'none';
    const hits = LIB.filter(m => (m.title + ' ' + m.collection + ' ' + (m.sub || '') + ' ' + m.rel).toLowerCase().includes(q));
    main.innerHTML = `<section class="row" style="margin-top:84px"><h2>Results <span>${hits.length}</span></h2>
      ${hits.length ? `<div class="grid">${hits.map(card).join('')}</div>` : '<div class="empty">Nothing matches that.</div>'}</section>`;
    return;
  }
  hero.style.display = '';
  renderHero();
  const cont = LIB.filter(inProgress).sort((a, b) => b.last_played - a.last_played);
  const recent = groupList([...LIB].sort((a, b) => b.added - a.added)).slice(0, 14);
  const cols = {};
  for (const m of LIB) (cols[m.collection] ||= []).push(m);
  const names = Object.keys(cols).sort((a, b) => (a === 'Movies' ? -1 : b === 'Movies' ? 1 : a.localeCompare(b)));
  main.innerHTML = row('Continue watching', cont, true) + row('Recently added', recent)
    + names.map(n => row(n, groupList(cols[n]))).join('')
    + (LIB.length ? '' : '<div class="empty">No videos found in the folder.</div>');
  wireScrollers();
  if (openFolderKey) renderFolder(openFolderKey);
}

// ---- navigation: folder page and details sheet use browser history so the phone's Back button closes them ----
function openFolder(key) { openFolderKey = key; renderFolder(key); history.pushState({folder: key}, ''); }
function openSheet(id) { showSheet(id); history.pushState({folder: openFolderKey, sheet: id}, ''); }
function goBack() { if (history.state) history.back(); else applyState(null); }
function applyState(s) {
  if (s && s.sheet) showSheet(s.sheet); else hideSheet();
  if (s && s.folder) { if (openFolderKey !== s.folder || document.getElementById('folder').hidden) { openFolderKey = s.folder; renderFolder(s.folder); } }
  else hideFolder();
  syncOverflow();
}
addEventListener('popstate', e => applyState(e.state));
if (history.state) history.replaceState(null, '');

function showSheet(id) {
  const m = BY[id]; if (!m) return;
  const p = pct(m);
  document.getElementById('panel').innerHTML = `
    <div class="shero" style="${ph(m)}">${m.thumb ? `<img src="${thumb(m)}" data-id="${m.id}" alt="">` : `<img data-id="${m.id}" alt="" hidden>`}
      <button class="x" data-close aria-label="Close">&#10005;</button></div>
    <div class="sbody">
      <h3>${esc(m.title)}</h3>
      <div class="meta">${m.year ? `<b>${m.year}</b>` : ''}${m.duration ? `<span>${dur(m.duration)}</span>` : ''}
        ${m.quality ? `<span class="pill">${m.quality}</span>` : ''}${m.hdr ? '<span class="pill">HDR</span>' : ''}<span>${gb(m.size)}</span></div>
      <div class="chips"><span class="chip">${esc(m.collection)}${m.sub ? ' / ' + esc(m.sub) : ''}</span>${m.langs.map(l => `<span class="chip">${l}</span>`).join('')}</div>
      ${inProgress(m) ? `<div class="sprog">Stopped at ${clock(m.position)} &middot; ${dur(m.duration - m.position)} left<div class="hbar"><i style="width:${p}%"></i></div></div>` : ''}
      <div class="sbtns">
        ${inProgress(m)
          ? `<button class="btn primary" data-play="${m.id}">&#9654; Resume from ${clock(m.position)}</button><button class="btn" data-start="${m.id}">Start over</button>`
          : `<button class="btn primary" data-start="${m.id}">&#9654; ${m.watched ? 'Watch again' : 'Play'}</button>`}
      </div>
      <div class="minor">
        <button data-act="${m.watched ? 'unwatched' : 'watched'}" data-id="${m.id}">${m.watched ? 'Mark unwatched' : '&#10003; Mark watched'}</button>
        <button data-act="thumb" data-id="${m.id}">&#8635; New thumbnail</button>
      </div>
      ${IOS ? '<div class="path">On iPhone, VLC starts from the beginning. Use the resume time above to skip ahead.</div>' : ''}
      <div class="path">${esc(m.rel)}</div>
    </div>`;
  document.getElementById('sheet').hidden = false;
  syncOverflow();
}
function hideSheet() { document.getElementById('sheet').hidden = true; syncOverflow(); }

function setLib(movies) { LIB = movies; BY = Object.fromEntries(movies.map(m => [m.id, m])); }

function toast(pending, ffmpeg) {
  const t = document.getElementById('toast');
  if (!ffmpeg) { t.hidden = false; t.querySelector('.spin').style.display = 'none'; t.querySelector('span').textContent = 'ffmpeg not found - thumbnails disabled'; return; }
  t.hidden = !pending;
  t.querySelector('span').textContent = `Making thumbnails... ${pending} left`;
}

async function load(full) {
  let d;
  try { d = await (await fetch('/api/library', {cache: 'no-store'})).json(); } catch { return; }
  const old = BY;
  setLib(d.movies);
  toast(d.pending, d.ffmpeg);
  if (full) render();
  else for (const m of d.movies) {   // patch in new thumbnails/durations without resetting scroll
    const o = old[m.id];
    if (o && o.thumb !== m.thumb && m.thumb) document.querySelectorAll(`img[data-id="${m.id}"]`).forEach(i => { i.hidden = false; i.src = thumb(m); });
    if (o && o.duration !== m.duration) document.querySelectorAll(`[data-meta="${m.id}"]`).forEach(e => e.textContent = metaLine(m));
  }
  if (!full && heroId && old[heroId] && old[heroId].thumb !== BY[heroId]?.thumb) renderHero();
  if (d.pending && !polling) { polling = true; setTimeout(() => { polling = false; load(false); }, 4000); }
}

document.addEventListener('click', e => {
  const t = e.target.closest('[data-open],[data-play],[data-start],[data-close],[data-back],[data-act],[data-dir],[data-folder]');
  if (!t) return;
  if (t.dataset.dir) {
    const sc = t.closest('.row').querySelector('.scroller');
    return sc.scrollBy({left: Number(t.dataset.dir) * sc.clientWidth * 0.85, behavior: 'smooth'});
  }
  if (t.dataset.close !== undefined || t.dataset.back !== undefined) return goBack();
  if (t.dataset.folder) return openFolder(t.dataset.folder);
  if (t.dataset.open) return openSheet(t.dataset.open);
  if (t.dataset.play) return play(BY[t.dataset.play], false);
  if (t.dataset.start) return play(BY[t.dataset.start], true);
  if (t.dataset.act) {
    const id = t.dataset.id, act = t.dataset.act;
    fetch(`/api/movie/${id}/${act}`, {method: 'POST'}).then(() => load(act !== 'thumb').then(() => { if (act !== 'thumb') showSheet(id); }));
    if (act === 'thumb') { t.textContent = 'Generating...'; t.disabled = true; }
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && (!document.getElementById('sheet').hidden || openFolderKey)) goBack();
  if (e.key === 'Enter' && e.target.matches('.item')) openSheet(e.target.dataset.open);
});
document.getElementById('q').addEventListener('input', render);
addEventListener('scroll', () => document.getElementById('top').classList.toggle('solid', scrollY > 40), {passive: true});
document.addEventListener('visibilitychange', () => { if (!document.hidden && document.getElementById('sheet').hidden) load(true); });
load(true);
</script>
</body></html>
"""


class Server(ThreadingHTTPServer):
    # Windows lets two sockets share a port with SO_REUSEADDR; claim it exclusively so a
    # second copy fails cleanly instead of running alongside the first.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def log(msg):
    """Print and append to movie_server_data/server.log (useful when started hidden at login)."""
    print(msg)
    try:
        with open(os.path.join(DATA_DIR, "server.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except OSError:
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    os.makedirs(THUMB_DIR, exist_ok=True)
    load_state()
    if not (FFMPEG and FFPROBE):
        log("ffmpeg/ffprobe not found on PATH - thumbnails and resume times are disabled.")
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=saver, daemon=True).start()
    scan()
    for f in os.listdir(THUMB_DIR) if library else []:  # drop thumbnails of videos that are gone
        if f.endswith(".jpg") and f[:-4] not in library:
            try:
                os.remove(os.path.join(THUMB_DIR, f))
            except OSError:
                pass
    url = f"http://{lan_ip()}:{PORT}"
    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError:
        log(f"Port {PORT} is busy - Local Cinema is probably already running at {url}")
        return
    log(f"Serving {ROOT}  ({len(library)} videos)")
    log(f"Open on your phone:  {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mark_dirty()
        save_state()


if __name__ == "__main__":
    main()
