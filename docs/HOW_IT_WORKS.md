# How Local Cinema works

A walkthrough of `movie_server.py`: what runs, what talks to what, and how streaming and resume work. The two [explainer videos](videos/) cover the same ground visually.

## The pieces

```
 Phone                          PC: movie_server.py
┌──────────────┐   Wi-Fi   ┌────────────────────────────────────────────┐
│ Cinema page  │◄────────►│ HTTP handler ──reads bytes──► movie folder │
│ (browser)    │  page,    │      │                         ▲           │
│      │ Play  │  JSON,    │      │ note_progress()   worker thread    │
│      ▼       │  thumbs   │      ▼                   (ffprobe/ffmpeg)  │
│ VLC          │◄═══════►│ state (memory) ──► saver thread ──► library.json
└──────────────┘  video    └────────────────────────────────────────────┘
                  bytes
```

- **HTTP handler.** Python's built-in `ThreadingHTTPServer` gives every connection its own thread, so loading thumbnails and streaming a movie happen side by side.
- **Worker thread.** Runs ffprobe/ffmpeg one video at a time to read running times and make thumbnails. Requests never wait on it.
- **Saver thread.** Copies progress from memory to `library.json` every 5 seconds, but only when something changed.

The page only fetches small things (the HTML, a JSON list, thumbnails). The movie itself goes straight from disk, through the handler, to VLC.

## Starting up: `main()`

1. Create `movie_server_data/thumbs` and load `library.json`.
2. Start the worker and saver threads.
3. `scan()` the movie folder and queue anything missing a thumbnail or running time.
4. Delete thumbnails for videos that no longer exist. This is skipped if no videos were found, so an unplugged drive doesn't wipe them.
5. Bind the port **exclusively** (`SO_EXCLUSIVEADDRUSE` on Windows). Windows otherwise lets two programs share a port, so without this a second copy would run alongside the first.
6. `serve_forever()`.

`start_hidden.vbs` launches `python.exe` with a hidden window instead of using `pythonw.exe`. Windows Firewall rules are per program, and a rule for `python.exe` doesn't cover `pythonw.exe`.

## Requests

| Request | Sent by | What happens |
|---|---|---|
| `GET /` | Browser | Returns the whole app. The HTML, CSS and JS live in the `PAGE` string. |
| `GET /api/library` | The page | Rescans the folder; returns every video with title, running time, progress and thumbnail version, plus how many thumbnails are still being made. |
| `GET /thumb/{id}.jpg` | Image tags | The thumbnail, cached for a year. The page adds `?v=N`, so a new thumbnail gets a new URL. |
| `GET /v/{id}/{name}` | VLC | Streams the video, honouring `Range`. The filename is only there so VLC shows a readable name. |
| `POST /api/movie/{id}/played` | The page, on Play | Records the time; orders the *Continue watching* row. |
| `POST …/watched`, `…/unwatched` | Details panel | Sets the watched flag and clears the saved position. |
| `POST …/thumb` | *New thumbnail* | Queues a forced re-grab. |

## Streaming without downloading: `Handler.stream()`

A movie file is a long row of bytes. HTTP lets a client ask for part of a file with the `Range` header, and the server answers `206 Partial Content` with only those bytes:

```
GET /v/0f9cd27b3e1e/Berlin.mkv HTTP/1.1
Range: bytes=455740000-

HTTP/1.1 206 Partial Content
Content-Range: bytes 455740000-1067960219/1067960220
Accept-Ranges: bytes
```

When VLC opens a movie it makes three kinds of reads:

1. **Header**: the first few hundred KB, which describe the tracks. Playback can start after this.
2. **Index**: MKV files keep a table near the end (the *Cues*) that maps times to byte positions. Seeking uses it.
3. **Playback**: a range starting wherever you want to watch.

The handler seeks to the start byte and loops: read 512 KB, write it to the socket, repeat. The connection sets the pace. When VLC's buffer is full it stops reading, the socket buffer fills, and the server's write blocks. So the server is only ever slightly ahead of what's on screen.

## Remembering your spot: `note_progress()` and `movie_json()`

VLC never reports the playback time, so the server estimates it from the one thing it knows: how far into the file it has sent.

- A request has to deliver **at least 8 MB** before it counts. This filters out header reads.
- Reads that **start past 97%** of the file are ignored. That's VLC reading the index.
- While a counted read is running, the byte offset is saved **every 3 seconds**, and once more when it ends.
- Reaching **94%** marks the movie as watched and clears the position.

The time is worked out when the page asks for the library:

```
position = offset ÷ file size × running time − 20 s
```

The 20 seconds covers what VLC had buffered but not yet shown. Anything under one minute counts as not started.

Example: 458,641,368 ÷ 1,067,960,220 = 0.4295, and 0.4295 × 1:59:54 = 51:30. Step back 20 s and the movie resumes at **51:10**.

The estimate assumes bytes are spread evenly over time. That's close enough for typical encodes, and the 20-second rewind absorbs the difference.

## Handing off to VLC: `playHref()`

On Android, *Play* builds an intent link that opens the stream directly in VLC with a start position in milliseconds:

```
intent://192.168.x.x:8000/v/{id}/{name}#Intent;scheme=http;package=org.videolan.vlc;type=video/*;S.title=Berlin;l.position=3070000;end
```

`S.` marks a string extra and `l.` a long. *Start over* sends `l.position=0` and `B.from_start=true`.

On iPhone the page uses `vlc-x-callback://x-callback-url/stream?url=…`. It has no start-position option, so VLC starts at the beginning and the details panel shows the time to skip to. On a desktop browser the stream URL opens in a new tab.

Before handing off, the page sends `navigator.sendBeacon()` to `/played`. Beacons still get delivered when the browser is moved to the background a moment later.

## Thumbnails and running times: `worker()`

`scan()` queues IDs; one worker thread handles them in order:

1. **Probe:** `ffprobe` reads running time, video height, and colour transfer (which reveals HDR).
2. **Pick a moment:** a random point between 10% and 70% of the running time, avoiding titles and credits.
3. **Grab:** ffmpeg seeks there, scales to 960 px wide, and its `thumbnail` filter keeps the most typical of the next 40 frames. That avoids black frames.
4. **HDR:** a tone-mapping filter runs first so HDR frames aren't washed out. It falls back to the plain command if that fails.
5. **Save:** written to a temp file, then renamed into place, so a half-written JPG is never served.

```
ffmpeg -ss {at} -i movie.mkv -an -sn -vf "scale=960:-2,thumbnail=40" -frames:v 1 -q:v 4 thumbs/{id}.jpg
```

Files ffmpeg can't read go in a `failed` set and aren't retried until restart. While the queue isn't empty, the page shows *Making thumbnails… N left* and polls every 4 seconds.

## Filenames to titles: `parse_name()`

1. Drop junk after a stray extension (`movie.mkv - copy.mkv`).
2. Take a leading `1.` or `2.` off as the order number.
3. If the name uses dots for spaces, turn them into spaces.
4. Find where the title ends: an episode code (`S01E03`), else a year from 1930 to 2049, else the first quality word or bracket.
5. Clean up: remove bracket groups, join spaced capitals (`K G F` → `KGF`), trim punctuation.
6. Read the rest for quality (`2160p/4K`, `1080p`, `720p`), HDR, and languages (`Hindi`, `HIN`, `Tamil`, `TAM`, …).

| Filename | Becomes |
|---|---|
| `Some.Movie.2023.1080p.WEB-DL.mkv` | **Some Movie** · 2023 · 1080p |
| `Another Film (2024) 720p [Hindi + English].mkv` | **Another Film** · 2024 · 720p · Hindi, English |
| `3. The.Third.Part.2004.1080p.mkv` | **The Third Part** · 2004 (order 3) |
| `Show S01E05 720p.mkv` | **Show S01E05**, shown as *Episode 5* inside its folder |

Folder names are tidied too: `Harry Potter (8)` → **Harry Potter**, `KINGSMAN(2)` → **Kingsman**.

## IDs and saved data: `scan()` and `save_state()`

Each video's ID is the first 12 hex characters of `sha1("{file name}|{size in bytes}")`. The folder isn't part of it, so a file moved between folders keeps its progress and thumbnail. `migrate_id()` carries over entries saved under the older path-based IDs.

One entry in `library.json`:

```json
"0f9cd27b3e1e": {
  "duration": 7194.312,
  "hdr": false,
  "height": 536,
  "thumb": 1,
  "pos_bytes": 0,
  "watched": false,
  "last_played": 0
}
```

Changes only set a `dirty` flag; the saver thread writes at most every 5 seconds. It writes `library.json.tmp` and then renames it over the real file, so a crash mid-write can't corrupt it. A lock keeps request threads, the worker and the saver from editing the data at the same time. Titles, folders and sizes aren't saved. They're rebuilt from disk on every scan.

## The page: `load()` and `render()`

Plain JavaScript, no framework:

- `load()` fetches `/api/library` and `render()` draws it.
- **Hero:** your most recent unfinished movie, else a random unwatched one.
- **Rows:** *Continue watching*, *Recently added* (14 newest), then one row per top-level folder. `groupList()` collapses videos that share a sub-folder into one folder card.
- **Back button:** opening a folder or the details panel adds a browser history entry, so the phone's Back gesture closes it instead of leaving the site.
- **Returning from VLC:** the page reloads the list when the tab becomes visible again (`visibilitychange`), so progress is current.
- **Desktop:** rows scroll with arrow buttons, click-and-drag, or a thin scrollbar.
