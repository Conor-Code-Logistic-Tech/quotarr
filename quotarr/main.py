"""FastAPI app: JSON API, image/clip endpoints and the single-page frontend."""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import Settings
from .db import Database
from .indexer import Indexer
from .media import Media, MediaError
from .meme import render_meme
from .plex import PlexClient, PlexError
from .search import code as ep_code
from .search import random_lines, search
from .subtitles import Cue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("quotarr")

STATIC = Path(__file__).parent / "static"
IMMUTABLE = {"Cache-Control": "public, max-age=604800, immutable"}
_CODE_RE = re.compile(r"^S(\d{1,4})E(\d{1,4})$", re.I)

settings = Settings.from_env()
db = Database(settings.data_dir / "quotarr.db")
plex = PlexClient(settings.plex_url, settings.plex_token, settings.plex_verify_ssl)
media = Media(settings, plex)
indexer = Indexer(settings, db, plex, media)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not media.can_tonemap:
        log.warning("ffmpeg lacks zscale/tonemap; HDR episodes will look washed out")
    indexer.start()
    yield


app = FastAPI(title="Quotarr", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- helpers
def _episode(slug: str, code: str):
    m = _CODE_RE.match(code)
    if not m:
        raise HTTPException(404, "Bad episode code, expected e.g. S01E02")
    row = db.one(
        "SELECT e.*, s.slug, s.title AS show_title FROM episodes e JOIN shows s ON s.id = e.show_id "
        "WHERE s.slug = ? AND e.season = ? AND e.episode = ? AND s.enabled = 1 ORDER BY e.id LIMIT 1",
        (slug, int(m.group(1)), int(m.group(2))))
    if not row:
        raise HTTPException(404, "Episode not found")
    return row


def _clamp_ms(ep, ms: int) -> int:
    duration = ep["duration_ms"] or 0
    return max(0, min(ms, duration - 200)) if duration > 400 else max(0, ms)


def _decode_lines(b64: str | None) -> str | None:
    if b64 is None:
        return None
    try:
        text = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(400, "b64lines must be URL-safe base64 of UTF-8 text")
    return text[:400]


def _shows_param(shows: str | None) -> list[str] | None:
    return [s for s in (shows or "").split(",") if s] or None


def _image(path: Path, download: str | None = None) -> FileResponse:
    headers = dict(IMMUTABLE)
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{download}"'
    return FileResponse(path, media_type="image/jpeg", headers=headers)


def _media_error(exc: MediaError) -> HTTPException:
    log.warning("Media error: %s", exc)
    return HTTPException(502, str(exc))


security = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not settings.admin_password:
        return
    ok = creds is not None and secrets.compare_digest(creds.username.encode(), settings.admin_user.encode()) \
        and secrets.compare_digest(creds.password.encode(), settings.admin_password.encode())
    if not ok:
        raise HTTPException(401, "Admin login required", headers={"WWW-Authenticate": 'Basic realm="Quotarr admin"'})


# ---------------------------------------------------------------- public API
@app.get("/api/shows")
def api_shows():
    rows = db.query(
        "SELECT s.slug, s.title, s.year, SUM(e.status = 'indexed') AS episodes, COALESCE(SUM(e.line_count), 0) AS lines "
        "FROM shows s LEFT JOIN episodes e ON e.show_id = s.id WHERE s.enabled = 1 GROUP BY s.id ORDER BY s.title")
    return [dict(r) | {"poster": f"/poster/{r['slug']}.jpg"} for r in rows if r["lines"]]


@app.get("/api/search")
def api_search(q: str = Query("", max_length=300), shows: str | None = None,
               limit: int = Query(48, ge=1, le=200), offset: int = Query(0, ge=0)):
    results, total = search(db, q, _shows_param(shows), limit, offset)
    return {"results": results, "total": total}


@app.get("/api/random")
def api_random(n: int = Query(12, ge=1, le=60), shows: str | None = None):
    return {"results": random_lines(db, n, _shows_param(shows))}


@app.get("/api/caption/{slug}/{code}/{ms}")
def api_caption(slug: str, code: str, ms: int):
    ep = _episode(slug, code)
    ms = _clamp_ms(ep, ms)
    lines = [dict(r) for r in db.query(
        "SELECT idx, start_ms, end_ms, text FROM lines WHERE episode_id = ? ORDER BY idx", (ep["id"],))]
    current = None
    if lines:
        current = next((i for i, l in enumerate(lines) if l["start_ms"] <= ms <= l["end_ms"]), None)
        if current is None:
            current = min(range(len(lines)), key=lambda i: min(abs(lines[i]["start_ms"] - ms), abs(lines[i]["end_ms"] - ms)))
        lo = max(0, current - 8)
        lines, current = lines[lo: current + 9], current - lo
    nearby = sorted({_clamp_ms(ep, ms + d) for d in (-2000, -1500, -1000, -500, 0, 500, 1000, 1500, 2000)})
    return {
        "show": ep["slug"], "show_title": ep["show_title"], "code": ep_code(ep["season"], ep["episode"]),
        "season": ep["season"], "episode": ep["episode"], "episode_title": ep["title"],
        "duration_ms": ep["duration_ms"], "ms": ms, "lines": lines, "current": current, "nearby": nearby,
        "max_clip_ms": int(settings.max_clip_seconds * 1000),
    }


@app.get("/thumb/{slug}/{code}/{ms:int}.jpg")
def thumb(slug: str, code: str, ms: int):
    ep = _episode(slug, code)
    try:
        return _image(media.frame(ep, _clamp_ms(ep, ms), thumb=True))
    except MediaError as exc:
        raise _media_error(exc)


@app.get("/img/{slug}/{code}/{ms:int}.jpg")
def frame(slug: str, code: str, ms: int, download: bool = False):
    ep = _episode(slug, code)
    try:
        path = media.frame(ep, _clamp_ms(ep, ms))
    except MediaError as exc:
        raise _media_error(exc)
    return _image(path, f"{slug}-{code}-{ms}.jpg" if download else None)


@app.get("/meme/{slug}/{code}/{ms:int}.jpg")
def meme(slug: str, code: str, ms: int, b64lines: str = "", download: bool = False):
    ep = _episode(slug, code)
    ms = _clamp_ms(ep, ms)
    text = (_decode_lines(b64lines) or "").strip()
    try:
        source = media.frame(ep, ms)
    except MediaError as exc:
        raise _media_error(exc)
    if not text:
        return _image(source)
    key = hashlib.sha1(f"{ep['id']}|{ms}|{text}|{settings.frame_width}".encode()).hexdigest()
    out = settings.cache_dir / "memes" / f"{key}.jpg"
    path = media.cached(out, lambda tmp: render_meme(source, text, tmp, settings.meme_font))
    return _image(path, f"{slug}-{code}-{ms}.jpg" if download else None)


@app.get("/clip/{slug}/{code}/{start:int}-{end:int}.{fmt}")
def clip(slug: str, code: str, start: int, end: int, fmt: str, b64lines: str | None = None,
         nosubs: bool = False, download: bool = False):
    if fmt not in ("gif", "mp4"):
        raise HTTPException(404, "Clips are .gif or .mp4")
    ep = _episode(slug, code)
    start, end = _clamp_ms(ep, start), _clamp_ms(ep, end)
    if end <= start:
        raise HTTPException(400, "Clip end must be after its start")
    if end - start > settings.max_clip_seconds * 1000:
        raise HTTPException(400, f"Clips are limited to {settings.max_clip_seconds:g} seconds")
    custom = _decode_lines(b64lines)
    if custom is not None and custom.strip():
        cues = [Cue(0, end - start, custom.strip())]
    elif nosubs:
        cues = []
    else:
        cues = [Cue(max(0, r["start_ms"] - start), min(end, r["end_ms"]) - start, r["text"]) for r in db.query(
            "SELECT start_ms, end_ms, text FROM lines WHERE episode_id = ? AND end_ms > ? AND start_ms < ? "
            "ORDER BY start_ms", (ep["id"], start, end))]
    try:
        path = media.clip(ep, start, end, cues, fmt)
    except MediaError as exc:
        raise _media_error(exc)
    headers = dict(IMMUTABLE)
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{slug}-{code}-{start}-{end}.{fmt}"'
    return FileResponse(path, media_type="image/gif" if fmt == "gif" else "video/mp4", headers=headers)


@app.get("/poster/{slug}.jpg")
def poster(slug: str):
    show = db.one("SELECT id, thumb FROM shows WHERE slug = ?", (slug,))
    if not show or not show["thumb"]:
        raise HTTPException(404, "No poster")
    out = settings.cache_dir / "posters" / f"{show['id']}-{hashlib.sha1(show['thumb'].encode()).hexdigest()[:10]}.jpg"
    try:
        path = media.cached(out, lambda tmp: tmp.write_bytes(plex.poster(show["thumb"])))
    except PlexError as exc:
        raise HTTPException(502, str(exc))
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- admin API
class Toggle(BaseModel):
    enabled: bool


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status():
    totals = db.one("SELECT COUNT(*) AS episodes, SUM(status = 'indexed') AS indexed, SUM(status = 'no_subs') AS no_subs, "
                    "SUM(status = 'error') AS errors, COALESCE(SUM(line_count), 0) AS lines FROM episodes e "
                    "JOIN shows s ON s.id = e.show_id WHERE s.enabled = 1")
    queued = indexer.queued()
    names = {r["id"]: r["title"] for r in db.query(
        f"SELECT id, title FROM shows WHERE id IN ({','.join('?' * len(queued))})", queued)} if queued else {}
    return {
        "version": __version__,
        "plex": {"configured": plex.configured, "url": settings.plex_url, "server": indexer.state["server"],
                 "error": indexer.state["plex_error"], "last_sync": indexer.state["last_sync"]},
        "indexer": {"current_show": indexer.state["current_show"], "current_episode": indexer.state["current_episode"],
                    "progress": indexer.state["progress"], "queued": [names.get(i, i) for i in queued],
                    "last_error": indexer.state["last_error"]},
        "totals": dict(totals),
        "disk": media.disk_usage(),
        "settings": {"pregenerate_thumbs": settings.pregenerate_thumbs, "cache_max_mb": settings.cache_max_mb,
                     "subtitle_languages": settings.subtitle_languages, "tonemap": media.can_tonemap},
    }


@app.get("/api/admin/shows", dependencies=[Depends(require_admin)])
def admin_shows():
    rows = db.query(
        "SELECT s.id, s.slug, s.title, s.year, s.library_title, s.leaf_count, s.enabled, s.last_indexed, "
        "COUNT(e.id) AS known, SUM(e.status = 'indexed') AS indexed, SUM(e.status = 'no_subs') AS no_subs, "
        "SUM(e.status = 'error') AS errors, COALESCE(SUM(e.line_count), 0) AS lines "
        "FROM shows s LEFT JOIN episodes e ON e.show_id = s.id GROUP BY s.id ORDER BY s.library_title, s.title")
    return [dict(r) | {"poster": f"/poster/{r['slug']}.jpg"} for r in rows]


@app.post("/api/admin/shows/{show_id}", dependencies=[Depends(require_admin)])
def admin_toggle(show_id: int, body: Toggle):
    if not db.one("SELECT 1 FROM shows WHERE id = ?", (show_id,)):
        raise HTTPException(404, "Unknown show")
    db.execute("UPDATE shows SET enabled = ?, user_toggled = 1 WHERE id = ?", (int(body.enabled), show_id))
    if body.enabled:
        indexer.enqueue(show_id)
    return {"ok": True}


@app.post("/api/admin/shows/{show_id}/reindex", dependencies=[Depends(require_admin)])
def admin_reindex(show_id: int):
    indexer.enqueue(show_id, force=True)
    return {"ok": True}


@app.post("/api/admin/shows/{show_id}/purge", dependencies=[Depends(require_admin)])
def admin_purge(show_id: int):
    db.execute("UPDATE shows SET enabled = 0, user_toggled = 1 WHERE id = ?", (show_id,))
    indexer.purge_show(show_id)
    return {"ok": True}


@app.get("/api/admin/shows/{show_id}/episodes", dependencies=[Depends(require_admin)])
def admin_episodes(show_id: int):
    rows = db.query("SELECT id, season, episode, title, status, sub_source, error, line_count, hdr, file_path "
                    "FROM episodes WHERE show_id = ? ORDER BY season, episode", (show_id,))
    return [dict(r) | {"code": ep_code(r["season"], r["episode"])} for r in rows]


@app.post("/api/admin/sync", dependencies=[Depends(require_admin)])
def admin_sync():
    indexer.trigger_sync()
    return {"ok": True}


# ---------------------------------------------------------------- frontend
def _index_html(request: Request, og: dict | None = None) -> HTMLResponse:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    if og:
        base = settings.public_url or str(request.base_url).rstrip("/")
        og["og:image"] = base + og["og:image"]
        tags = "\n".join(f'    <meta property="{k}" content="{html.escape(v)}">' for k, v in og.items())
        page = page.replace("<!--og-->", tags + '\n    <meta name="twitter:card" content="summary_large_image">')
    return HTMLResponse(page)


@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
def index(request: Request):
    return _index_html(request)


@app.get("/caption/{slug}/{code}/{ms}", response_class=HTMLResponse)
def caption_page(request: Request, slug: str, code: str, ms: int):
    try:
        ep = _episode(slug, code)
    except HTTPException:
        return _index_html(request)
    line = db.one("SELECT text FROM lines WHERE episode_id = ? AND start_ms <= ? ORDER BY start_ms DESC LIMIT 1",
                  (ep["id"], ms))
    return _index_html(request, {
        "og:title": f"{ep['show_title']} {code.upper()}" + (f" - {ep['title']}" if ep["title"] else ""),
        "og:description": line["text"].replace("\n", " ") if line else "",
        "og:image": f"/img/{slug}/{code.upper()}/{ms}.jpg",
        "og:type": "website",
    })


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/thumb/", "/img/", "/meme/", "/clip/", "/poster/", "/static/")):
        detail = getattr(exc, "detail", "Not found")
        return JSONResponse({"detail": detail}, status_code=404)
    return _index_html(request)


@app.get("/healthz")
def healthz():
    return Response("ok", media_type="text/plain")
