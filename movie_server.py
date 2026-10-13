"""Local Cinema - stream your movie folders to your phone over Wi-Fi.

Usage:  python movie_server.py [port]

Then open http://cinema.local:8000 (or the address it prints) and add your
movie folders from the PC. Movies play in VLC, thumbnails are random frames
grabbed with ffmpeg, and the last position is remembered. Folders, thumbnails
and watch progress are stored in ./movie_server_data next to this file.
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
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Arguments: an optional port, and (for older setups) an optional folder that seeds
# the folder list the first time the server runs.
_ARGS = sys.argv[1:]
PORT = next((int(a) for a in _ARGS if a.isdigit()), 8000)
SEED_FOLDER = next((os.path.abspath(a) for a in _ARGS if not a.isdigit()), None)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "movie_server_data")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
STATE_FILE = os.path.join(DATA_DIR, "library.json")
FOLDERS_FILE = os.path.join(DATA_DIR, "folders.json")
MDNS_NAME = "cinema.local"

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


# ------------------------------------------------------------- folders ----

folders = []  # absolute paths of the library folders, in the order they were added


def load_folders():
    global folders
    try:
        with open(FOLDERS_FILE, encoding="utf-8") as fh:
            folders = [f for f in json.load(fh).get("folders", []) if isinstance(f, str)]
    except (OSError, ValueError):
        folders = [SEED_FOLDER] if SEED_FOLDER and os.path.isdir(SEED_FOLDER) else []
        save_folders()


def save_folders():
    tmp = FOLDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"folders": folders}, fh, indent=1)
    os.replace(tmp, FOLDERS_FILE)


def _inside(child, parent):
    child, parent = os.path.normcase(child), os.path.normcase(parent)
    return child != parent and child.startswith(parent.rstrip("\\/") + os.sep)


def add_folder(path):
    """Add a library folder. Returns an error message, or None on success."""
    path = os.path.abspath(path.strip().strip('"'))
    if not os.path.isdir(path):
        return "That folder doesn't exist."
    with lock:
        for f in folders:
            if os.path.normcase(f) == os.path.normcase(path):
                return "That folder is already in your library."
            if _inside(path, f):
                return f"It's already included, because it's inside {f}."
        # a parent replaces any of its subfolders that were added before
        folders[:] = [f for f in folders if not _inside(f, path)] + [path]
        save_folders()
    return None


def remove_folder(path):
    with lock:
        folders[:] = [f for f in folders if os.path.normcase(f) != os.path.normcase(path)]
        save_folders()


def folder_label(root):
    name = os.path.basename(root.rstrip("\\/"))
    return clean_folder(name) if name else root.rstrip("\\/")  # a drive root like E:\ shows as "E:"


def is_hidden(path, name):
    if name.startswith((".", "$")) or name in ("System Volume Information", "Recovery", "Windows"):
        return True
    try:
        return bool(getattr(os.stat(path), "st_file_attributes", 0) & 0x6)  # hidden or system
    except OSError:
        return True


def browse(path):
    """Folder listing for the in-page folder picker. '' lists drives and common places."""
    if not path:
        if os.name == "nt":
            roots = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
        else:
            roots = ["/"]
        home = os.path.expanduser("~")
        places = [os.path.join(home, n) for n in ("Videos", "Movies", "Downloads", "Desktop")
                  if os.path.isdir(os.path.join(home, n))]
        dirs = [{"name": r.rstrip("\\"), "path": r, "kind": "drive"} for r in roots]
        dirs += [{"name": os.path.basename(p), "path": p, "kind": "place"} for p in places]
        return {"path": "", "parent": None, "dirs": dirs, "videos": 0}
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"error": "That folder doesn't exist."}
    dirs, videos = [], 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir() and not is_hidden(e.path, e.name):
                        dirs.append({"name": e.name, "path": e.path, "kind": "dir"})
                    elif e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                        videos += 1
                except OSError:
                    pass
    except OSError:
        return {"error": "Windows didn't allow reading that folder."}
    dirs.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(path.rstrip("\\/")) if os.path.dirname(path.rstrip("\\/")) != path.rstrip("\\/") else ""
    if os.name == "nt" and len(path.rstrip("\\")) <= 2:  # E:\ -> back to the drive list
        parent = ""
    return {"path": path, "parent": parent, "dirs": dirs, "videos": videos}


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
    with lock:
        roots = list(folders)
    for root in roots:
        label = folder_label(root)
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "$")))
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() not in VIDEO_EXT:
                    continue
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
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
                    id=mid, rel=os.path.join(label, rel), full=full, name=f, size=st.st_size,
                    added=st.st_mtime, root=root,
                    # each top-level subfolder is a row; loose files go in a row named after the folder
                    collection=clean_folder(parts[0]) if len(parts) > 1 else label,
                    sub=parse_name(parts[1])["title"] if len(parts) > 2 else None,
                    group=root + "|" + "/".join(parts[:2]) if len(parts) > 2 else None,
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


# ------------------------------------------------------------- QR code ----
# A small QR encoder: byte mode, error correction level M, versions 1-10 (up to 213 bytes).

_QR_BLOCKS = {  # version: (EC codewords per block, [(block count, data codewords per block), ...])
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
             8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x = (_x << 1) ^ (0x11D if _x & 0x80 else 0)
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_ec(data, n):
    gen = [1]
    for i in range(n):  # product of (x - a^i)
        nxt = gen + [0]
        for j, g in enumerate(gen):
            nxt[j + 1] ^= _gmul(g, _EXP[i])
        gen = nxt
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gmul(gen[j], c)
    return rem[len(data):]


def qr_matrix(text):
    data = text.encode("utf-8")
    for ver in range(1, 11):
        ec, groups = _QR_BLOCKS[ver]
        cap = sum(b * d for b, d in groups)
        cc_bits = 8 if ver < 10 else 16
        if 4 + cc_bits + 8 * len(data) <= cap * 8:
            break
    else:
        raise ValueError("text too long for a QR code")
    bits = []
    put = lambda v, k: bits.extend((v >> (k - 1 - i)) & 1 for i in range(k))
    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    bits += [0] * (-len(bits) % 8)
    pad = 0
    while len(bits) < cap * 8:
        put((0xEC, 0x11)[pad % 2], 8)
        pad += 1
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    blocks, ecs, pos = [], [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(cw[pos:pos + size])
            ecs.append(_rs_ec(blocks[-1], ec))
            pos += size
    final = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)]
    final += [e[i] for i in range(ec) for e in ecs]

    n = 17 + 4 * ver
    M = [[False] * n for _ in range(n)]
    F = [[False] * n for _ in range(n)]

    def setf(x, y, v):
        M[y][x], F[y][x] = v, True

    for i in range(n):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < n and 0 <= cy + dy < n:
                    setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    al = _QR_ALIGN[ver]
    for i, ay in enumerate(al):
        for j, ax in enumerate(al):
            if (i, j) in ((0, 0), (0, len(al) - 1), (len(al) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = (0 << 3) | mask  # level M = 0b00
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        fb = ((d << 10) | rem) ^ 0x5412
        g = lambda i: (fb >> i) & 1 == 1
        for i in range(6):
            setf(8, i, g(i))
        setf(8, 7, g(6))
        setf(8, 8, g(7))
        setf(7, 8, g(8))
        for i in range(9, 15):
            setf(14 - i, 8, g(i))
        for i in range(8):
            setf(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            setf(8, n - 15 + i, g(i))
        setf(8, n - 8, True)

    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vb = (ver << 12) | rem
        for i in range(18):
            a, b = n - 11 + i % 3, i // 3
            setf(a, b, (vb >> i) & 1 == 1)
            setf(b, a, (vb >> i) & 1 == 1)

    allbits = [(final[i >> 3] >> (7 - (i & 7))) & 1 for i in range(len(final) * 8)]
    k, right = 0, n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not F[y][x] and k < len(allbits):
                    M[y][x] = allbits[k] == 1
                    k += 1
        right -= 2

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def penalty(G):
        p = 0
        for lines in (G, [list(c) for c in zip(*G)]):
            for line in lines:
                run = 1
                for i in range(1, n + 1):
                    if i < n and line[i] == line[i - 1]:
                        run += 1
                    else:
                        if run >= 5:
                            p += run - 2
                        run = 1
                s = "".join("1" if v else "0" for v in line)
                p += 40 * (s.count("00001011101") + s.count("10111010000"))
        for y in range(n - 1):
            for x in range(n - 1):
                if G[y][x] == G[y][x + 1] == G[y + 1][x] == G[y + 1][x + 1]:
                    p += 3
        dark = sum(map(sum, G))
        p += 10 * ((abs(dark * 20 - n * n * 10) + n * n - 1) // (n * n) - 1)
        return p

    best = None
    for mi, fn in enumerate(masks):
        G = [[M[y][x] ^ (not F[y][x] and fn(x, y)) for x in range(n)] for y in range(n)]
        saved = [row[:] for row in M]
        M[:] = G
        draw_format(mi)
        score = penalty(M)
        if best is None or score < best[0]:
            best = (score, [row[:] for row in M])
        M[:] = saved
    return best[1]


def qr_svg(text):
    m = qr_matrix(text)
    n, q = len(m), 4
    path = "".join(f"M{x + q},{y + q}h1v1h-1z" for y in range(n) for x in range(n) if m[y][x])
    size = n + 2 * q
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            f'<rect width="{size}" height="{size}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def qr_text(text):
    """QR drawn with block characters, for the console (light modules drawn, for dark terminals)."""
    m = qr_matrix(text)
    n, q = len(m), 2
    cell = lambda x, y: 0 <= x < n and 0 <= y < n and m[y][x]
    rows = []
    for y in range(-q, n + q, 2):
        rows.append("".join(" ▀▄█"[(not cell(x, y)) | (not cell(x, y + 1)) << 1] for x in range(-q, n + q)))
    return "\n".join(rows)


# ------------------------------------------------- cinema.local (mDNS) ----
# Answers multicast DNS lookups for cinema.local with this PC's address, so phones
# can use one fixed name even when the router gives the PC a different IP.

def local_ips():
    ips = {"127.0.0.1", lan_ip()}
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return ips


def _ip_for(peer):
    """The PC's address on the same subnet as the peer (falls back to the default route)."""
    prefix = peer.rsplit(".", 1)[0] + "."
    return next((ip for ip in sorted(local_ips()) if ip.startswith(prefix)), lan_ip())


def _dns_name(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def _read_name(data, i):
    labels, jumped, end = [], False, i
    for _ in range(64):
        ln = data[i]
        if ln == 0:
            i += 1
            break
        if (ln & 0xC0) == 0xC0:
            if not jumped:
                end = i + 2
            i, jumped = ((ln & 0x3F) << 8) | data[i + 1], True
            continue
        labels.append(data[i + 1:i + 1 + ln].decode("ascii", "replace"))
        i += 1 + ln
    return ".".join(labels).lower(), (end if jumped else i)


def _mdns_answer(ip, qid=0, question=b"", flush=True):
    head = struct.pack(">HHHHHH", qid, 0x8400, 1 if question else 0, 1, 0, 0)
    rr = _dns_name(MDNS_NAME) + struct.pack(">HHIH", 1, 0x8001 if flush else 1, 120, 4) + socket.inet_aton(ip)
    return head + question + rr


MDNS_GROUP = "224.0.0.251"
MDNS_REFRESH = 300  # rebuild the socket every 5 minutes so a Wi-Fi reset can't leave it deaf


def mdns_responder():
    """Keeps cinema.local answering for the life of the server. The socket is rebuilt after any
    error, after the PC wakes from sleep, when its addresses change, and every few minutes."""
    last_problem = None
    while True:
        try:
            _mdns_serve()
            last_problem = None
        except Exception as e:  # never let the name disappear for good
            msg = f"{MDNS_NAME} paused ({e}); retrying"
            if msg != last_problem:  # don't fill the log with the same line every few seconds
                log(msg)
                last_problem = msg
            time.sleep(5)


def _mdns_serve():
    ips = local_ips() - {"127.0.0.1"}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 5353))
        joined = 0
        for ip in ips:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(MDNS_GROUP) + socket.inet_aton(ip))
                joined += 1
            except OSError:
                pass
        if not joined:
            raise OSError("no network to announce on")
        s.settimeout(10)
        for ip in ips:  # tell everyone where cinema.local is now
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.sendto(_mdns_answer(ip), (MDNS_GROUP, 5353))
            except OSError:
                pass
        started = last_tick = time.time()
        while True:
            now = time.time()
            # rebuild after sleep (a big jump in the clock), on a schedule, or when the addresses change
            if now - last_tick > 60 or now - started > MDNS_REFRESH:
                return
            last_tick = now
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                if local_ips() - {"127.0.0.1"} != ips:
                    return
                continue
            except OSError:
                time.sleep(0.5)  # network briefly gone (sleep/resume); the checks above will rebuild
                continue
            _mdns_reply(s, data, addr)
    finally:
        s.close()


def _mdns_reply(s, data, addr):
    """Answer any question in this packet that asks for cinema.local."""
    try:
        qid, flags, qd = struct.unpack(">HHH", data[:6])
        if flags & 0x8000:
            return  # a response, not a question
        i = 12
        for _ in range(qd):
            start = i
            name, i = _read_name(data, i)
            qtype, qclass = struct.unpack(">HH", data[i:i + 4])
            i += 4
            if name != MDNS_NAME or qtype not in (1, 255):
                continue
            ip = _ip_for(addr[0])
            if addr[1] != 5353:  # a simple resolver asking directly: answer it directly
                s.sendto(_mdns_answer(ip, qid, data[start:i], flush=False), addr)
                continue
            if qclass & 0x8000:  # asked for a unicast reply
                s.sendto(_mdns_answer(ip), addr)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.sendto(_mdns_answer(ip), (MDNS_GROUP, 5353))
    except (struct.error, IndexError, OSError, UnicodeError, ValueError):
        pass  # ignore malformed or unrelated packets


# ----------------------------------------------------------------- http ----

seen_devices = set()

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

    def is_local(self):
        """True when the request comes from this PC. Only the PC may browse and change folders."""
        ip = self.client_address[0].replace("::ffff:", "")
        return ip in local_ips() or ip.startswith("127.") or ip == "::1"

    def note_device(self):
        ip = self.client_address[0].replace("::ffff:", "")
        if ip not in seen_devices:
            seen_devices.add(ip)
            log(f"Device connected: {ip}" + (" (this PC)" if self.is_local() else ""))

    def route(self, head):
        self.note_device()
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path == "/":
            return self.send_bytes(PAGE.encode(), "text/html; charset=utf-8", head)
        if path == "/api/library":
            scan()
            with lock:
                movies = [movie_json(i) for i in library.values()]
                pending = len(queued)
                counts = {}
                for i in library.values():
                    counts[i["root"]] = counts.get(i["root"], 0) + 1
                fl = [{"path": f, "name": folder_label(f), "exists": os.path.isdir(f), "count": counts.get(f, 0)}
                      for f in folders]
            body = json.dumps({"movies": movies, "pending": pending, "ffmpeg": bool(FFMPEG),
                               "folders": fl, "canManage": self.is_local(),
                               "urls": {"name": f"http://{MDNS_NAME}:{PORT}", "ip": f"http://{lan_ip()}:{PORT}"}})
            return self.send_bytes(body.encode(), "application/json", head)
        if path == "/qr.svg":
            return self.send_bytes(qr_svg(f"http://{lan_ip()}:{PORT}").encode(), "image/svg+xml", head)
        if path == "/api/browse":
            if not self.is_local():
                return self.send_json({"error": "Folders can only be browsed from the PC running Local Cinema."}, 403)
            return self.send_json(browse(urllib.parse.parse_qs(url.query).get("path", [""])[0]))
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
        self.note_device()
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if path == "/api/folders":
            if not self.is_local():
                return self.send_json({"error": "Folders can only be changed from the PC running Local Cinema."}, 403)
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                return self.send_json({"error": "Bad request."}, 400)
            if req.get("add"):
                err = add_folder(str(req["add"]))
                if err:
                    return self.send_json({"error": err}, 400)
                log(f"Folder added: {req['add']}")
            elif req.get("remove"):
                remove_folder(str(req["remove"]))
                log(f"Folder removed: {req['remove']}")
            scan()
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/movie/([0-9a-f]{12})/(\w+)", path)
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

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

/* top bar tools */
.tools{margin-left:auto;display:flex;gap:8px}
.top .search{margin-left:0}
.tbtn{display:inline-flex;align-items:center;gap:7px;height:40px;padding:0 15px;border-radius:999px;border:1px solid var(--line);
  background:#ffffff10;font-size:14px;font-weight:600;white-space:nowrap;transition:background .15s}
.tbtn:hover{background:#ffffff1e}
.tbtn.acc{background:var(--grad);border-color:transparent}
.tbtn.acc:hover{filter:brightness(1.08)}
@media (max-width:700px){.tbtn span{display:none}.tbtn{width:40px;padding:0;justify-content:center}}

/* dialogs */
.dlg{position:fixed;inset:0;z-index:60;display:flex;align-items:flex-end;justify-content:center}
.dlg[hidden]{display:none}
.dlg .back{position:absolute;inset:0;background:#000000a8;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);animation:fade .2s}
.dpanel{position:relative;width:100%;max-width:640px;max-height:92vh;display:flex;flex-direction:column;background:var(--panel);
  border-radius:20px 20px 0 0;animation:up .28s cubic-bezier(.2,.9,.3,1);padding-bottom:env(safe-area-inset-bottom)}
@media (min-width:720px){.dlg{align-items:center}.dpanel{border-radius:20px}}
.dhead{display:flex;align-items:center;gap:12px;padding:20px 22px 14px}
.dhead h3{margin:0;font-size:21px;letter-spacing:-.01em;flex:1}
.dclose{width:36px;height:36px;border-radius:50%;background:#ffffff12;display:grid;place-items:center;font-size:16px}
.dbody{padding:0 22px 22px;overflow:auto}
.sect{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:4px 0 10px}
.fl{display:grid;gap:8px;margin-bottom:22px}
.frow2{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:12px;background:var(--panel2);border:1px solid var(--line)}
.frow2 > svg{flex:none;color:var(--accent)}
.frow2 .fi{flex:1;min-width:0}
.frow2 b{display:block;font-size:15px}
.frow2 small{display:block;color:var(--muted);font:12px ui-monospace,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.frow2 .cnt{font-size:12px;color:var(--muted);white-space:nowrap}
.frow2 .cnt.bad{color:#f0b35a}
.rm{font-size:13px;font-weight:600;padding:7px 11px;border-radius:8px;color:#ffb4a8;background:#ff4d6d1c}
.rm.confirm{background:#ff4d6d;color:#fff}
.pathbar{display:flex;gap:8px;margin-bottom:10px}
.pathbar input{flex:1;min-width:0;padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:#0e0e14;color:var(--text);
  font:13px ui-monospace,Consolas,monospace;outline:none}
.pathbar input:focus{border-color:#ffffff40}
.pathbar button{padding:0 14px;border-radius:10px;background:#ffffff14;font-size:14px;font-weight:600}
.dirs{border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:min(40vh,330px);background:#0e0e14}
.dir{display:flex;align-items:center;gap:11px;width:100%;padding:11px 14px;border-bottom:1px solid #ffffff08;text-align:left;font-size:14.5px}
.dir:hover{background:#ffffff0d}
.dir svg{flex:none;color:#c9c9d6}
.dir .k{margin-left:auto;font-size:11px;color:var(--muted)}
.dir.up{color:var(--muted)}
.dempty{padding:18px;color:var(--muted);font-size:14px;text-align:center}
.addbar{display:flex;align-items:center;gap:14px;margin-top:14px;flex-wrap:wrap}
.addbar .note{flex:1;min-width:180px;font-size:13px;color:var(--muted);line-height:1.4}
.addbar .btn{padding:11px 18px}
.err{color:#ff9b8a;font-size:13.5px;margin-top:10px}
.btn[disabled]{opacity:.4;pointer-events:none}
.qrwrap{display:flex;gap:22px;align-items:center;flex-wrap:wrap}
.qrimg{width:196px;height:196px;border-radius:14px;background:#fff;padding:6px;flex:none}
.qrimg img{width:100%;height:100%;display:block;image-rendering:pixelated}
.urls{flex:1;min-width:220px;display:grid;gap:14px;text-align:left}
.url b{display:block;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.url code{display:block;font:15px ui-monospace,Consolas,monospace;color:var(--text);word-break:break-all}
.url small{display:block;color:var(--muted);font-size:12.5px;margin-top:3px;line-height:1.4}
.welcome{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:96px 16px 40px}
.wcard{max-width:720px;width:100%;text-align:center}
.wcard h1{font-size:clamp(32px,6vw,54px);letter-spacing:-.03em;margin:14px 0 12px;line-height:1.05}
.wcard p{color:var(--muted);font-size:17px;margin:0 auto 26px;max-width:520px;line-height:1.5}
.wcard .btn{font-size:16px;padding:14px 26px}
.wqr{margin:40px auto 0;max-width:600px;text-align:left;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}
.empty .btn{margin-top:14px}
</style>
</head><body>
<header class="top" id="top">
  <div class="brand"><i>&#9654;</i>Cinema</div>
  <div class="tools">
    <button class="tbtn" data-dlg="connect" title="Connect your phone">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3"/></svg>
      <span>Connect phone</span></button>
    <button class="tbtn acc" data-dlg="folders" title="Add or remove movie folders">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
      <span>Add folder</span></button>
  </div>
  <label class="search">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input id="q" type="search" placeholder="Search movies" autocomplete="off">
  </label>
</header>
<section class="hero" id="hero"></section>
<main id="main"></main>
<div class="fview" id="folder" hidden></div>
<div class="sheet" id="sheet" hidden><div class="back" data-close></div><div class="panel" id="panel"></div></div>
<div class="dlg" id="dlg" hidden><div class="back" data-close></div><div class="dpanel" id="dpanel"></div></div>
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
  const open = !document.getElementById('sheet').hidden || !document.getElementById('folder').hidden || !document.getElementById('dlg').hidden;
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
  if (!INFO.folders.length) {   // first run: no folders yet
    hero.style.display = 'none';
    main.innerHTML = `<section class="welcome"><div class="wcard">
      <div class="kicker">Welcome to Local Cinema</div>
      <h1>Add your first movie folder</h1>
      <p>${INFO.canManage
        ? 'Pick a folder on this PC. Every video inside it, subfolders included, shows up here, ready to stream to your phone.'
        : 'Folders are added on the PC that runs Local Cinema. Open this page there to pick one.'}</p>
      ${INFO.canManage ? `<button class="btn primary" data-dlg="folders">&#65291; Add folder</button>
        <div class="wqr"><div class="sect">Open on your phone</div>${qrBlock()}</div>` : ''}
    </div></section>`;
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
    + (LIB.length ? '' : `<div class="empty">No videos found in your folders yet.<br>
        <button class="btn" data-dlg="folders">Manage folders</button></div>`);
  wireScrollers();
  if (openFolderKey) renderFolder(openFolderKey);
}

// ---- navigation: folder page and details sheet use browser history so the phone's Back button closes them ----
function openFolder(key) { openFolderKey = key; renderFolder(key); history.pushState({folder: key}, ''); }
function openSheet(id) { showSheet(id); history.pushState({folder: openFolderKey, sheet: id}, ''); }
function openDialog(kind) { showDialog(kind); history.pushState({folder: openFolderKey, dlg: kind}, ''); }
function goBack() { if (history.state) history.back(); else applyState(null); }
function applyState(s) {
  if (s && s.sheet) showSheet(s.sheet); else hideSheet();
  if (s && s.dlg) showDialog(s.dlg); else hideDialog();
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

// ---- dialogs: library folders and connect-your-phone ----
let INFO = {folders: [], canManage: false, urls: {}}, dlgKind = null, browseData = null, rmConfirm = null, dlgErr = '';
const ICON_FOLDER = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICON_DRIVE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 14h.01M11 14h6"/></svg>';
const baseName = p => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;

function qrBlock() {
  return `<div class="qrwrap"><div class="qrimg"><img src="/qr.svg" alt="QR code for ${esc(INFO.urls.ip)}"></div>
    <div class="urls">
      <div class="url"><b>Scan with your phone camera</b><small>Your phone and this PC need to be on the same Wi-Fi.</small></div>
      <div class="url"><b>Or type this address</b><code>${esc(INFO.urls.ip || '')}</code><small>Works on any phone. It can change if the router gives the PC a new address.</small></div>
      <div class="url"><b>Address that never changes</b><code>${esc(INFO.urls.name || '')}</code><small>Works on iPhone and computers. On Android it depends on the browser.</small></div>
    </div></div>`;
}
const dhead = title => `<div class="dhead"><h3>${title}</h3><button class="dclose" data-close aria-label="Close">&#10005;</button></div>`;

function showDialog(kind) {
  dlgKind = kind;
  document.getElementById('dlg').hidden = false;
  if (kind === 'folders' && INFO.canManage && !browseData) loadBrowse('');
  renderDialog();
  syncOverflow();
}
function hideDialog() { dlgKind = null; rmConfirm = null; dlgErr = ''; document.getElementById('dlg').hidden = true; syncOverflow(); }

function renderDialog() {
  const p = document.getElementById('dpanel');
  if (!dlgKind) return;
  if (dlgKind === 'connect') { p.innerHTML = dhead('Connect your phone') + `<div class="dbody">${qrBlock()}</div>`; return; }
  const list = INFO.folders.map(f => `<div class="frow2">${ICON_FOLDER}<div class="fi"><b>${esc(f.name)}</b><small>${esc(f.path)}</small></div>
      <span class="cnt${f.exists ? '' : ' bad'}">${f.exists ? f.count + ' video' + (f.count === 1 ? '' : 's') : 'Not found'}</span>
      ${INFO.canManage ? `<button class="rm${rmConfirm === f.path ? ' confirm' : ''}" data-rm="${esc(f.path)}">${rmConfirm === f.path ? 'Remove?' : 'Remove'}</button>` : ''}
    </div>`).join('');
  let add;
  if (!INFO.canManage) {
    add = `<div class="sect">Add a folder</div><p style="color:var(--muted);font-size:14.5px;line-height:1.55;margin:0">
      Folders are added on the PC that runs Local Cinema, so nobody else on your Wi-Fi can look through its drives.
      Open <code>${esc(INFO.urls.name || '')}</code> on that PC.</p>`;
  } else {
    const b = browseData;
    const rows = !b ? '<div class="dempty">Loading…</div>'
      : b.error ? `<div class="dempty">${esc(b.error)}</div>`
      : (b.parent !== null && b.parent !== undefined ? `<button class="dir up" data-go="${esc(b.parent)}"><span style="width:18px;text-align:center">&#8593;</span>${b.parent ? 'Up to ' + esc(baseName(b.parent)) : 'All drives'}</button>` : '')
        + (b.dirs.length ? b.dirs.map(d => `<button class="dir" data-go="${esc(d.path)}">${d.kind === 'drive' ? ICON_DRIVE : ICON_FOLDER}${esc(d.name)}${d.kind === 'place' ? '<span class="k">shortcut</span>' : ''}</button>`).join('')
          : '<div class="dempty">No folders inside this one.</div>');
    const canAdd = b && !b.error && b.path;
    add = `<div class="sect">Add a folder</div>
      <div class="pathbar"><input id="pathIn" value="${esc(b && b.path || '')}" placeholder="Pick a drive below, or paste a path" spellcheck="false" autocomplete="off"><button data-goin>Go</button></div>
      <div class="dirs">${rows}</div>
      <div class="addbar"><div class="note">${canAdd
          ? (b.videos ? `${b.videos} video${b.videos > 1 ? 's' : ''} directly in this folder. Videos in its subfolders are included too.` : 'Videos in this folder and all its subfolders will be added.')
          : 'Open the folder that holds your movies, then add it.'}</div>
        <button class="btn primary" data-addf ${canAdd ? '' : 'disabled'}>&#65291; Add ${canAdd ? '&ldquo;' + esc(baseName(b.path)) + '&rdquo;' : 'this folder'}</button></div>
      ${dlgErr ? `<div class="err">${esc(dlgErr)}</div>` : ''}`;
  }
  p.innerHTML = dhead('Library folders') + `<div class="dbody">
    ${INFO.folders.length ? `<div class="sect">Your folders</div><div class="fl">${list}</div>` : ''}${add}</div>`;
}

async function loadBrowse(path) {
  browseData = null; dlgErr = ''; renderDialog();
  try { browseData = await (await fetch('/api/browse?path=' + encodeURIComponent(path), {cache: 'no-store'})).json(); }
  catch { browseData = {error: 'Could not reach Local Cinema.'}; }
  renderDialog();
}
async function changeFolders(body) {
  dlgErr = '';
  let r, d;
  try { r = await fetch('/api/folders', {method: 'POST', body: JSON.stringify(body)}); d = await r.json(); }
  catch { d = {error: 'Could not reach Local Cinema.'}; }
  if (!r || !r.ok) { dlgErr = d.error || 'That didn\'t work.'; renderDialog(); return false; }
  await load(true);
  return true;
}

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
  const hadFolders = INFO.folders.length;
  INFO = {folders: d.folders || [], canManage: !!d.canManage, urls: d.urls || {}};
  if (!full && !hadFolders !== !INFO.folders.length) full = true;  // first folder added or last removed
  toast(d.pending, d.ffmpeg);
  if (dlgKind) renderDialog();
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
  const t = e.target.closest('[data-open],[data-play],[data-start],[data-close],[data-back],[data-act],[data-dir],[data-folder],[data-dlg],[data-go],[data-goin],[data-addf],[data-rm]');
  if (!t) return;
  if (t.dataset.dlg) return dlgKind ? showDialog(t.dataset.dlg) : openDialog(t.dataset.dlg);
  if (t.dataset.go !== undefined) return loadBrowse(t.dataset.go);
  if (t.dataset.goin !== undefined) return loadBrowse(document.getElementById('pathIn').value.trim());
  if (t.dataset.addf !== undefined) {
    t.disabled = true; t.textContent = 'Adding…';
    return changeFolders({add: browseData.path}).then(ok => { if (ok) { browseData = null; goBack(); } });
  }
  if (t.dataset.rm) {
    if (rmConfirm !== t.dataset.rm) { rmConfirm = t.dataset.rm; return renderDialog(); }
    rmConfirm = null;
    return changeFolders({remove: t.dataset.rm});
  }
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
  if (e.key === 'Enter' && e.target.id === 'pathIn') return loadBrowse(e.target.value.trim());
  if (e.key === 'Escape' && (!document.getElementById('sheet').hidden || openFolderKey || dlgKind)) goBack();
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
    load_folders()
    url = f"http://{lan_ip()}:{PORT}"
    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError:
        log(f"Port {PORT} is busy - Local Cinema is probably already running at {url}")
        return
    if not (FFMPEG and FFPROBE):
        log("ffmpeg/ffprobe not found on PATH - thumbnails and resume times are disabled.")
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=saver, daemon=True).start()
    threading.Thread(target=mdns_responder, daemon=True).start()
    scan()
    # drop thumbnails of videos that are gone, but only when every folder is reachable,
    # so an unplugged drive doesn't wipe its thumbnails
    if library and all(os.path.isdir(f) for f in folders):
        for f in os.listdir(THUMB_DIR):
            if f.endswith(".jpg") and f[:-4] not in library:
                try:
                    os.remove(os.path.join(THUMB_DIR, f))
                except OSError:
                    pass
    log(f"Serving {len(folders)} folder(s), {len(library)} videos" if folders
        else "No folders yet - open the page on this PC and click Add folder")
    log(f"Open on your phone:  http://{MDNS_NAME}:{PORT}  or  {url}")
    try:
        if sys.stdout and sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8")
            print("\nScan to open on your phone:\n" + qr_text(url) + "\n")
    except (UnicodeError, OSError, AttributeError, ValueError):
        pass
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
