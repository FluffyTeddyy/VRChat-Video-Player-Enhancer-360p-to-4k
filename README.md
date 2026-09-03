# VRChat Video Player Enhancer (From 360p up to 4k)

VRChat Video Player Enhancer is a local HTTP service that turns a non-livestream
YouTube video into a single HLS playlist. It asks `yt-dlp` for separate H.264
video and AAC audio streams, remuxes them with FFmpeg without re-encoding, and
serves the result from `127.0.0.1` for an HLS-capable client.

By default, the service selects the best available H.264 video at up to 1080p
and 30 FPS. This avoids high-bitrate 60 FPS streams that may not download fast
enough for uninterrupted playback. Both limits are configurable.

The service is local-only and has no authentication. Do not expose its port to
your network or the internet.

## Requirements

The service has no third-party Python package dependencies. Install these
runtime requirements instead:

- Python 3.10 or newer
- A recent `yt-dlp` executable
- FFmpeg with HLS and MPEG-TS support
- Optional Netscape-format YouTube cookies file (recommended for signed-in or
  restricted videos)
- Internet access for YouTube and the resolved media streams

`curl` is useful for the HTTP examples below but is not required by the
service. An HLS-capable player or client is also required to consume the
playlist.

Only non-live, non-upcoming videos are supported. The selected formats must
contain H.264/AVC video and AAC audio; videos that do not offer that pair cannot
be resolved by this service.

## Install on Linux

The commands below use Debian/Ubuntu paths. Other distributions can use their
own package manager, but the executable paths passed to the service must match
your installation.

1. Install Python, FFmpeg, and `curl`:

   ```bash
   sudo apt update
   sudo apt install python3 ffmpeg curl git
   ```

   Verify that `python3 --version` reports 3.10 or newer.

2. Get the source:

   ```bash
   git clone https://github.com/FluffyTeddyy/VRChat-Video-Player-Enhancer-360p-to-4k.git
   cd VRChat-Video-Player-Enhancer-360p-to-4k
   ```

3. Install the `yt-dlp` standalone executable at the default path:

   ```bash
   install -d ~/.local/bin
   curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
     -o ~/.local/bin/yt-dlp
   chmod +x ~/.local/bin/yt-dlp
   ```

   If you keep it somewhere else, set `yt_dlp` in `config.ini`.

4. Cookies are optional. If you need signed-in or restricted videos, export
   YouTube cookies as described in [Cookie setup](#cookie-setup). The Linux
   default location is `youtube_cookies.txt` beside `main.py`.

5. Start the service from the repository directory:

   ```bash
   python3 main.py
   ```

   If you use fish, a convenience wrapper is also included:

   ```fish
   fish run.fish
   ```

   The wrapper uses `/usr/bin/python3`; use `python3 main.py` directly if your
   active Python 3.10+ interpreter is installed elsewhere.

## Install on Windows

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/windows/).
   Enable the option to add Python to `PATH`, then verify in PowerShell:

   ```powershell
   py --version
   ```

2. Download `yt-dlp.exe` from the
   [yt-dlp releases](https://github.com/yt-dlp/yt-dlp/releases/latest) page.
   Save it somewhere such as `C:\Tools\yt-dlp\yt-dlp.exe`.

3. Install an FFmpeg Windows build from the
   [FFmpeg download page](https://ffmpeg.org/download.html#build-windows), and
   note the full path to `ffmpeg.exe`, for example
   `C:\ffmpeg\bin\ffmpeg.exe`.

4. Get the source with Git or download the repository archive, then open
   PowerShell in the repository directory. `run.fish` is a Unix/fish helper and
   is not used on Windows.

5. Cookies are optional. For signed-in or restricted videos, export cookies as
   described in [Cookie setup](#cookie-setup), saving the file as
   `youtube_cookies.txt` beside `main.py`.

6. Open `config.ini` and set the Windows paths for `yt_dlp` and `ffmpeg`:

   ```ini
   yt_dlp = C:\Tools\yt-dlp\yt-dlp.exe
   ffmpeg = C:\ffmpeg\bin\ffmpeg.exe
   ```

7. Start the service:

   ```powershell
   py -3 .\main.py
   ```

   The cache is created as `cache\` beside `main.py` unless you set
   `cache_dir` in `config.ini`.

## Cookie setup

The service does not read a browser profile directly. Cookies are optional and
are only needed for signed-in or otherwise restricted YouTube videos. When
configured, the file must be in Netscape format.
The service first resolves videos without cookies because authenticated
YouTube player URLs can be rejected by the media server even when extraction
succeeds. If that public resolve fails and a cookie file is configured,
`yt-dlp` retries with a temporary copy of it; the source file is not modified.

Close the browser completely before exporting. A browser database that is still
open can prevent `yt-dlp` from reading it.

### Export with yt-dlp on Linux

Replace `chrome` with your browser (`firefox`, `chromium`, `edge`, `brave`,
`opera`, `vivaldi`, or another browser supported by your `yt-dlp` build). Use a
YouTube URL that you can open while signed in; `--skip-download` prevents the
export command from downloading the media.

```bash
cd /path/to/VRChat-Video-Player-Enhancer-360p-to-4k
~/.local/bin/yt-dlp \
  --cookies-from-browser chrome \
  --cookies youtube_cookies.txt \
  --skip-download --no-playlist \
  'https://www.youtube.com/watch?v=VIDEO_ID'
```

For a non-default browser profile, use the profile syntax shown by
`yt-dlp --help` (for example, `chrome:Profile 1`).

### Export with yt-dlp on Windows

Run this in PowerShell after closing the browser:

```powershell
Set-Location 'C:\path\to\VRChat-Video-Player-Enhancer-360p-to-4k'
& 'C:\Tools\yt-dlp\yt-dlp.exe' `
  --cookies-from-browser chrome `
  --cookies .\youtube_cookies.txt `
  --skip-download --no-playlist `
  'https://www.youtube.com/watch?v=VIDEO_ID'
```

If browser extraction is unavailable, use a reputable browser extension that
exports cookies in Netscape `cookies.txt` format. Export the cookies for
`youtube.com` while signed in and save the resulting file at the path supplied
to `--cookies`. Do not use a JSON export: the service and `yt-dlp --cookies`
expect Netscape format.

Cookies grant access to your account. Keep the file private, outside the Git
working tree when possible, and never commit or upload it. Re-export the file if
YouTube reports that it is expired or invalid.

## Start and use the service

The default listener is `http://127.0.0.1:9696`. Keep the terminal running while
the service is in use. Press `Ctrl+C` to stop it.

Resolve a YouTube URL or 11-character video ID from Linux or Windows (Windows
10 and later include `curl.exe`):

```bash
curl --get \
  --data-urlencode 'url=https://www.youtube.com/watch?v=VIDEO_ID' \
  http://127.0.0.1:9696/resolve
```

The response includes `playlist_url`, `status_url`, and a job `status`. With the
default `--startup-wait 10`, a new job waits briefly for the first playlist and
returns HTTP 202 only if it is still starting. Poll the status URL until the
status is `running` or `complete`:

```bash
curl http://127.0.0.1:9696/status/VIDEO_ID
```

Resolution or remux failures return HTTP 502 with an error message in the JSON
response.

Then give the returned `playlist_url` to an HLS-capable client, for example:

```text
http://127.0.0.1:9696/hls/VIDEO_ID/stream.m3u8
```

You can make `/resolve` wait briefly for the first playlist:

```bash
python3 main.py --startup-wait 10
```

On Windows, the equivalent startup command is `py -3 .\main.py` with the
`--yt-dlp` and `--ffmpeg` options shown above. Add `--cookies` if you have
exported a cookie file. A PowerShell request
can use the built-in HTTP client:

```powershell
Invoke-RestMethod 'http://127.0.0.1:9696/resolve?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DVIDEO_ID'
```

## Automatic VRChat interception

The repository includes a small Windows PE `yt-dlp` stub. Build it on Linux
with the installed .NET SDK:

```bash
dotnet publish yt-dlp-stub/yt-dlp-stub.csproj \
  -r win-x64 -c Release -o yt-dlp-stub/publish
```

Start the service normally. If the project-owned stub has not been built yet,
the service builds it automatically with `dotnet publish`, then patches
VRChat's bundled executable:

```bash
python3 main.py
```

On Linux the target is discovered by reading Steam's
`steamapps/libraryfolders.vdf`, locating AppID `438100`, and deriving the
Proton `LocalLow/VRChat/VRChat/Tools/yt-dlp.exe` path. On Windows it is derived
from `%LOCALAPPDATA%`. Use `--vrchat-yt-dlp`, `--stub`, or `--backup` to
override the discovered paths. The original is retained as `yt-dlp.exe.bkp`.
When the service exits (including `Ctrl+C`), it restores the original
executable automatically. The explicit commands remain available for recovery
or inspection:

```bash
python3 main.py --patch
python3 main.py --restore
```

Patching changes the VRChat installation. Do not run this at the same time as
another tool that patches the same `yt-dlp.exe`; competing backups and restores
can interfere with one another. Normal server startup
keeps the patch active only for the service lifetime and reapplies it if VRChat
refreshes its bundled executable during startup. The stub has a strict
YouTube-only gate: plain YouTube URLs are sent to `/api/getvideo`; every other
request is delegated directly to VRChat's original `yt-dlp.exe.bkp` with its
arguments unchanged. Non-YouTube requests therefore do not contact this
service, run redirect probes, copy cookies, or receive format changes. Direct
`.m3u8` URLs are part of that untouched normal path.

The playlist is published as an HLS event while FFmpeg is running and ends with
`#EXT-X-ENDLIST` when complete. FFmpeg atomically rewrites the same playlist as
new segments arrive; this is normal HLS polling behavior, not a new video for
each segment. The local HLS endpoint waits briefly for the initial playlist so
clients do not see a transient missing-file response. Completed videos are
reused from the cache while the service is running; stale partial output is
replaced on the next request. The cache is cleared when the service exits. An
active download is cancelled after 30 seconds without a playlist or segment
request, which prevents videos that are no longer playing from continuing to
download in the background.

## Configuration

The service automatically loads `[settings]` from `config.ini` beside
`main.py`. Edit that file for normal use. Relative paths in it are resolved
from the configuration file's directory, not the current terminal directory.
Empty optional path values retain automatic discovery or built-in defaults.

Use `--config /path/to/another.ini` to load another file. Explicit command-line
arguments override the file. The complete precedence order is command line,
environment variables, configuration file, then built-in defaults.

All command-line overrides are available with `python3 main.py --help` on
Linux or `py -3 .\main.py --help` on Windows. INI setting names use underscores
instead of the option's leading dashes, as shown in the included file.

| Option | Default | Purpose |
| --- | --- | --- |
| `--config` | `./config.ini` | Load settings from another INI file |
| `--port` | `9696` | Loopback HTTP port |
| `--max-height` | `1080` | Maximum selected video height |
| `--max-fps` | `30` | Maximum selected video frame rate |
| `--segment-seconds` | `6` | Steady-state HLS segment duration; the initial segment targets 1 second |
| `--startup-wait` | `10` | Seconds to wait for the first playlist per resolve request; use `0` to return immediately |
| `--cache-max-size` (`--cache-limit`) | `10G` | Maximum cache size; accepts values such as `200M` or `10G` |
| `--cache-grace-minutes` | `10` | Keep recently accessed completed entries protected |
| `--cache-cleanup-interval` | `300` | Periodic cleanup interval in seconds; `0` disables it |
| `--job-idle-timeout` | `30` | Cancel active downloads after this many seconds without HLS requests; `0` disables it |
| `--cache-dir` | `./cache` | HLS output and cache metadata directory |
| `--yt-dlp` | Linux: `~/.local/bin/yt-dlp` | Path to the `yt-dlp` executable |
| `--ffmpeg` | Linux: `/usr/bin/ffmpeg` | Path to the FFmpeg executable |
| `--cookies` | `./youtube_cookies.txt` | Optional Netscape-format cookie file; omitted when absent |
| `--vrchat-yt-dlp` | Automatic | Override discovery of VRChat's bundled `yt-dlp.exe` |
| `--stub` | `./yt-dlp-stub/publish/yt-dlp-stub.exe` | Interception stub executable |
| `--backup` | Beside VRChat's `yt-dlp.exe` | Override the interception backup path |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

The cache size, grace period, cleanup interval, and active-job timeout can also
be set with
`VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_MAX_SIZE`,
`VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_GRACE_MINUTES`,
`VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_CLEANUP_INTERVAL`, and
`VRCHAT_VIDEO_PLAYER_ENHANCER_JOB_IDLE_TIMEOUT` respectively. Inspect current
cache usage during a run at:

```text
http://127.0.0.1:9696/cache/status
```

The service always binds to loopback and is not designed for shared or remote
use.

## Troubleshooting

- **Startup says an executable is missing:** pass the actual paths with
  `--yt-dlp` and `--ffmpeg`. The service validates both files before listening.
- **A restricted video cannot be resolved:** export a Netscape-format cookie
  file and pass it with `--cookies`. Public videos can run without cookies.
- **Cookie extraction fails:** close every browser window, check the browser
  profile, and re-run the export command. Chromium-based browsers may require
  the keyring/profile options shown by `yt-dlp --help`.
- **A video cannot be resolved:** live/upcoming videos are unsupported, and the
  video must provide separate H.264 video and AAC audio formats.
- **The player cannot open the URL immediately:** wait for the job status to
  become `running` or `complete`, then open the `playlist_url` returned by
  `/resolve`.
