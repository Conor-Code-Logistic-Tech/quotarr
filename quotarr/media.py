"""Everything that shells out to ffmpeg: frames, subtitle extraction and clips."""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .config import Settings
from .plex import PlexClient
from .subtitles import Cue, decode_bytes

log = logging.getLogger(__name__)

# HDR -> SDR so frames from 4K HDR files don't come out grey and washed out.
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)
_SUB_EXT = {"ass": "ass", "ssa": "ssa", "vtt": "vtt", "webvtt": "vtt", "smi": "smi", "sami": "smi", "mov_text": "srt"}


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    url: str
    headers: str | None = None

    @property
    def is_local(self) -> bool:
        return self.headers is None

    def input_args(self, seek_ms: int | None = None) -> list[str]:
        args = ["-ss", f"{seek_ms / 1000:.3f}"] if seek_ms else []
        if self.headers:
            args += ["-headers", self.headers]
        return args + ["-i", self.url]


def map_path(path: str | None, mappings: list[tuple[str, str]]) -> str | None:
    if not path:
        return None
    for src, dst in mappings:
        if path.startswith(src):
            mapped = dst.rstrip("/\\") + path[len(src):]
            return mapped.replace("\\", "/") if "\\" in src else mapped
    return path


def _srt_time(ms: int) -> str:
    h, rem = divmod(max(ms, 0), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[Cue]) -> str:
    return "\n".join(
        f"{i}\n{_srt_time(c.start_ms)} --> {_srt_time(c.end_ms)}\n{c.text}\n" for i, c in enumerate(cues, 1)
    )


class Media:
    def __init__(self, settings: Settings, plex: PlexClient):
        self.settings = settings
        self.plex = plex
        self._sem = threading.BoundedSemaphore(settings.ffmpeg_concurrency)
        self._guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        self._usage: tuple[float, dict] | None = None
        for d in (settings.thumbs_dir, settings.cache_dir, settings.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.can_tonemap = self._has_filter("zscale") and self._has_filter("tonemap")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _has_filter(name: str) -> bool:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return any(line.split()[1:2] == [name] for line in out.stdout.splitlines() if line.strip())

    def _run(self, args: list[str], timeout: float, stdout: bool = False) -> bytes:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
        try:
            with self._sem:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"ffmpeg timed out after {timeout:.0f}s") from exc
        except FileNotFoundError as exc:
            raise MediaError("ffmpeg is not installed") from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise MediaError("ffmpeg failed: " + (" | ".join(err[-3:]) or f"exit {proc.returncode}"))
        return proc.stdout if stdout else b""

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        """Serialize work on one cache key so concurrent requests don't render the same file twice."""
        with self._guard:
            lock, users = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._guard:
                lock, users = self._locks[key]
                if users <= 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, users - 1)

    def _tmp_path(self, suffix: str) -> Path:
        fd, name = tempfile.mkstemp(suffix=suffix, dir=self.settings.tmp_dir)
        os.close(fd)
        return Path(name)

    def cached(self, path: Path, render) -> Path:
        if path.exists():
            _touch(path)
            return path
        with self._locked(str(path)):
            if path.exists():
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._tmp_path(path.suffix)
            try:
                render(tmp)
                if tmp.stat().st_size == 0:
                    raise MediaError("ffmpeg produced no output (timestamp past the end of the video?)")
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        return path

    def _video_filter(self, width: int, tonemap: bool) -> str:
        chain = [TONEMAP] if tonemap else []
        # Square up anamorphic pixels (DVD rips), never upscale, keep dimensions even.
        chain.append(f"scale=w='trunc(min({width},iw*sar)/2)*2':h='trunc(ow/dar/2)*2',setsar=1")
        return ",".join(chain)

    def _run_video(self, build: Callable[[bool], list[str]], hdr: bool, timeout: float) -> None:
        """Run ffmpeg with tone mapping for HDR sources, retrying without it if the file's colour tags confuse zscale."""
        if hdr and self.can_tonemap:
            try:
                self._run(build(True), timeout)
                return
            except MediaError as exc:
                log.info("Tone mapping failed, retrying without it: %s", exc)
        self._run(build(False), timeout)

    # ---------------------------------------------------------------- sources
    def source_for(self, ep: Mapping) -> Source:
        local = map_path(ep["file_path"], self.settings.path_mappings)
        if local and os.path.isfile(local):
            return Source(local)
        if not ep["part_key"]:
            raise MediaError("Plex has no media file for this episode")
        return Source(self.plex.media_url(ep["part_key"]), f"X-Plex-Token: {self.plex.token}\r\n")

    # ---------------------------------------------------------------- frames
    def thumb_path(self, episode_id: int, ms: int) -> Path:
        return self.settings.thumbs_dir / str(episode_id) / f"{ms}.jpg"

    def frame(self, ep: Mapping, ms: int, thumb: bool = False) -> Path:
        if thumb:
            path, width, quality = self.thumb_path(ep["id"], ms), self.settings.thumb_width, 5
        else:
            path = self.settings.cache_dir / "frames" / str(ep["id"]) / f"{ms}.jpg"
            width, quality = self.settings.frame_width, 2

        def render(out: Path) -> None:
            src = self.source_for(ep)
            self._run_video(
                lambda tonemap: [*src.input_args(ms), "-map", "0:V:0", "-frames:v", "1",
                                 "-vf", self._video_filter(width, tonemap),
                                 "-q:v", str(quality), "-f", "image2", "-c:v", "mjpeg", str(out)],
                bool(ep["hdr"]), timeout=45 if src.is_local else 90,
            )

        return self.cached(path, render)

    # ---------------------------------------------------------------- subtitles
    def extract_subtitle(self, ep: Mapping, stream_index: int) -> str:
        src = self.source_for(ep)
        out = self._run([*src.input_args(), "-map", f"0:{stream_index}", "-f", "srt", "pipe:1"],
                        timeout=1800, stdout=True)
        return out.decode("utf-8", "replace")

    def convert_subtitle(self, data: bytes, codec: str) -> str:
        codec = codec.lower()
        if codec in ("srt", "subrip"):
            return decode_bytes(data)
        tmp = self._tmp_path("." + _SUB_EXT.get(codec, codec))
        try:
            tmp.write_text(decode_bytes(data), encoding="utf-8")
            return self._run(["-i", str(tmp), "-f", "srt", "pipe:1"], timeout=60, stdout=True).decode("utf-8", "replace")
        finally:
            tmp.unlink(missing_ok=True)

    # ---------------------------------------------------------------- clips
    def clip(self, ep: Mapping, start_ms: int, end_ms: int, cues: list[Cue] | None, fmt: str) -> Path:
        """Render a GIF or MP4 for [start_ms, end_ms]; `cues` are burned in (times relative to start)."""
        srt = build_srt(cues) if cues else ""
        key = hashlib.sha1(f"{ep['id']}|{start_ms}|{end_ms}|{fmt}|{srt}".encode()).hexdigest()
        path = self.settings.cache_dir / "clips" / f"{key}.{fmt}"
        width = self.settings.gif_width if fmt == "gif" else self.settings.mp4_width

        def render(out: Path) -> None:
            src = self.source_for(ep)
            sub_file = None
            try:
                if srt:
                    sub_file = self._tmp_path(".srt")
                    sub_file.write_text(srt, encoding="utf-8")

                def build(tonemap: bool) -> list[str]:
                    chain = (["fps=12"] if fmt == "gif" else []) + [self._video_filter(width, tonemap)]
                    if sub_file:
                        style = ("FontName=DejaVu Sans,Bold=1,FontSize=20,PrimaryColour=&H00FFFFFF,"
                                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,MarginV=14")
                        chain.append(f"subtitles=filename='{sub_file}':force_style='{style}'")
                    vf = ",".join(chain)
                    args = [*src.input_args(start_ms), "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-map", "0:V:0"]
                    if fmt == "gif":
                        vf += (",split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];"
                               "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
                        return args + ["-vf", vf, "-loop", "0", "-f", "gif", str(out)]
                    return args + ["-map", "0:a:0?", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                                   "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                                   "-movflags", "+faststart", "-f", "mp4", str(out)]

                self._run_video(build, bool(ep["hdr"]), timeout=300)
            finally:
                if sub_file:
                    sub_file.unlink(missing_ok=True)

        return self.cached(path, render)

    # ---------------------------------------------------------------- cache
    def prune_cache(self) -> None:
        """Delete least-recently-used cache files once the cache is over CACHE_MAX_MB."""
        limit = self.settings.cache_max_mb * 1024 * 1024
        roots = [self.settings.cache_dir]
        if not self.settings.pregenerate_thumbs:
            roots.append(self.settings.thumbs_dir)
        files = []
        for root in roots:
            for path in root.rglob("*"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                if path.is_file():
                    files.append((st.st_mtime, st.st_size, path))
        total = sum(f[1] for f in files)
        if total > limit:
            files.sort()
            target = limit * 0.9
            for _, size, path in files:
                if total <= target:
                    break
                path.unlink(missing_ok=True)
                total -= size
            log.info("Pruned cache to %.0f MB", total / 1e6)
        for stale in self.settings.tmp_dir.glob("*"):
            if time.time() - _mtime(stale) > 3600:
                stale.unlink(missing_ok=True)
        self._usage = None

    def disk_usage(self) -> dict:
        if self._usage and time.time() - self._usage[0] < 60:
            return self._usage[1]
        thumbs = [p for p in self.settings.thumbs_dir.rglob("*.jpg")]
        usage = {"thumbs": sum(_file_size(p) for p in thumbs), "thumb_files": len(thumbs),
                 "cache": _dir_size(self.settings.cache_dir)}
        db = self.settings.data_dir / "quotarr.db"
        usage["database"] = sum(_file_size(Path(f"{db}{s}")) for s in ("", "-wal", "-shm"))
        self._usage = (time.time(), usage)
        return usage

    def remove_thumbs(self, episode_id: int) -> None:
        folder = self.settings.thumbs_dir / str(episode_id)
        for path in folder.glob("*"):
            path.unlink(missing_ok=True)
        if folder.exists():
            folder.rmdir()


def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dir_size(path: Path) -> int:
    return sum(_file_size(p) for p in path.rglob("*") if p.is_file())
