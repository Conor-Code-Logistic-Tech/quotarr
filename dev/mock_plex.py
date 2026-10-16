"""A tiny fake Plex Media Server for testing Quotarr end to end.

Serves the handful of endpoints Quotarr uses, backed by dev/make_fixtures.sh output.
Run: FIXTURES=/fixtures uvicorn dev.mock_plex:app --port 32400
"""
from __future__ import annotations

import io
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageDraw

FIXTURES = Path(os.environ.get("FIXTURES", "/fixtures"))
PLEX_PATH_ROOT = os.environ.get("PLEX_PATH_ROOT", "/data")  # what Plex "sees" the fixtures as
TOKEN = os.environ.get("MOCK_TOKEN", "testtoken")
# Prefix every ratingKey, to simulate shows being removed and re-added to Plex.
SHIFT = os.environ.get("KEY_SHIFT", "")

app = FastAPI()


def _file(show: str, code: str) -> tuple[str, Path]:
    rel = f"TV/{show}/Season 01/{show} - {code}.mkv"
    return f"{PLEX_PATH_ROOT}/{rel}", FIXTURES / rel


def _video(trc: str | None = None) -> dict:
    s = {"id": 1, "streamType": 1, "codec": "h264", "index": 0}
    if trc:
        s |= {"codec": "hevc", "colorTrc": trc, "colorPrimaries": "bt2020"}
    return s


def _ext_sub(sid: int, lang: str, **extra) -> dict:
    return {"id": sid, "streamType": 3, "codec": "srt", "languageCode": lang, "key": f"/library/streams/{sid}", **extra}


def _emb_sub(sid: int, codec: str, index: int, lang: str = "eng", **extra) -> dict:
    return {"id": sid, "streamType": 3, "codec": codec, "index": index, "languageCode": lang, **extra}


SHOWS = {
    "100": {"title": "Test Show", "year": 2020},
    "200": {"title": "Another Show", "year": 2021},
    "300": {"title": "Not Indexed", "year": 1999},
}
EPISODES = {
    "1001": ("100", 1, 1, "Pilot", [_video(), _ext_sub(501, "spa"), _ext_sub(502, "eng")]),
    "1002": ("100", 1, 2, "Embedded SRT", [_video(), _emb_sub(503, "srt", 2), _emb_sub(504, "pgs", 3)]),
    "1003": ("100", 1, 3, "Embedded ASS", [_video(), _emb_sub(505, "ass", 2, title="English")]),
    "1004": ("100", 1, 4, "Image subs only", [_video(), _emb_sub(506, "pgs", 2)]),
    "2001": ("200", 1, 1, "Anamorphic DVD", [_video(), _emb_sub(507, "srt", 2)]),
    "2002": ("200", 1, 2, "HDR10", [_video("smpte2084"), _ext_sub(508, "eng", forced=True), _ext_sub(509, "eng")]),
}
STREAM_FILES = {501: "spanish.srt", 502: "main.srt", 508: "spanish.srt", 509: "main.srt"}


def _episode_json(key: str, detailed: bool) -> dict:
    show_key, season, index, title, streams = EPISODES[key]
    show = SHOWS[show_key]
    code = f"S{season:02d}E{index:02d}"
    plex_path, _ = _file(show["title"], code)
    _, local = _file(show["title"], code)
    part = {"id": int(key), "key": f"/library/parts/{key}/1700000000/file.mkv", "file": plex_path, "duration": 30000,
            "size": local.stat().st_size if local.exists() else 0}
    if detailed:
        part["Stream"] = streams
    return {
        "ratingKey": SHIFT + key, "type": "episode", "title": title, "grandparentTitle": show["title"],
        "parentIndex": season, "index": index, "duration": 30000,
        "Media": [{"id": int(key), "height": 1080 if "trc" in str(streams[0]) else 720, "Part": [part]}],
    }


@app.middleware("http")
async def check_token(request: Request, call_next):
    token = request.headers.get("X-Plex-Token") or request.query_params.get("X-Plex-Token")
    if token != TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


def mc(**kw) -> dict:
    return {"MediaContainer": kw}


@app.get("/")
def root():
    return mc(friendlyName="Mock Plex", version="1.40.0.0000-mock")


@app.get("/library/sections")
def sections():
    return mc(Directory=[{"key": "1", "type": "show", "title": "TV Shows"},
                         {"key": "2", "type": "movie", "title": "Movies"}])


@app.get("/library/sections/1/all")
def section_all():
    return mc(Metadata=[{"ratingKey": SHIFT + k, "type": "show", "title": v["title"], "year": v["year"],
                         "thumb": f"/library/metadata/{k}/thumb/1700000000",
                         "leafCount": sum(1 for e in EPISODES.values() if e[0] == k)} for k, v in SHOWS.items()])


@app.get("/library/metadata/{key}/allLeaves")
def all_leaves(key: str):
    key = key.removeprefix(SHIFT)
    return mc(Metadata=[_episode_json(k, False) for k, e in EPISODES.items() if e[0] == key])


@app.get("/library/metadata/{key}")
def metadata(key: str):
    key = key.removeprefix(SHIFT)
    if key not in EPISODES:
        raise HTTPException(404)
    return mc(Metadata=[_episode_json(key, True)])


@app.get("/library/streams/{sid}")
def stream(sid: int):
    if sid not in STREAM_FILES:
        raise HTTPException(404)
    return FileResponse(FIXTURES / "subs" / STREAM_FILES[sid], media_type="application/x-subrip")


@app.get("/library/parts/{key}/{ts}/file.mkv")
def part(key: str, ts: str):
    show_key, season, index, *_ = EPISODES[key]
    _, local = _file(SHOWS[show_key]["title"], f"S{season:02d}E{index:02d}")
    return FileResponse(local, media_type="video/x-matroska")


@app.get("/photo/:/transcode")
def photo(url: str, width: int = 300, height: int = 450):
    img = Image.new("RGB", (width, height), (40, 60, 120))
    ImageDraw.Draw(img).text((10, 10), url.split("/")[3], fill="white")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return Response(buf.getvalue(), media_type="image/jpeg")
