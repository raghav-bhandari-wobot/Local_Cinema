# Local Cinema

Local Cinema is a lightweight Python server that streams a PC's movie folder to your phone over home Wi-Fi. Movies play instantly in VLC through HTTP range requests with no downloads, resume where you stopped, and show auto-generated thumbnails. The server can start silently when Windows boots.

It is one Python file with no third-party packages. ffmpeg is optional and only used for thumbnails and running times.

## Features

- **Instant streaming, no downloads.** VLC requests the file in byte ranges, so playback starts right away and seeking works anywhere in the movie.
- **Resume where you stopped.** The server works out your position from how far into the file VLC has read. On Android, VLC opens straight at that spot.
- **A library that looks like an app.** A hero banner, a *Continue watching* row, *Recently added*, and one row per folder. Sub-folders (a TV season, a film series) collapse into a single folder card with its own page.
- **Automatic thumbnails.** A random frame from each movie, taken with ffmpeg. Tap *New thumbnail* for a different one.
- **Clean titles.** Release-style filenames are turned into readable titles, with year, quality (720p/1080p/4K/HDR) and audio languages.
- **Watched tracking.** Progress bars, a check mark when you pass 94%, and manual *Mark watched / unwatched*.
- **Runs quietly.** Optional hidden start at Windows login, single-instance guard, and a log file.

## Explainer videos

| | |
|---|---|
| [1 · How it fits together](docs/videos/1-how-it-fits-together.mp4) | The phone, the server, VLC and the video path (1:29) |
| [2 · Streaming and resume](docs/videos/2-streaming-and-resume.mp4) | Byte ranges, buffering, and how the resume time is worked out (1:51) |

For a written walkthrough of the code, see [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Requirements

- **Python 3.8+** on the computer that holds the movies (standard library only).
- **ffmpeg and ffprobe** on the PATH. These are optional: without them, everything works except thumbnails and running times.
- **VLC** on the phone ([Android](https://play.google.com/store/apps/details?id=org.videolan.vlc) / [iPhone](https://apps.apple.com/app/vlc-media-player/id650377962)). Phone browsers can't play most `.mkv` files, so the page hands each movie to VLC.
- The phone and the computer on the **same Wi-Fi**.

The server itself runs on any OS. The start/stop helper scripts are for Windows.

## Quick start

```bash
python movie_server.py "D:\Movies" 8000
```

The folder and port are optional (defaults: `E:\Movies` and `8000`). The server prints the address to open:

```
Serving D:\Movies  (34 videos)
Open on your phone:  http://192.168.x.x:8000
```

Open that address on your phone. Tap a movie, then **Play** or **Resume**.

### If your phone can't connect

- **Windows Firewall:** allow `python.exe` for the network type you're on. On first run Windows usually asks; tick the box for your network (Private or Public).
- **Same network:** guest Wi-Fi or "AP isolation" on some routers blocks devices from seeing each other.
- **Address changed:** the PC's local IP can change after a restart. Reserve it in your router (DHCP reservation) or set a static IP so the bookmark keeps working.

## Start automatically with Windows

1. Open `start_hidden.vbs` and set `moviesFolder`, `port` and, if needed, the path to `python`.
2. Press **Win+R**, type `shell:startup`, and put a shortcut to `start_hidden.vbs` in that folder.

The server then starts hidden at every login. Use `stop_server.bat` to stop it. Starting it twice is safe: the second copy notices the port is taken, logs a note and exits.

## Organising your movies

```
Movies\
├── Some Movie (2024) 1080p.mkv          → row "Movies"
├── Harry Potter (8)\                    → row "Harry Potter"
│   ├── 1. Harry.Potter.and.the….mkv
│   └── Fantastic Beasts\                → one folder card inside that row
└── Series\
    └── Show Season 1\                   → folder card listing "Episode 1, 2, …"
        ├── Show S01E01 720p.mkv
        └── Show S01E02 720p.mkv
```

- Each top-level folder becomes a row. Loose files go in a row called **Movies**.
- A folder one level deeper becomes a folder card. Its page has a button that plays the episode you're on, or the first one you haven't watched.
- New files appear the next time the page loads. Moving a file to another folder keeps its progress and thumbnail.

## Your data

Everything the server learns is stored next to the script in `movie_server_data/`. This folder is in `.gitignore`.

| File | Contents |
|---|---|
| `library.json` | Running times, resume positions, watched flags |
| `thumbs/` | One JPG per video |
| `server.log` | Start-up messages and errors |

Your movie folder is only read, never written to.

## Security note

There is no login. Anyone on the same network who knows the address can browse and stream your library. Run it on a network you trust, such as your home Wi-Fi, and don't expose the port to the internet.

## Project layout

```
movie_server.py        the server, plus the web page it serves (HTML/CSS/JS in one string)
start_hidden.vbs       starts the server with no window (Windows)
stop_server.bat        stops the background server (Windows)
docs/HOW_IT_WORKS.md   how the code works
docs/videos/           explainer videos
```
