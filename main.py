#!/usr/bin/env python3
"""Local YouTube-to-HLS remux proxy."""

from __future__ import annotations

import argparse
import json
import logging
import ntpath
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse


LOGGER = logging.getLogger("vrchat_video_player_enhancer")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ACTIVE_STATES = {"resolving", "starting", "running"}
DEFAULT_YTDLP = Path("~/.local/bin/yt-dlp").expanduser()
DEFAULT_STUB = Path(__file__).parent / "yt-dlp-stub" / "publish" / "yt-dlp-stub.exe"
DEFAULT_FFMPEG = Path("/usr/bin/ffmpeg")
DEFAULT_COOKIES = Path(__file__).parent / "youtube_cookies.txt"
DEFAULT_CACHE_LIMIT_BYTES = 200 * 1024**2
DEFAULT_CACHE_GRACE_SECONDS = 10 * 60
DEFAULT_CACHE_CLEANUP_INTERVAL = 5 * 60
DEFAULT_JOB_IDLE_TIMEOUT_SECONDS = 30.0
DEFAULT_JOB_MONITOR_INTERVAL = 1.0
HLS_PLAYLIST_WAIT_SECONDS = 10.0
STATE_FILENAME = ".cache_state.json"
VRCHAT_APP_ID = "438100"


class ResolveError(RuntimeError):
    """An expected input or media-resolution failure."""


class JobCancelled(RuntimeError):
    """A remux job was stopped because no HLS client was still using it."""


class ProcessLike(Protocol):
    stderr: Any
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class DirectStreams:
    video_id: str
    title: str
    video_url: str
    audio_url: str
    video_format: str
    audio_format: str
    video_headers: dict[str, str] = field(default_factory=dict)
    audio_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Job:
    video_id: str
    playlist_path: Path
    state: str = "resolving"
    title: str | None = None
    video_format: str | None = None
    audio_format: str | None = None
    error: str | None = None
    process: ProcessLike | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    last_hls_access: float = field(default_factory=time.monotonic)
    cancel_requested: bool = False
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock()), repr=False
    )

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "video_id": self.video_id,
                "status": self.state,
                "title": self.title,
                "video_format": self.video_format,
                "audio_format": self.audio_format,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


def normalize_video_id(reference: str) -> str:
    reference = reference.strip()
    if VIDEO_ID_RE.fullmatch(reference):
        return reference

    try:
        parsed = urlparse(reference)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"}:
            raise ResolveError("Expected a YouTube URL or an 11-character video ID")

        video_id: str | None = None
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.lstrip("/").split("/", 1)[0]
        elif host == "youtube.com" or host.endswith(".youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]
            else:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
                    video_id = parts[1]

        if video_id and VIDEO_ID_RE.fullmatch(video_id):
            return video_id
    except ValueError as exc:
        raise ResolveError("Malformed YouTube URL") from exc

    raise ResolveError("Expected a YouTube URL or an 11-character video ID")


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"


def is_youtube_url(reference: str) -> bool:
    try:
        parsed = urlparse(reference.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return host in {"youtu.be", "www.youtu.be", "youtube.com"} or host.endswith(
        ".youtube.com"
    )


def _parse_vdf(text: str) -> dict[str, Any]:
    """Parse the quoted strings and objects used by Steam's library VDF."""
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text)
    flattened = [
        (value.replace(r'\\"', '"').replace(r'\\\\', '\\') if value else brace)
        for value, brace in tokens
    ]
    position = 0

    def parse_object(expect_close: bool = False) -> dict[str, Any]:
        nonlocal position
        result: dict[str, Any] = {}
        while position < len(flattened):
            token = flattened[position]
            if token == "}":
                if not expect_close:
                    raise ResolveError("Unexpected closing brace in libraryfolders.vdf")
                position += 1
                return result
            if token == "{":
                raise ResolveError("Unexpected opening brace in libraryfolders.vdf")
            key = token
            position += 1
            if position >= len(flattened):
                raise ResolveError("Missing value in libraryfolders.vdf")
            value = flattened[position]
            position += 1
            if value == "{":
                result[key] = parse_object(True)
            elif value == "}":
                raise ResolveError("Missing value before closing brace in libraryfolders.vdf")
            else:
                result[key] = value
        if expect_close:
            raise ResolveError("Unclosed object in libraryfolders.vdf")
        return result

    return parse_object()


def parse_steam_libraryfolders(text: str, app_id: str = VRCHAT_APP_ID) -> list[Path]:
    """Return library roots whose VDF app list contains ``app_id``."""
    root = _parse_vdf(text)
    libraryfolders = root.get("libraryfolders", root)
    if not isinstance(libraryfolders, dict):
        return []
    matches: list[Path] = []
    for value in libraryfolders.values():
        if not isinstance(value, dict):
            continue
        path = value.get("path")
        apps = value.get("apps")
        if isinstance(path, str) and isinstance(apps, dict) and app_id in apps:
            matches.append(Path(path))
    return matches


def discover_vrchat_ytdlp_path(
    *,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> Path:
    """Find VRChat's bundled yt-dlp on Windows or in a Steam/Proton prefix."""
    environment = os.environ if environment is None else environment
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name.startswith("win"):
        local_app_data = environment.get("LOCALAPPDATA")
        if not local_app_data:
            raise ResolveError("LOCALAPPDATA is not set; cannot locate VRChat Tools")
        if os.name == "nt":
            return (
                Path(local_app_data).parent
                / "LocalLow"
                / "VRChat"
                / "VRChat"
                / "Tools"
                / "yt-dlp.exe"
            )
        # Keep Windows separators when this branch is unit-tested on Linux.
        return Path(ntpath.join(ntpath.dirname(local_app_data), "LocalLow", "VRChat", "VRChat", "Tools", "yt-dlp.exe"))

    home = Path.home() if home is None else home
    steam_roots = (
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
        home / ".steam/steam",
        home / ".local/share/Steam",
    )
    checked: list[Path] = []
    candidates: list[Path] = []
    for steam_root in steam_roots:
        vdf_path = steam_root / "steamapps/libraryfolders.vdf"
        checked.append(vdf_path)
        LOGGER.info("Checking Steam libraryfolders.vdf at %s", vdf_path)
        try:
            text = vdf_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ResolveError(f"Could not read {vdf_path}: {exc}") from exc
        try:
            library_roots = parse_steam_libraryfolders(text)
        except ResolveError as exc:
            raise ResolveError(f"Could not parse {vdf_path}: {exc}") from exc
        for library_root in library_roots:
            candidate = (
                library_root
                / "steamapps/compatdata"
                / VRCHAT_APP_ID
                / "pfx/drive_c/users/steamuser/AppData/LocalLow/VRChat/VRChat/Tools/yt-dlp.exe"
            )
            candidates.append(candidate)
            if candidate.is_file() or candidate.with_suffix(candidate.suffix + ".bkp").is_file():
                return candidate
    detail = ", ".join(str(path) for path in candidates or checked)
    raise ResolveError(f"Could not locate VRChat's Tools/yt-dlp.exe; checked: {detail}")


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_with_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def patch_yt_dlp(target: Path, stub: Path, backup: Path | None = None) -> bool:
    """Safely replace VRChat's yt-dlp, returning True only when changed."""
    target = target.resolve()
    stub = stub.resolve()
    backup = (backup or target.with_suffix(target.suffix + ".bkp")).resolve()
    LOGGER.info("Patch target: %s", target)
    LOGGER.info("Patch backup: %s", backup)
    LOGGER.info("Patch stub: %s", stub)
    if not stub.is_file():
        raise ResolveError(f"Stub executable is missing or not a file: {stub}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stub_hash = sha256_file(stub)
    if target.is_file() and sha256_file(target) == stub_hash:
        LOGGER.info("VRChat yt-dlp is already patched; leaving backup untouched")
        return False
    if target.exists() and not target.is_file():
        raise ResolveError(f"Patch target exists but is not a file: {target}")

    if target.is_file():
        original_hash = sha256_file(target)
        if backup.exists():
            if not backup.is_file():
                raise ResolveError(f"Backup exists but is not a file: {backup}")
            if sha256_file(backup) != original_hash:
                raise ResolveError(
                    f"Refusing to overwrite target because existing backup does not match it: {backup}"
                )
        else:
            _replace_with_copy(target, backup)
        if not backup.is_file() or sha256_file(backup) != original_hash:
            raise ResolveError(f"Backup verification failed; target was not modified: {backup}")
        LOGGER.info("Backed up original yt-dlp to %s", backup)
    elif backup.exists():
        raise ResolveError(
            f"Target is missing but backup already exists; restore it before patching: {backup}"
        )

    _replace_with_copy(stub, target)
    if not target.is_file() or sha256_file(target) != stub_hash:
        raise ResolveError(f"Patched yt-dlp verification failed: {target}")
    LOGGER.info("Patched VRChat yt-dlp at %s", target)
    return True


def repatch_yt_dlp(target: Path, stub: Path, backup: Path) -> bool:
    """Reapply the stub after VRChat refreshes its bundled executable.

    The existing backup is deliberately never replaced here: it is the
    restore point captured before the service first patched VRChat.
    """
    target = target.resolve()
    stub = stub.resolve()
    backup = backup.resolve()
    if not stub.is_file():
        raise ResolveError(f"Stub executable is missing or not a file: {stub}")
    if not backup.is_file():
        raise ResolveError(f"Original backup is missing; cannot re-patch safely: {backup}")
    stub_hash = sha256_file(stub)
    if target.is_file() and sha256_file(target) == stub_hash:
        return False
    _replace_with_copy(stub, target)
    if not target.is_file() or sha256_file(target) != stub_hash:
        raise ResolveError(f"Re-patched yt-dlp verification failed: {target}")
    LOGGER.warning("VRChat refreshed yt-dlp; re-applied interception stub at %s", target)
    return True


def restore_yt_dlp(target: Path, backup: Path | None = None) -> bool:
    """Restore VRChat's yt-dlp from its verified backup, retaining the backup."""
    target = target.resolve()
    backup = (backup or target.with_suffix(target.suffix + ".bkp")).resolve()
    LOGGER.info("Restore target: %s", target)
    LOGGER.info("Restore backup: %s", backup)
    if not backup.exists():
        LOGGER.info("No backup exists; nothing to restore")
        return False
    if not backup.is_file():
        raise ResolveError(f"Backup exists but is not a file: {backup}")
    try:
        if target.exists():
            target.chmod(target.stat().st_mode | 0o200)
    except OSError as exc:
        raise ResolveError(f"Could not make restore target writable: {target}: {exc}") from exc
    backup_hash = sha256_file(backup)
    _replace_with_copy(backup, target)
    if not target.is_file() or sha256_file(target) != backup_hash:
        raise ResolveError(f"Restored yt-dlp verification failed: {target}")
    LOGGER.info("Restored original yt-dlp at %s", target)
    return True


def ensure_stub_executable(
    stub: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Build the default Windows stub on demand without installing tooling."""
    stub = stub.resolve()
    if stub.is_file():
        return
    if stub != DEFAULT_STUB.resolve():
        raise ResolveError(f"Stub executable is missing or not a file: {stub}")

    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise ResolveError(
            f"Stub executable is missing and dotnet is unavailable to build it: {stub}"
        )
    project = Path(__file__).parent / "yt-dlp-stub" / "yt-dlp-stub.csproj"
    if not project.is_file():
        raise ResolveError(f"Stub project is missing: {project}")
    command = [
        dotnet,
        "publish",
        str(project),
        "-r",
        "win-x64",
        "-c",
        "Release",
        "-o",
        str(stub.parent),
        "--nologo",
    ]
    LOGGER.info("Stub executable is missing; building it with: %s", shlex.join(command))
    try:
        result = run(command, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise ResolveError(f"Could not start dotnet to build the stub: {exc}") from exc
    if result.stdout.strip():
        LOGGER.info("dotnet publish: %s", result.stdout.strip())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "dotnet exited without output"
        raise ResolveError(f"Stub build failed (exit {result.returncode}): {detail}")
    if not stub.is_file():
        raise ResolveError(f"Stub build completed but did not create: {stub}")
    try:
        with stub.open("rb") as executable:
            signature = executable.read(2)
    except OSError as exc:
        raise ResolveError(f"Could not verify built stub {stub}: {exc}") from exc
    if signature != b"MZ":
        raise ResolveError(f"Built stub is not a Windows PE executable: {stub}")
    LOGGER.info("Built Windows yt-dlp stub at %s", stub)


def playlist_is_complete(path: Path) -> bool:
    try:
        with path.open("rb") as playlist:
            tail = playlist.read()[-4096:]
    except (FileNotFoundError, OSError):
        return False
    return b"#EXT-X-ENDLIST" in tail


def directory_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except (FileNotFoundError, OSError):
                continue
    except (FileNotFoundError, OSError):
        return 0
    return total


class CacheStateStore:
    """Persistent access metadata and bounded cache eviction."""

    def __init__(
        self,
        cache_root: Path,
        limit_bytes: int,
        grace_seconds: float,
        state_path: Path | None = None,
    ) -> None:
        self.cache_root = cache_root.resolve()
        self.limit_bytes = limit_bytes
        self.grace_seconds = grace_seconds
        self.state_path = (state_path or self.cache_root / STATE_FILENAME).resolve()
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.state_path.open("r", encoding="utf-8") as state_file:
                payload = json.load(state_file)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("videos"), dict):
            return
        with self._lock:
            for video_id, raw in payload["videos"].items():
                if not VIDEO_ID_RE.fullmatch(str(video_id)) or not isinstance(raw, dict):
                    continue
                path = self.cache_root / str(video_id)
                try:
                    last_accessed = float(raw.get("last_accessed", 0))
                    size_bytes = max(0, int(raw.get("size_bytes", 0)))
                except (TypeError, ValueError):
                    continue
                self._entries[str(video_id)] = {
                    "path": str(path),
                    "size_bytes": size_bytes,
                    "last_accessed": last_accessed,
                    "active": bool(raw.get("active", False)),
                }

    def _save_locked(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "videos": self._entries}
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as state_file:
                json.dump(payload, state_file, ensure_ascii=True, sort_keys=True)
                state_file.write("\n")
            temporary.replace(self.state_path)
        except OSError as exc:
            LOGGER.warning("Could not persist cache state %s: %s", self.state_path, exc)
            try:
                temporary.unlink()
            except OSError:
                pass

    def _entry_locked(self, video_id: str) -> dict[str, Any]:
        entry = self._entries.setdefault(
            video_id,
            {
                "path": str(self.cache_root / video_id),
                "size_bytes": 0,
                "last_accessed": 0.0,
                "active": False,
            },
        )
        entry["path"] = str(self.cache_root / video_id)
        return entry

    def touch(self, video_id: str, now: float | None = None) -> None:
        with self._lock:
            entry = self._entry_locked(video_id)
            entry["last_accessed"] = time.time() if now is None else now
            entry["size_bytes"] = directory_size(self.cache_root / video_id)
            self._save_locked()

    def set_active(self, video_id: str, active: bool) -> None:
        with self._lock:
            entry = self._entry_locked(video_id)
            entry["active"] = active
            entry["size_bytes"] = directory_size(self.cache_root / video_id)
            self._save_locked()

    def total_size(self) -> int:
        with self._lock:
            total = 0
            for video_id in list(self._entries):
                total += directory_size(self.cache_root / video_id)
            return total

    def evict_if_needed(self, protected_ids: set[str] | None = None, now: float | None = None) -> int:
        protected_ids = protected_ids or set()
        current_time = time.time() if now is None else now
        target_bytes = int(self.limit_bytes * 0.9)
        with self._lock:
            directories = {
                path.name: path
                for path in self.cache_root.iterdir()
                if path.is_dir() and VIDEO_ID_RE.fullmatch(path.name)
            }
            total = 0
            for video_id, path in directories.items():
                entry = self._entry_locked(video_id)
                entry["size_bytes"] = directory_size(path)
                entry["path"] = str(path)
                if video_id in protected_ids:
                    entry["active"] = True
                elif playlist_is_complete(path / "stream.m3u8"):
                    entry["active"] = False
                total += entry["size_bytes"]
            if total <= self.limit_bytes:
                self._save_locked()
                return 0

            candidates = []
            grace_cutoff = current_time - self.grace_seconds
            for video_id, path in directories.items():
                entry = self._entry_locked(video_id)
                playlist = path / "stream.m3u8"
                if video_id in protected_ids or entry.get("active"):
                    continue
                if playlist.exists() and not playlist_is_complete(playlist):
                    continue
                last_accessed = float(entry.get("last_accessed", 0))
                if last_accessed > grace_cutoff:
                    continue
                candidates.append((last_accessed, video_id, path, int(entry["size_bytes"])))
            candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))

            freed = 0
            for last_accessed, video_id, path, size_bytes in candidates:
                if total <= target_bytes:
                    break
                try:
                    if path.resolve().parent != self.cache_root:
                        LOGGER.warning("Skipping cache path outside root: %s", path)
                        continue
                    shutil.rmtree(path)
                except OSError as exc:
                    LOGGER.warning("Could not evict cache video %s: %s", video_id, exc)
                    continue
                total -= size_bytes
                freed += size_bytes
                self._entries.pop(video_id, None)
                LOGGER.info(
                    "Evicted cache video %s: freed %d bytes; last_accessed=%s",
                    video_id,
                    size_bytes,
                    last_accessed,
                )
            self._save_locked()
            if total > self.limit_bytes:
                LOGGER.warning(
                    "Cache remains over limit: %d bytes used, limit %d bytes; protected entries prevented eviction",
                    total,
                    self.limit_bytes,
                )
            return freed

    def status(self, active_ids: set[str] | None = None) -> dict[str, Any]:
        active_ids = active_ids or set()
        with self._lock:
            directories = {
                path.name: path
                for path in self.cache_root.iterdir()
                if path.is_dir() and VIDEO_ID_RE.fullmatch(path.name)
            }
            videos = []
            total = 0
            for video_id, path in directories.items():
                entry = self._entry_locked(video_id)
                entry["size_bytes"] = directory_size(path)
                if video_id in active_ids:
                    entry["active"] = True
                elif playlist_is_complete(path / "stream.m3u8"):
                    entry["active"] = False
                total += entry["size_bytes"]
                videos.append(
                    {
                        "video_id": video_id,
                        "path": str(path),
                        "size_bytes": entry["size_bytes"],
                        "last_accessed": entry["last_accessed"],
                        "active": entry["active"],
                        "finalized": playlist_is_complete(path / "stream.m3u8"),
                    }
                )
            self._save_locked()
            videos.sort(key=lambda video: (video["last_accessed"], video["video_id"]))
            return {
                "cache_dir": str(self.cache_root),
                "state_file": str(self.state_path),
                "limit_bytes": self.limit_bytes,
                "target_bytes": int(self.limit_bytes * 0.9),
                "grace_seconds": self.grace_seconds,
                "total_bytes": total,
                "videos": videos,
            }

    def clear(self) -> int:
        """Remove cached video directories and metadata for this service run."""
        removed = 0
        with self._lock:
            try:
                directories = list(self.cache_root.iterdir())
            except (FileNotFoundError, OSError):
                directories = []
            for path in directories:
                if not VIDEO_ID_RE.fullmatch(path.name):
                    continue
                try:
                    if path.is_symlink():
                        path.unlink()
                        removed += 1
                        continue
                    if not path.is_dir():
                        continue
                    if path.resolve().parent != self.cache_root:
                        LOGGER.warning("Skipping cache path outside root: %s", path)
                        continue
                    shutil.rmtree(path)
                    removed += 1
                except OSError as exc:
                    LOGGER.warning("Could not remove cache video %s: %s", path.name, exc)
            self._entries.clear()
            for path in (self.state_path, self.state_path.with_name(self.state_path.name + ".tmp")):
                try:
                    if path.parent == self.cache_root:
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning("Could not remove cache metadata %s: %s", path, exc)
        return removed


class YtDlpResolver:
    def __init__(
        self,
        executable: Path,
        cookies: Path | None,
        max_height: int,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.cookies = cookies
        self.max_height = max_height
        self.run = run

    def _resolve_once(
        self,
        expected_video_id: str,
        cookie_path: Path | None,
    ) -> DirectStreams:
        format_selector = (
            f"bestvideo[protocol^=http][vcodec^=avc1][height<={self.max_height}]"
            "+bestaudio[protocol^=http][acodec^=mp4a]"
        )
        command = [
            str(self.executable),
            "--no-playlist",
            "--match-filter",
            "!is_live",
            "--format",
            format_selector,
            "--dump-single-json",
            "--no-warnings",
            "--",
            canonical_youtube_url(expected_video_id),
        ]
        if cookie_path is not None:
            command[2:2] = ["--cookies", str(cookie_path)]
        LOGGER.info("yt-dlp command: %s", shlex.join(command))
        try:
            result = self.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            raise ResolveError(f"Could not start yt-dlp: {exc}") from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or "yt-dlp exited without an error message"
            raise ResolveError(f"yt-dlp failed (exit {result.returncode}): {detail}")

        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ResolveError(f"yt-dlp returned invalid JSON: {exc}") from exc

        actual_video_id = info.get("id")
        if actual_video_id != expected_video_id:
            raise ResolveError(
                f"yt-dlp resolved unexpected video ID {actual_video_id!r} "
                f"instead of {expected_video_id!r}"
            )
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise ResolveError("Live streams and upcoming streams are not supported")

        requested = info.get("requested_formats")
        if not isinstance(requested, list):
            raise ResolveError("yt-dlp did not return separate video and audio formats")

        video = next(
            (
                item
                for item in requested
                if str(item.get("vcodec", "")).startswith("avc1")
                and item.get("acodec") in {None, "none"}
            ),
            None,
        )
        audio = next(
            (
                item
                for item in requested
                if str(item.get("acodec", "")).startswith("mp4a")
                and item.get("vcodec") in {None, "none"}
            ),
            None,
        )
        if not video or not audio or not video.get("url") or not audio.get("url"):
            raise ResolveError("yt-dlp did not return H.264 video and AAC audio URLs")

        video_url = str(video["url"])
        audio_url = str(audio["url"])
        LOGGER.info("yt-dlp video URL: %s", video_url)
        LOGGER.info("yt-dlp audio URL: %s", audio_url)
        return DirectStreams(
            video_id=expected_video_id,
            title=str(info.get("title") or expected_video_id),
            video_url=video_url,
            audio_url=audio_url,
            video_format=str(video.get("format_id") or "unknown"),
            audio_format=str(audio.get("format_id") or "unknown"),
            video_headers=_safe_headers(video.get("http_headers")),
            audio_headers=_safe_headers(audio.get("http_headers")),
        )

    def __call__(self, expected_video_id: str) -> DirectStreams:
        with tempfile.TemporaryDirectory(prefix="vrchat-video-player-enhancer-cookies-") as temporary_dir:
            cookie_path: Path | None = None
            if self.cookies is not None and self.cookies.is_file():
                cookie_path = Path(temporary_dir) / "youtube_cookies.txt"
                try:
                    shutil.copyfile(self.cookies, cookie_path)
                except OSError as exc:
                    raise ResolveError(f"Could not copy cookies file for yt-dlp: {exc}") from exc
            try:
                return self._resolve_once(expected_video_id, None)
            except ResolveError as public_error:
                if cookie_path is None:
                    raise
                LOGGER.info(
                    "Public YouTube resolve failed; retrying with configured cookies: %s",
                    public_error,
                )
                return self._resolve_once(expected_video_id, cookie_path)


def _safe_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name): str(header_value)
        for name, header_value in value.items()
        if str(name).lower() not in {"cookie", "host", "content-length"}
        and isinstance(header_value, (str, int, float))
    }


def _ffmpeg_headers(headers: dict[str, str]) -> str:
    return "".join(f"{name}: {value}\r\n" for name, value in headers.items())


class FfmpegRemuxer:
    def __init__(
        self,
        executable: Path,
        segment_seconds: int,
        popen: Callable[..., ProcessLike] = subprocess.Popen,
    ) -> None:
        self.executable = executable
        self.segment_seconds = segment_seconds
        self.popen = popen

    def __call__(self, streams: DirectStreams, output_dir: Path) -> ProcessLike:
        output_dir.mkdir(parents=True, exist_ok=True)
        playlist_path = output_dir / "stream.m3u8"
        segment_pattern = output_dir / "segment_%05d.ts"
        command = [
            str(self.executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
        ]
        if streams.video_headers:
            command.extend(["-headers", _ffmpeg_headers(streams.video_headers)])
        command.extend([
            "-i",
            streams.video_url,
        ])
        if streams.audio_headers:
            command.extend(["-headers", _ffmpeg_headers(streams.audio_headers)])
        command.extend([
            "-i",
            streams.audio_url,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            "-f",
            "hls",
            "-hls_segment_type",
            "mpegts",
            "-hls_init_time",
            "1",
            "-hls_time",
            str(self.segment_seconds),
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "temp_file",
            "-hls_segment_filename",
            str(segment_pattern),
            str(playlist_path),
        ])
        LOGGER.info("ffmpeg command: %s", shlex.join(command))
        try:
            return self.popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ResolveError(f"Could not start ffmpeg: {exc}") from exc


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose each redirect target instead of following it automatically."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class YtDlpPassthrough:
    """Resolve non-YouTube URLs with a controlled real yt-dlp invocation."""

    def __init__(
        self,
        executable: Path,
        cookies: Path | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.cookies = cookies
        self.run = run

    @staticmethod
    def follow_http_redirect(url: str) -> str | None:
        """Inspect a bounded chain of HTTP redirects without downloading media."""
        current_url = url
        seen = {current_url}
        opener = urllib.request.build_opener(_NoRedirectHandler())

        def probe(method: str) -> str | None:
            headers = {"User-Agent": "VRChat-Video-Player-Enhancer/1.0"}
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            request = urllib.request.Request(current_url, headers=headers, method=method)
            try:
                response = opener.open(request, timeout=3)
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    location = exc.headers.get("Location")
                    exc.close()
                    if location:
                        return urljoin(current_url, location)
                raise
            except (urllib.error.URLError, OSError, ValueError):
                raise
            else:
                response.close()
                return None

        for _hop in range(5):
            try:
                target_url = probe("HEAD")
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in {400, 403, 405, 501}:
                    return None
                try:
                    target_url = probe("GET")
                except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as get_exc:
                    if isinstance(get_exc, urllib.error.HTTPError):
                        get_exc.close()
                    return None
            except (urllib.error.URLError, OSError, ValueError):
                return None

            if not target_url:
                return None
            if is_youtube_url(target_url):
                return target_url
            if target_url in seen:
                return None
            seen.add(target_url)
            current_url = target_url
        return None

    def __call__(self, url: str, *, avpro: bool, source: str) -> str:
        command = [str(self.executable), "--no-playlist"]
        if source == "resonite":
            command.append("--flat-playlist")
        if not avpro:
            command.extend(["--format", "best[protocol^=http]"])
        with tempfile.TemporaryDirectory(prefix="vrchat-video-player-enhancer-passthrough-cookies-") as temporary_dir:
            if self.cookies is not None and self.cookies.is_file():
                cookie_path = Path(temporary_dir) / "cookies.txt"
                try:
                    shutil.copyfile(self.cookies, cookie_path)
                except OSError as exc:
                    raise ResolveError(f"Could not copy cookies file for passthrough yt-dlp: {exc}") from exc
                command.extend(["--cookies", str(cookie_path)])
            command.extend(["--get-url", "--", url])
            LOGGER.info("Passing through non-YouTube URL with command: %s", shlex.join(command))
            try:
                result = self.run(command, text=True, capture_output=True, check=False)
            except OSError as exc:
                raise ResolveError(f"Could not start passthrough yt-dlp: {exc}") from exc
        if result.stderr.strip():
            LOGGER.warning("passthrough yt-dlp: %s", result.stderr.strip())
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "yt-dlp exited without output"
            lowered = detail.lower()
            if any(marker in lowered for marker in ("403", "401", "age-restricted", "age restricted", "sign in", "login", "cookie")):
                detail += (
                    " Cookies might be needed to access this video; export browser cookies "
                    "to cookies.txt beside main.py and try again."
                )
            raise ResolveError(f"yt-dlp passthrough failed (exit {result.returncode}): {detail}")
        output = result.stdout.strip()
        if not output:
            raise ResolveError("yt-dlp passthrough returned no URL")
        return output


class JobManager:
    def __init__(
        self,
        cache_root: Path,
        resolver: Callable[[str], DirectStreams],
        remuxer: Callable[[DirectStreams, Path], ProcessLike],
        poll_interval: float = 0.1,
        cache_limit_bytes: int = DEFAULT_CACHE_LIMIT_BYTES,
        cache_grace_seconds: float = DEFAULT_CACHE_GRACE_SECONDS,
        cache_cleanup_interval: float = DEFAULT_CACHE_CLEANUP_INTERVAL,
        job_idle_timeout: float = DEFAULT_JOB_IDLE_TIMEOUT_SECONDS,
        job_monitor_interval: float = DEFAULT_JOB_MONITOR_INTERVAL,
    ) -> None:
        self.cache_root = cache_root
        self.resolver = resolver
        self.remuxer = remuxer
        self.poll_interval = poll_interval
        self._jobs: dict[str, Job] = {}
        self._workers: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.cache = CacheStateStore(cache_root, cache_limit_bytes, cache_grace_seconds)
        self.cache_cleanup_interval = cache_cleanup_interval
        self.job_idle_timeout = job_idle_timeout
        self.job_monitor_interval = job_monitor_interval
        self._maintenance_thread: threading.Thread | None = None
        if cache_cleanup_interval > 0 or job_idle_timeout > 0:
            self._maintenance_thread = threading.Thread(
                target=self._maintenance_loop,
                name="cache-maintenance",
                daemon=True,
            )
            self._maintenance_thread.start()

    def start_or_get(self, video_id: str) -> tuple[Job, bool]:
        output_dir = self.cache_root / video_id
        playlist_path = output_dir / "stream.m3u8"
        with self._lock:
            current = self._jobs.get(video_id)
            if current and current.state in ACTIVE_STATES | {"complete"}:
                self.cache.touch(video_id)
                return current, False
            if playlist_is_complete(playlist_path):
                cached = Job(video_id, playlist_path, state="complete")
                cached.finished_at = playlist_path.stat().st_mtime
                self._jobs[video_id] = cached
                self.cache.touch(video_id)
                return cached, False

            self.cache.touch(video_id)
            active_ids = {
                active_video_id
                for active_video_id, active_job in self._jobs.items()
                if active_job.state in ACTIVE_STATES or active_job.process is not None
            }
            self.cache.evict_if_needed(active_ids | {video_id})
            job = Job(video_id, playlist_path)
            self._jobs[video_id] = job
            self.cache.set_active(video_id, True)
            worker = threading.Thread(
                target=self._run_job,
                args=(job,),
                name=f"remux-{video_id}",
                daemon=True,
            )
            self._workers.add(worker)
            worker.start()
            return job, True

    def get(self, video_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(video_id)
        if job:
            return job
        playlist_path = self.cache_root / video_id / "stream.m3u8"
        if playlist_is_complete(playlist_path):
            job = Job(video_id, playlist_path, state="complete")
            job.finished_at = playlist_path.stat().st_mtime
            with self._lock:
                return self._jobs.setdefault(video_id, job)
        return None

    def record_access(self, video_id: str) -> None:
        with self._lock:
            job = self._jobs.get(video_id)
        if job:
            with job.condition:
                if job.state in ACTIVE_STATES:
                    job.last_hls_access = time.monotonic()
        self.cache.touch(video_id)

    def cache_status(self) -> dict[str, Any]:
        with self._lock:
            active_ids = {
                video_id
                for video_id, job in self._jobs.items()
                if job.state in ACTIVE_STATES or job.process is not None
            }
        return self.cache.status(active_ids)

    def _maintenance_loop(self) -> None:
        next_cleanup = (
            time.monotonic() + self.cache_cleanup_interval
            if self.cache_cleanup_interval > 0
            else None
        )
        while not self._stopping.wait(self._maintenance_wait(next_cleanup)):
            now = time.monotonic()
            try:
                self._cancel_inactive_jobs(now)
                if next_cleanup is not None and now >= next_cleanup:
                    with self._lock:
                        active_ids = {
                            video_id
                            for video_id, job in self._jobs.items()
                            if job.state in ACTIVE_STATES or job.process is not None
                        }
                    self.cache.evict_if_needed(active_ids)
                    next_cleanup = now + self.cache_cleanup_interval
            except Exception:
                LOGGER.exception("Cache maintenance failed")

    def _maintenance_wait(self, next_cleanup: float | None) -> float:
        wait = self.job_monitor_interval if self.job_idle_timeout > 0 else float("inf")
        if next_cleanup is not None:
            wait = min(wait, max(0.0, next_cleanup - time.monotonic()))
        return wait

    def _cancel_inactive_jobs(self, now: float) -> None:
        if self.job_idle_timeout <= 0 or self._stopping.is_set():
            return
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.condition:
                if (
                    job.state not in ACTIVE_STATES
                    or job.cancel_requested
                    or now - job.last_hls_access <= self.job_idle_timeout
                ):
                    continue
                process = job.process
                if process is not None and process.poll() is not None:
                    continue
                job.cancel_requested = True
            LOGGER.info(
                "Cancelling inactive HLS cache for %s (no request for %.1f seconds)",
                job.video_id,
                now - job.last_hls_access,
            )
            if process is not None:
                try:
                    process.terminate()
                except OSError as exc:
                    LOGGER.warning("Could not stop inactive HLS cache %s: %s", job.video_id, exc)

    def wait_until_available(self, job: Job, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with job.condition:
            while job.state in {"resolving", "starting"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                job.condition.wait(remaining)

    def _set_state(self, job: Job, state: str, **values: Any) -> None:
        with job.condition:
            job.state = state
            for key, value in values.items():
                setattr(job, key, value)
            job.condition.notify_all()

    def _clear_partial_output(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        candidates = [output_dir / "stream.m3u8", output_dir / "stream.m3u8.tmp"]
        candidates.extend(output_dir.glob("segment_*.ts"))
        candidates.extend(output_dir.glob("segment_*.ts.tmp"))
        for path in candidates:
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except FileNotFoundError:
                pass

    def _drain_stderr(self, process: ProcessLike, video_id: str) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip()
            if line:
                LOGGER.warning("ffmpeg[%s]: %s", video_id, line)

    def _run_job(self, job: Job) -> None:
        try:
            streams = self.resolver(job.video_id)
            if self._stopping.is_set():
                raise ResolveError("Service is shutting down")
            with job.condition:
                if job.cancel_requested:
                    raise JobCancelled
            self._set_state(
                job,
                "starting",
                title=streams.title,
                video_format=streams.video_format,
                audio_format=streams.audio_format,
            )
            output_dir = job.playlist_path.parent
            self._clear_partial_output(output_dir)
            process = self.remuxer(streams, output_dir)
            with job.condition:
                job.process = process
                cancelled = job.cancel_requested
            if cancelled:
                if process.poll() is None:
                    process.terminate()
                raise JobCancelled
            stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(process, job.video_id),
                name=f"ffmpeg-log-{job.video_id}",
                daemon=True,
            )
            stderr_thread.start()

            while process.poll() is None:
                if job.playlist_path.is_file() and job.state == "starting":
                    self._set_state(job, "running")
                if self._stopping.wait(self.poll_interval):
                    process.terminate()
                    break

            returncode = process.wait()
            stderr_thread.join(timeout=1)
            if self._stopping.is_set():
                raise ResolveError("Service stopped before remuxing completed")
            with job.condition:
                if job.cancel_requested:
                    raise JobCancelled
            if returncode != 0:
                raise ResolveError(f"ffmpeg failed with exit code {returncode}")
            if not playlist_is_complete(job.playlist_path):
                raise ResolveError("ffmpeg exited successfully but HLS playlist is incomplete")
            self._set_state(job, "complete", finished_at=time.time())
            self.cache.set_active(job.video_id, False)
            self.cache.touch(job.video_id)
            LOGGER.info("Completed HLS cache for %s", job.video_id)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            with job.condition:
                cancelled = isinstance(exc, JobCancelled) or job.cancel_requested
            if cancelled and not self._stopping.is_set():
                self._clear_partial_output(job.playlist_path.parent)
                self._set_state(job, "cancelled", error=None, finished_at=time.time())
                self.cache.set_active(job.video_id, False)
                self.cache.touch(job.video_id)
                LOGGER.info("Cancelled HLS cache for inactive video %s", job.video_id)
            else:
                self._set_state(job, "failed", error=message, finished_at=time.time())
                if not self._stopping.is_set():
                    self.cache.set_active(job.video_id, False)
                    self.cache.touch(job.video_id)
                    LOGGER.error("Job %s failed: %s", job.video_id, message)
        finally:
            with job.condition:
                job.process = None
            with self._lock:
                self._workers.discard(threading.current_thread())

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            processes = [job.process for job in self._jobs.values() if job.process]
            workers = list(self._workers)
        for process in processes:
            if process and process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for process in processes:
            if not process:
                continue
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=max(0, deadline - time.monotonic()))
        removed = self.cache.clear()
        LOGGER.info("Cleared %d cached video(s) on shutdown", removed)


class ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        manager: JobManager,
        public_host: str,
        startup_wait: float,
        passthrough: Callable[..., str] | None = None,
    ) -> None:
        self.manager = manager
        self.public_host = public_host
        self.startup_wait = startup_wait
        self.passthrough = passthrough
        super().__init__(address, ProxyRequestHandler)

    def playlist_url(self, video_id: str) -> str:
        return f"http://{self.public_host}:{self.server_port}/hls/{video_id}/stream.m3u8"

    def status_url(self, video_id: str) -> str:
        return f"http://{self.public_host}:{self.server_port}/status/{video_id}"


class ProxyRequestHandler(BaseHTTPRequestHandler):
    server: ProxyHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def _dispatch(self, send_body: bool) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"}, send_body)
            return
        if parsed.path == "/resolve":
            if not send_body:
                self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "HEAD is not supported"}, False)
                return
            self._handle_resolve(parsed.query)
            return
        if parsed.path == "/api/getvideo":
            if not send_body:
                self._send_text(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is not supported\n", False)
                return
            self._handle_getvideo(parsed.query)
            return
        if parsed.path == "/cache/status":
            self._send_json(HTTPStatus.OK, self.server.manager.cache_status(), send_body)
            return
        if parsed.path.startswith("/status/"):
            self._handle_status(parsed.path, send_body)
            return
        if parsed.path.startswith("/hls/"):
            self._serve_hls(parsed.path, send_body)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}, send_body)

    def _handle_resolve(self, query: str) -> None:
        values = parse_qs(query).get("url", [])
        if len(values) != 1 or not values[0].strip():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Provide exactly one non-empty url query parameter"},
                True,
            )
            return
        try:
            video_id = normalize_video_id(values[0])
        except ResolveError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)}, True)
            return

        job, created = self.server.manager.start_or_get(video_id)
        self.server.manager.record_access(video_id)
        self.server.manager.wait_until_available(job, self.server.startup_wait)
        snapshot = job.snapshot()
        snapshot.update(
            {
                "created": created,
                "playlist_url": self.server.playlist_url(video_id),
                "status_url": self.server.status_url(video_id),
            }
        )
        if snapshot["status"] == "failed":
            status = HTTPStatus.BAD_GATEWAY
        elif snapshot["status"] in {"resolving", "starting"}:
            status = HTTPStatus.ACCEPTED
        else:
            status = HTTPStatus.OK
        self._send_json(status, snapshot, True)

    def _handle_getvideo(self, query: str) -> None:
        query_values = parse_qs(query)
        urls = query_values.get("url", [])
        if len(urls) != 1 or not urls[0].strip():
            self._send_text(
                HTTPStatus.BAD_REQUEST,
                "ERROR: Provide exactly one non-empty url query parameter\n",
                True,
            )
            return
        avpro_values = query_values.get("avpro", ["true"])
        source_values = query_values.get("source", ["vrchat"])
        if len(avpro_values) != 1 or avpro_values[0].lower() not in {"true", "false"}:
            self._send_text(HTTPStatus.BAD_REQUEST, "ERROR: avpro must be true or false\n", True)
            return
        if len(source_values) != 1 or source_values[0].lower() not in {"vrchat", "resonite"}:
            self._send_text(
                HTTPStatus.BAD_REQUEST,
                "ERROR: source must be vrchat or resonite\n",
                True,
            )
            return
        url = urls[0].strip()
        avpro = avpro_values[0].lower() == "true"
        source = source_values[0].lower()

        redirected_url: str | None = None
        if not is_youtube_url(url) and self.server.passthrough is not None:
            redirect_detector = getattr(self.server.passthrough, "follow_http_redirect", None)
            if callable(redirect_detector):
                redirected_url = redirect_detector(url)
        if redirected_url:
            LOGGER.info("Interception followed redirect to YouTube: %s -> %s", url, redirected_url)
            url = redirected_url

        if is_youtube_url(url):
            try:
                video_id = normalize_video_id(url)
            except ResolveError as exc:
                self._send_text(HTTPStatus.BAD_REQUEST, f"ERROR: {exc}\n", True)
                return
            LOGGER.info(
                "Interception handled YouTube URL locally: video_id=%s avpro=%s source=%s",
                video_id,
                avpro,
                source,
            )
            job, _created = self.server.manager.start_or_get(video_id)
            self.server.manager.record_access(video_id)
            self.server.manager.wait_until_available(job, self.server.startup_wait)
            snapshot = job.snapshot()
            if snapshot["status"] == "failed":
                self._send_text(
                    HTTPStatus.BAD_GATEWAY,
                    f"ERROR: {snapshot['error'] or 'YouTube resolve failed'}\n",
                    True,
                )
                return
            self._send_text(HTTPStatus.OK, self.server.playlist_url(video_id) + "\n", True)
            return

        if self.server.passthrough is None:
            self._send_text(HTTPStatus.BAD_GATEWAY, "ERROR: yt-dlp passthrough is unavailable\n", True)
            return
        LOGGER.info("Interception passing through non-YouTube URL: avpro=%s source=%s", avpro, source)
        try:
            output = self.server.passthrough(url, avpro=avpro, source=source)
        except ResolveError as exc:
            self._send_text(HTTPStatus.BAD_GATEWAY, f"ERROR: {exc}\n", True)
            return
        self._send_text(HTTPStatus.OK, output.rstrip("\n") + "\n", True)

    def _handle_status(self, path: str, send_body: bool) -> None:
        video_id = unquote(path.removeprefix("/status/")).strip("/")
        if not VIDEO_ID_RE.fullmatch(video_id):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid video ID"}, send_body)
            return
        job = self.server.manager.get(video_id)
        if not job:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown video ID"}, send_body)
            return
        snapshot = job.snapshot()
        snapshot.update(
            {
                "playlist_url": self.server.playlist_url(video_id),
                "status_url": self.server.status_url(video_id),
            }
        )
        self._send_json(HTTPStatus.OK, snapshot, send_body)

    def _serve_hls(self, request_path: str, send_body: bool) -> None:
        relative = unquote(request_path.removeprefix("/hls/"))
        parts = Path(relative).parts
        if (
            len(parts) != 2
            or not VIDEO_ID_RE.fullmatch(parts[0])
            or not re.fullmatch(r"(?:stream\.m3u8|segment_\d{5}\.ts)", parts[1])
        ):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}, send_body)
            return
        path = self.server.manager.cache_root / parts[0] / parts[1]
        self.server.manager.record_access(parts[0])
        if parts[1] == "stream.m3u8" and not path.is_file():
            deadline = time.monotonic() + HLS_PLAYLIST_WAIT_SECONDS
            while time.monotonic() < deadline and not path.is_file():
                job = self.server.manager.get(parts[0])
                if not job or job.state not in ACTIVE_STATES:
                    break
                time.sleep(0.05)
            if not path.is_file():
                self._send_text(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "HLS playlist is not available yet; retry shortly\n",
                    send_body,
                )
                return
        try:
            stat = path.stat()
            file_handle = path.open("rb")
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "HLS file is not available yet"}, send_body)
            return

        content_type = (
            "application/vnd.apple.mpegurl"
            if path.suffix == ".m3u8"
            else "video/mp2t"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header(
            "Cache-Control",
            "no-store" if path.suffix == ".m3u8" else "public, max-age=31536000, immutable",
        )
        self.end_headers()
        try:
            if send_body:
                while chunk := file_handle.read(1024 * 256):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            file_handle.close()

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], send_body: bool) -> None:
        body = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_text(self, status: HTTPStatus, payload: str, send_body: bool) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("HTTP %s - %s", self.address_string(), fmt % args)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def size_bytes(value: str) -> int:
    text = str(value).strip().upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT](?:I?B)?)?", text)
    if not match:
        raise argparse.ArgumentTypeError("use a byte count or suffix such as 200M or 10G")
    amount = float(match.group(1))
    unit = match.group(2) or "B"
    multiplier = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "KIB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "MIB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "GIB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
        "TIB": 1024**4,
    }[unit]
    parsed = int(amount * multiplier)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--patch", action="store_true", help="back up and patch VRChat's yt-dlp.exe")
    action.add_argument("--restore", action="store_true", help="restore VRChat's yt-dlp.exe from backup")
    parser.add_argument(
        "--vrchat-yt-dlp",
        type=Path,
        help="override automatic discovery of VRChat's bundled Tools/yt-dlp.exe",
    )
    parser.add_argument("--stub", type=Path, default=DEFAULT_STUB, help="Windows stub executable used by --patch")
    parser.add_argument("--backup", type=Path, help="override the adjacent yt-dlp.exe.bkp backup path")
    parser.add_argument("--port", type=int, default=9696)
    parser.add_argument("--max-height", type=positive_int, default=1080)
    parser.add_argument("--segment-seconds", type=positive_int, default=6)
    parser.add_argument(
        "--startup-wait",
        type=nonnegative_float,
        default=10.0,
        help="wait this many seconds for the first playlist (default: 10; use 0 to return immediately)",
    )
    parser.add_argument(
        "--cache-max-size",
        "--cache-limit",
        dest="cache_max_size",
        type=size_bytes,
        default=os.environ.get("VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_MAX_SIZE", "10G"),
        help="maximum cache size in bytes or units such as 200M/10G (env: VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_MAX_SIZE)",
    )
    parser.add_argument(
        "--cache-grace-minutes",
        type=nonnegative_float,
        default=float(os.environ.get("VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_GRACE_MINUTES", "10")),
        help="protect recently accessed entries for this many minutes",
    )
    parser.add_argument(
        "--cache-cleanup-interval",
        type=nonnegative_float,
        default=float(os.environ.get("VRCHAT_VIDEO_PLAYER_ENHANCER_CACHE_CLEANUP_INTERVAL", "300")),
        help="periodic cleanup interval in seconds; zero disables periodic cleanup",
    )
    parser.add_argument(
        "--job-idle-timeout",
        type=nonnegative_float,
        default=float(os.environ.get("VRCHAT_VIDEO_PLAYER_ENHANCER_JOB_IDLE_TIMEOUT", "30")),
        help="cancel active HLS jobs after this many seconds without a playlist/segment request; zero disables it",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).parent / "cache")
    parser.add_argument("--yt-dlp", type=Path, default=DEFAULT_YTDLP)
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--cookies", type=Path, default=DEFAULT_COOKIES)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def validate_runtime(args: argparse.Namespace) -> None:
    for label, path in (("yt-dlp", args.yt_dlp), ("ffmpeg", args.ffmpeg)):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ResolveError(f"{label} executable is missing or not executable: {path}")
    if args.cookies is not None:
        if args.cookies.exists() and not args.cookies.is_file():
            raise ResolveError(f"Cookies path is not a file: {args.cookies}")
        if args.cookies.is_file() and not os.access(args.cookies, os.R_OK):
            raise ResolveError(f"Cookies file is unreadable: {args.cookies}")
        if not args.cookies.exists():
            LOGGER.info("Cookies file not found; continuing without cookies: %s", args.cookies)
    try:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ResolveError(f"Cannot create cache directory {args.cache_dir}: {exc}") from exc
    if not os.access(args.cache_dir, os.W_OK):
        raise ResolveError(f"Cache directory is not writable: {args.cache_dir}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.patch or args.restore:
        try:
            target = args.vrchat_yt_dlp or discover_vrchat_ytdlp_path()
            backup = args.backup or target.with_suffix(target.suffix + ".bkp")
            if args.patch:
                ensure_stub_executable(args.stub)
                patch_yt_dlp(target, args.stub, backup)
            else:
                restore_yt_dlp(target, backup)
        except (ResolveError, OSError) as exc:
            LOGGER.error("%s failed: %s", "Patch" if args.patch else "Restore", exc)
            return 2
        return 0

    # Keep interception active only while this service runs. Restore the
    # original executable on every normal or failed shutdown path.
    patch_target: Path | None = None
    patch_backup: Path | None = None
    patch_active = False
    try:
        ensure_stub_executable(args.stub)
        patch_target = (args.vrchat_yt_dlp or discover_vrchat_ytdlp_path()).resolve()
        patch_backup = (args.backup or patch_target.with_suffix(patch_target.suffix + ".bkp")).resolve()
        patch_yt_dlp(patch_target, args.stub, patch_backup)
        if not patch_backup.is_file():
            raise ResolveError(
                f"VRChat yt-dlp is patched but its original backup is unavailable: {patch_backup}"
            )
        patch_active = (
            patch_target.is_file()
            and sha256_file(patch_target) == sha256_file(args.stub.resolve())
            and patch_backup.is_file()
        )
        LOGGER.info("Automatic VRChat yt-dlp interception is active")
    except (ResolveError, OSError) as exc:
        LOGGER.error("Startup patch failed: %s", exc)
        return 2

    def restore_patch() -> None:
        nonlocal patch_active
        if not patch_active or patch_target is None or patch_backup is None:
            return
        try:
            restore_yt_dlp(patch_target, patch_backup)
            patch_active = False
        except (ResolveError, OSError) as exc:
            LOGGER.error("Automatic VRChat yt-dlp restore failed: %s", exc)

    try:
        validate_runtime(args)
    except ResolveError as exc:
        LOGGER.error("Startup failed: %s", exc)
        restore_patch()
        return 2

    resolver = YtDlpResolver(args.yt_dlp, args.cookies, args.max_height)
    passthrough = YtDlpPassthrough(args.yt_dlp, args.cookies)
    remuxer = FfmpegRemuxer(args.ffmpeg, args.segment_seconds)
    try:
        manager = JobManager(
            args.cache_dir.resolve(),
            resolver,
            remuxer,
            cache_limit_bytes=args.cache_max_size,
            cache_grace_seconds=args.cache_grace_minutes * 60,
            cache_cleanup_interval=args.cache_cleanup_interval,
            job_idle_timeout=args.job_idle_timeout,
        )
    except Exception as exc:
        LOGGER.error("Could not initialize cache manager: %s", exc)
        restore_patch()
        return 2
    try:
        server = ProxyHTTPServer(
            ("127.0.0.1", args.port),
            manager,
            "127.0.0.1",
            args.startup_wait,
            passthrough,
        )
    except OSError as exc:
        LOGGER.error("Could not listen on 127.0.0.1:%s: %s", args.port, exc)
        manager.shutdown()
        restore_patch()
        return 2

    interception_stop = threading.Event()

    def monitor_interception() -> None:
        while not interception_stop.wait(0.5):
            if not patch_active or patch_target is None or patch_backup is None:
                continue
            try:
                repatch_yt_dlp(patch_target, args.stub, patch_backup)
            except (ResolveError, OSError) as exc:
                LOGGER.error("Could not maintain VRChat yt-dlp interception: %s", exc)

    interception_thread = threading.Thread(
        target=monitor_interception,
        name="vrchat-yt-dlp-interception",
        daemon=True,
    )
    interception_thread.start()

    stopping = False

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal stopping
        if not stopping:
            stopping = True
            LOGGER.info("Received signal %s; shutting down", signum)
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    LOGGER.info("Listening on http://127.0.0.1:%s", server.server_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        manager.shutdown()
        interception_stop.set()
        interception_thread.join(timeout=2)
        restore_patch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
