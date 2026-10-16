"""Background worker: syncs shows from Plex and indexes subtitles for enabled shows."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import Settings
from .db import Database
from .media import Media, MediaError
from .plex import PlexClient, PlexError
from .subtitles import choose_subtitle, describe_missing, normalize_cues, parse_srt

log = logging.getLogger(__name__)

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "show"


def _first_part(item: dict) -> tuple[dict | None, dict | None]:
    media = _choose_media(item.get("Media") or [])
    parts = (media or {}).get("Part") or []
    return media, (parts[0] if parts else None)


def _choose_media(media: list[dict]) -> dict | None:
    """Prefer a ~1080p version over 4K when Plex has several versions of an episode."""
    def score(m: dict) -> tuple:
        height = int(m.get("height") or 0)
        return (str(m.get("videoResolution")).lower() == "4k", abs(height - 1080) if height else 10_000)
    return min(media, key=score) if media else None


class Indexer:
    def __init__(self, settings: Settings, db: Database, plex: PlexClient, media: Media):
        self.settings = settings
        self.db = db
        self.plex = plex
        self.media = media
        self._queue: queue.Queue[int] = queue.Queue()
        self._pending: dict[int, bool] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.state: dict[str, Any] = {
            "current_show": None, "current_episode": None, "progress": [0, 0],
            "last_sync": None, "plex_error": None, "last_error": None, "server": None,
        }

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        threading.Thread(target=self._worker, name="indexer", daemon=True).start()
        threading.Thread(target=self._scheduler, name="scheduler", daemon=True).start()

    def enqueue(self, show_id: int, force: bool = False) -> None:
        with self._lock:
            if show_id in self._pending:
                self._pending[show_id] = self._pending[show_id] or force
                return
            self._pending[show_id] = force
        self._queue.put(show_id)

    def trigger_sync(self) -> None:
        self._wake.set()

    def queued(self) -> list[int]:
        with self._lock:
            return list(self._pending)

    # ---------------------------------------------------------------- threads
    def _scheduler(self) -> None:
        while True:
            delay = self.settings.rescan_hours * 3600
            try:
                self.sync_library()
                for row in self.db.query("SELECT id FROM shows WHERE enabled = 1 ORDER BY title"):
                    self.enqueue(row["id"])
            except PlexError as exc:
                self.state["plex_error"] = str(exc)
                log.warning("Plex sync failed: %s", exc)
                delay = 60
            except Exception:
                log.exception("Library sync crashed")
                delay = 300
            try:
                self.media.prune_cache()
            except Exception:
                log.exception("Cache prune failed")
            self._wake.wait(delay)
            self._wake.clear()

    def _worker(self) -> None:
        while True:
            show_id = self._queue.get()
            with self._lock:
                force = self._pending.pop(show_id, False)
            try:
                self.index_show(show_id, force)
            except PlexError as exc:
                self.state["plex_error"] = str(exc)
                log.warning("Indexing show %s failed: %s", show_id, exc)
            except Exception as exc:
                self.state["last_error"] = str(exc)
                log.exception("Indexing show %s crashed", show_id)
            finally:
                self.state.update(current_show=None, current_episode=None, progress=[0, 0])

    # ---------------------------------------------------------------- library
    def sync_library(self) -> None:
        info = self.plex.server_info()
        now = time.time()
        auto = {t.lower() for t in self.settings.auto_index_shows}
        found = [(lib, show) for lib in self.plex.show_libraries() for show in self.plex.shows(lib["key"])]
        live_keys = {str(show["ratingKey"]) for _, show in found}
        for lib, show in found:
            self._upsert_show(lib, show, auto, now, live_keys)
        self.state.update(server=info, plex_error=None, last_sync=now)

    def _upsert_show(self, lib: dict, show: dict, auto: set[str], now: float, live_keys: set[str]) -> None:
        key = str(show["ratingKey"])
        title = show.get("title") or "Untitled"
        wanted = title.lower() in auto or f"{title} ({show.get('year')})".lower() in auto
        with self.db.tx() as conn:
            row = conn.execute("SELECT id, user_toggled FROM shows WHERE plex_key = ?", (key,)).fetchone()
            if row is None:
                # A show that was removed and re-added in Plex gets a new ratingKey. Re-link it so its
                # slug (and every shared link) stays the same; its episodes are re-matched on next index.
                for old in conn.execute("SELECT id, plex_key, user_toggled FROM shows WHERE title = ? AND "
                                        "year IS ?", (title, show.get("year"))).fetchall():
                    if old["plex_key"] not in live_keys:
                        conn.execute("UPDATE shows SET plex_key = ? WHERE id = ?", (key, old["id"]))
                        row = old
                        break
            values = (str(lib["key"]), lib.get("title"), title, show.get("year"), show.get("thumb"),
                      int(show.get("leafCount") or 0), now)
            if row:
                conn.execute(
                    "UPDATE shows SET library_key=?, library_title=?, title=?, year=?, thumb=?, leaf_count=?, "
                    "last_seen=? WHERE id=?", (*values, row["id"]))
                if wanted and not row["user_toggled"]:
                    conn.execute("UPDATE shows SET enabled = 1 WHERE id = ?", (row["id"],))
            else:
                conn.execute(
                    "INSERT INTO shows(library_key, library_title, title, year, thumb, leaf_count, last_seen, "
                    "plex_key, slug, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, key, self._unique_slug(conn, title, show.get("year")), int(wanted)))

    @staticmethod
    def _unique_slug(conn, title: str, year: Any) -> str:
        base = slugify(title)
        candidates = [base] + ([f"{base}-{year}"] if year else []) + [f"{base}-{n}" for n in range(2, 100)]
        for slug in candidates:
            if not conn.execute("SELECT 1 FROM shows WHERE slug = ?", (slug,)).fetchone():
                return slug
        raise RuntimeError(f"Could not find a free slug for {title!r}")

    # ---------------------------------------------------------------- shows
    def index_show(self, show_id: int, force: bool = False) -> None:
        show = self.db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
        if not show or not show["enabled"]:
            return
        self.state.update(current_show=show["title"], current_episode=None, progress=[0, 0])
        plex_eps = self.plex.episodes(show["plex_key"])
        existing = {r["plex_key"]: r for r in self.db.query("SELECT * FROM episodes WHERE show_id = ?", (show_id,))}
        live = {str(item["ratingKey"]) for item in plex_eps}
        # Episodes whose Plex key vanished (show re-added, episode re-matched) can be re-linked by number.
        orphans = {(r["season"], r["episode"]): r for k, r in existing.items() if k not in live}
        todo: list[int] = []
        with self.db.tx() as conn:
            for item in plex_eps:
                key = str(item["ratingKey"])
                _, part = _first_part(item)
                part = part or {}
                fields = (int(item.get("parentIndex") or 0), int(item.get("index") or 0), item.get("title"),
                          item.get("duration"), part.get("key"), part.get("file"), part.get("size"))
                row = existing.pop(key, None)
                if row is None and (orphan := orphans.pop(fields[:2], None)):
                    existing.pop(orphan["plex_key"], None)
                    conn.execute("UPDATE episodes SET plex_key = ? WHERE id = ?", (key, orphan["id"]))
                    row = orphan
                if row is None:
                    cur = conn.execute(
                        "INSERT INTO episodes(season, episode, title, duration_ms, part_key, file_path, file_size, "
                        "plex_key, show_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*fields, key, show_id))
                    todo.append(cur.lastrowid)
                    continue
                # Compare the file itself, not the part key, which changes whenever Plex re-adds an item.
                changed = (row["file_path"], row["file_size"]) != (part.get("file"), part.get("size"))
                conn.execute("UPDATE episodes SET season=?, episode=?, title=?, duration_ms=?, part_key=?, "
                             "file_path=?, file_size=? WHERE id=?", (*fields, row["id"]))
                if force or changed or row["status"] != "indexed":
                    todo.append(row["id"])
        for gone in existing.values():
            self._remove_episode(gone["id"])

        for n, ep_id in enumerate(todo, 1):
            ep = self.db.one("SELECT * FROM episodes WHERE id = ?", (ep_id,))
            self.state.update(current_episode=f"S{ep['season']:02d}E{ep['episode']:02d} {ep['title'] or ''}".strip(),
                              progress=[n, len(todo)])
            current = self.db.one("SELECT enabled FROM shows WHERE id = ?", (show_id,))
            if not current or not current["enabled"]:
                log.info("Show %s was disabled mid-index; stopping", show["title"])
                return
            self.index_episode(ep_id)
        self.db.execute("UPDATE shows SET last_indexed = ? WHERE id = ?", (time.time(), show_id))
        log.info("Indexed %s (%d episodes checked)", show["title"], len(todo))

    def _remove_episode(self, episode_id: int) -> None:
        self.db.delete_episode_lines("id = ?", (episode_id,))
        self.db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        self.media.remove_thumbs(episode_id)

    def purge_show(self, show_id: int) -> None:
        self.db.delete_episode_lines("show_id = ?", (show_id,))
        for row in self.db.query("SELECT id FROM episodes WHERE show_id = ?", (show_id,)):
            self.media.remove_thumbs(row["id"])
        self.db.execute("UPDATE episodes SET status='pending', line_count=0, sub_source=NULL, error=NULL "
                        "WHERE show_id = ?", (show_id,))

    # ---------------------------------------------------------------- episodes
    def index_episode(self, episode_id: int) -> None:
        ep = self.db.one("SELECT * FROM episodes WHERE id = ?", (episode_id,))
        try:
            status, source, detail, count = self._index_episode(ep)
        except (PlexError, MediaError) as exc:
            status, source, detail, count = "error", None, str(exc), 0
            log.warning("%s: %s", self.state.get("current_episode"), exc)
        self.db.execute("UPDATE episodes SET status=?, sub_source=?, error=?, line_count=?, indexed_at=? WHERE id=?",
                        (status, source, detail, count, time.time(), episode_id))

    def _index_episode(self, ep) -> tuple[str, str | None, str | None, int]:
        meta = self.plex.metadata(ep["plex_key"])
        if not meta:
            raise PlexError("Episode no longer exists in Plex")
        _, part = _first_part(meta)
        if not part:
            raise PlexError("Plex has no media file for this episode")
        streams = part.get("Stream") or []
        hdr = any(s.get("streamType") == 1 and str(s.get("colorTrc", "")).lower() in HDR_TRANSFERS for s in streams)
        self.db.execute("UPDATE episodes SET part_key=?, file_path=?, hdr=?, duration_ms=COALESCE(?, duration_ms) "
                        "WHERE id=?", (part.get("key"), part.get("file"), int(hdr), meta.get("duration"), ep["id"]))
        ep = self.db.one("SELECT * FROM episodes WHERE id = ?", (ep["id"],))

        stream = choose_subtitle(streams, self.settings.subtitle_languages)
        if stream is None:
            self.db.delete_episode_lines("id = ?", (ep["id"],))
            return "no_subs", None, describe_missing(streams), 0

        codec = (stream.get("codec") or "srt").lower()
        lang = stream.get("languageCode") or "und"
        srt, source = None, None
        if stream.get("key"):
            try:
                srt = self.media.convert_subtitle(self.plex.fetch(stream["key"]), codec)
                source = f"plex {codec} ({lang})"
            except (PlexError, MediaError):
                if stream.get("index") is None:
                    raise
        if srt is None:
            srt = self.media.extract_subtitle(ep, int(stream["index"]))
            source = f"embedded {codec} ({lang})"

        cues = normalize_cues(parse_srt(srt), self.settings.strip_sdh)
        if not cues:
            self.db.delete_episode_lines("id = ?", (ep["id"],))
            return "no_subs", source, "The subtitle track was empty after cleanup.", 0
        self.db.replace_lines(ep["id"], cues)
        if self.settings.pregenerate_thumbs:
            self._pregenerate(ep, cues)
        return "indexed", source, None, len(cues)

    def _pregenerate(self, ep, cues) -> None:
        def one(cue):
            try:
                self.media.frame(ep, (cue.start_ms + cue.end_ms) // 2, thumb=True)
            except MediaError as exc:
                log.debug("Thumb failed: %s", exc)
        with ThreadPoolExecutor(self.settings.ffmpeg_concurrency) as pool:
            list(pool.map(one, cues))
