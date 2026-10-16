"""Full-text search over subtitle lines."""
from __future__ import annotations

import re
from typing import Sequence

from .db import Database

_WORD_RE = re.compile(r"\w+", re.UNICODE)
CANDIDATES_PER_QUERY = 300

_SELECT = """
SELECT l.id, l.episode_id, l.idx, l.start_ms, l.end_ms, l.text,
       e.season, e.episode, e.title AS episode_title,
       s.slug, s.title AS show_title
FROM lines l
JOIN episodes e ON e.id = l.episode_id
JOIN shows s ON s.id = e.show_id
"""


def fts_queries(q: str) -> list[str]:
    """FTS5 queries from strictest to loosest: exact phrase, all words, any word."""
    words = _WORD_RE.findall(q.lower())[:16]
    if not words:
        return []
    *head, last = words
    all_words = " AND ".join([f'"{w}"' for w in head] + [f'("{last}" OR "{last}"*)'])
    queries = ['"' + " ".join(words) + '"', all_words]
    if head:
        queries.append(" OR ".join(f'"{w}"' for w in words))
    return queries


def _show_filter(shows: Sequence[str] | None) -> tuple[str, list[str]]:
    if not shows:
        return "", []
    return f" AND s.slug IN ({','.join('?' * len(shows))})", list(shows)


def code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def result_dict(r) -> dict:
    ms = (r["start_ms"] + r["end_ms"]) // 2
    c = code(r["season"], r["episode"])
    return {
        "show": r["slug"], "show_title": r["show_title"], "code": c,
        "episode_title": r["episode_title"], "ms": ms, "start_ms": r["start_ms"], "end_ms": r["end_ms"],
        "text": r["text"], "thumb": f"/thumb/{r['slug']}/{c}/{ms}.jpg",
    }


def search(db: Database, q: str, shows: Sequence[str] | None = None, limit: int = 48,
           offset: int = 0) -> tuple[list[dict], int]:
    show_sql, show_params = _show_filter(shows)
    sql = (_SELECT.replace("FROM lines l", "FROM lines_fts JOIN lines l ON l.id = lines_fts.rowid")
           + f" WHERE lines_fts MATCH ? AND s.enabled = 1{show_sql}"
           + " ORDER BY bm25(lines_fts, 4.0, 1.0) LIMIT ?")
    seen: set[int] = set()
    taken: dict[int, set[int]] = {}
    results = []
    for i, fq in enumerate(fts_queries(q)):
        if i == 2 and len(results) >= 12:
            break  # only fall back to "any word" when the stricter queries found little
        for r in db.query(sql, [fq, *show_params, CANDIDATES_PER_QUERY]):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            used = taken.setdefault(r["episode_id"], set())
            if used & {r["idx"] - 1, r["idx"], r["idx"] + 1}:
                continue  # neighbouring cue of a better match in the same episode
            used.add(r["idx"])
            results.append(r)
    return [result_dict(r) for r in results[offset:offset + limit]], len(results)


def random_lines(db: Database, n: int = 12, shows: Sequence[str] | None = None) -> list[dict]:
    show_sql, show_params = _show_filter(shows)
    rows = db.query(
        _SELECT + f" WHERE s.enabled = 1 AND length(l.text) > 12{show_sql} ORDER BY random() LIMIT ?",
        [*show_params, n])
    return [result_dict(r) for r in rows]
