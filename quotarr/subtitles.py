"""Subtitle parsing, cleanup and track selection."""
from __future__ import annotations

import codecs
import html
import re
from dataclasses import dataclass

# Text formats ffmpeg can turn into SRT. Image formats (PGS, VobSub) would need OCR.
TEXT_CODECS = {"srt", "subrip", "ass", "ssa", "vtt", "webvtt", "mov_text", "tx3g", "text", "smi", "sami"}
IMAGE_CODECS = {"pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "dvbsub"}


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str


_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
_ARROW_RE = re.compile(rf"{_TIME}\s*-->\s*{_TIME}")


def _to_ms(h: str | None, m: str, s: str, frac: str) -> int:
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = _ARROW_RE.search(line)
            if m:
                g = m.groups()
                cues.append(Cue(_to_ms(*g[:4]), _to_ms(*g[4:]), "\n".join(lines[i + 1:])))
                break
    return cues


_ASS_TAG_RE = re.compile(r"\{[^}]*\}")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_SDH_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_SPEAKER_RE = re.compile(r"^[A-Z][A-Z0-9 .'&-]{0,30}:\s+")
_DASH_RE = re.compile(r"^[-–—]+\s*")
_DRAWING_RE = re.compile(r"^\s*m\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+[lb]\s", re.I)


def clean_text(raw: str, strip_sdh: bool = True) -> str:
    t = raw.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    t = html.unescape(_HTML_TAG_RE.sub("", _ASS_TAG_RE.sub("", t)))
    out = []
    for line in t.split("\n"):
        if _DRAWING_RE.match(line):
            continue
        line = _DASH_RE.sub("", line.strip())
        if strip_sdh:
            line = _SPEAKER_RE.sub("", _SDH_RE.sub("", line).strip())
        line = re.sub(r"\s+", " ", line.replace("♪", " ").replace("♫", " ")).strip()
        line = _DASH_RE.sub("", line)
        if re.search(r"\w", line):
            out.append(line)
    return "\n".join(out)


def normalize_cues(cues: list[Cue], strip_sdh: bool = True) -> list[Cue]:
    out: list[Cue] = []
    for cue in sorted(cues, key=lambda c: c.start_ms):
        text = clean_text(cue.text, strip_sdh)
        if not text:
            continue
        end = cue.end_ms if cue.end_ms > cue.start_ms else cue.start_ms + 1000
        prev = out[-1] if out else None
        if prev and prev.text == text and cue.start_ms - prev.end_ms < 250:
            prev.end_ms = max(prev.end_ms, end)
            continue
        out.append(Cue(cue.start_ms, end, text))
    return out


def decode_bytes(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", "replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _lang(stream: dict) -> str:
    return (stream.get("languageCode") or stream.get("languageTag") or "").lower()


def _lang_rank(lang: str, preferred: list[str]) -> int | None:
    """Lower is better; None means the language is not wanted."""
    if "*" in preferred:
        return 0
    for i, want in enumerate(preferred):
        if lang == want or lang.split("-")[0] == want:
            return i
    return len(preferred) if not lang else None


def choose_subtitle(streams: list[dict], preferred_langs: list[str]) -> dict | None:
    best, best_score = None, None
    for s in streams:
        if s.get("streamType") != 3:
            continue
        codec = (s.get("codec") or "").lower()
        title = f"{s.get('title') or ''} {s.get('displayTitle') or ''}".lower()
        if codec not in TEXT_CODECS or s.get("forced") or "forced" in title or "signs" in title:
            continue
        if not s.get("key") and s.get("index") is None:
            continue
        rank = _lang_rank(_lang(s), preferred_langs)
        if rank is None:
            continue
        hearing_impaired = bool(s.get("hearingImpaired")) or "sdh" in title
        score = (rank, hearing_impaired, not s.get("key"))
        if best_score is None or score < best_score:
            best, best_score = s, score
    return best


def describe_missing(streams: list[dict]) -> str:
    subs = [s for s in streams if s.get("streamType") == 3]
    if not subs:
        return "No subtitle tracks in Plex. Add an SRT (e.g. with Bazarr) and refresh the episode in Plex."
    codecs_found = sorted({(s.get("codec") or "?").lower() for s in subs})
    if all(c in IMAGE_CODECS for c in codecs_found):
        return f"Only image-based subtitles ({', '.join(codecs_found)}), which need OCR. Add an SRT (e.g. with Bazarr)."
    langs = sorted({_lang(s) or "unknown" for s in subs})
    return f"No usable text subtitles in the wanted languages (found {', '.join(codecs_found)} in {', '.join(langs)})."
