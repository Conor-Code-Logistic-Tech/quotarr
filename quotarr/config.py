"""Runtime settings, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    return float(raw) if raw else default


def _split(raw: str, seps: str = ";\n") -> list[str]:
    for sep in seps[1:]:
        raw = raw.replace(sep, seps[0])
    return [part.strip() for part in raw.split(seps[0]) if part.strip()]


def parse_path_mappings(raw: str) -> list[tuple[str, str]]:
    """Parse "plex/path=local/path;other=..." into (plex, local) pairs, longest prefix first."""
    pairs = []
    for chunk in _split(raw):
        if "=" not in chunk:
            continue
        src, dst = (p.strip() for p in chunk.split("=", 1))
        if src and dst:
            pairs.append((src, dst))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


@dataclass(frozen=True)
class Settings:
    plex_url: str
    plex_token: str
    plex_verify_ssl: bool
    data_dir: Path
    path_mappings: list[tuple[str, str]]
    subtitle_languages: list[str]
    strip_sdh: bool
    auto_index_shows: list[str]
    pregenerate_thumbs: bool
    thumb_width: int
    frame_width: int
    cache_max_mb: int
    ffmpeg_concurrency: int
    rescan_hours: float
    max_clip_seconds: float
    gif_width: int
    mp4_width: int
    meme_font: str
    admin_user: str
    admin_password: str
    public_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            plex_url=_str("PLEX_URL").rstrip("/"),
            plex_token=_str("PLEX_TOKEN"),
            plex_verify_ssl=_bool("PLEX_VERIFY_SSL", True),
            data_dir=Path(_str("DATA_DIR", "./data")),
            path_mappings=parse_path_mappings(_str("PATH_MAPPINGS")),
            subtitle_languages=[s.lower() for s in _split(_str("SUBTITLE_LANGUAGES", "eng,en"), ",;")],
            strip_sdh=_bool("STRIP_SDH", True),
            auto_index_shows=_split(_str("AUTO_INDEX_SHOWS")),
            pregenerate_thumbs=_bool("PREGENERATE_THUMBS", False),
            thumb_width=_int("THUMB_WIDTH", 480),
            frame_width=_int("FRAME_WIDTH", 1280),
            cache_max_mb=_int("CACHE_MAX_MB", 2048),
            ffmpeg_concurrency=max(1, _int("FFMPEG_CONCURRENCY", 4)),
            rescan_hours=_float("RESCAN_HOURS", 6),
            max_clip_seconds=_float("MAX_CLIP_SECONDS", 15),
            gif_width=_int("GIF_WIDTH", 480),
            mp4_width=_int("MP4_WIDTH", 854),
            meme_font=_str("MEME_FONT"),
            admin_user=_str("ADMIN_USER", "admin"),
            admin_password=_str("ADMIN_PASSWORD"),
            public_url=_str("PUBLIC_URL").rstrip("/"),
        )

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"
