from quotarr.db import Database
from quotarr.media import build_srt, map_path
from quotarr.search import fts_queries, search
from quotarr.subtitles import Cue, parse_srt


def make_db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.execute("INSERT INTO shows(id, plex_key, slug, title, enabled) VALUES (1, 'a', 'futurama', 'Futurama', 1)")
    db.execute("INSERT INTO shows(id, plex_key, slug, title, enabled) VALUES (2, 'b', 'other', 'Other', 1)")
    db.execute("INSERT INTO shows(id, plex_key, slug, title, enabled) VALUES (3, 'c', 'off', 'Off', 0)")
    for ep_id, show_id in ((10, 1), (20, 2), (30, 3)):
        db.execute("INSERT INTO episodes(id, plex_key, show_id, season, episode, status) VALUES (?, ?, ?, 1, 1, 'indexed')",
                   (ep_id, str(ep_id), show_id))
    db.replace_lines(10, [
        Cue(1000, 2000, "I'm gonna build my own theme park"),
        Cue(2100, 3000, "with blackjack and hookers."),
        Cue(4000, 5000, "In fact, forget the park."),
        Cue(6000, 7000, "Running is the best."),
    ])
    db.replace_lines(20, [Cue(0, 1000, "Blackjack is a card game.")])
    db.replace_lines(30, [Cue(0, 1000, "blackjack hidden show")])
    return db


def test_fts_queries_quote_terms_and_prefix_last_word():
    assert fts_queries("Don't!") == ['"don t"', '"don" AND ("t" OR "t"*)', '"don" OR "t"']
    assert fts_queries("park") == ['"park"', '("park" OR "park"*)']
    assert fts_queries('"; DROP') == ['"drop"', '("drop" OR "drop"*)']
    assert fts_queries("!!!") == []


def test_phrase_spanning_two_cues_matches_first_cue(tmp_path):
    db = make_db(tmp_path)
    results, total = search(db, "theme park with blackjack")
    assert results[0]["text"].startswith("I'm gonna build")
    assert results[0]["ms"] == 1500


def test_search_dedupes_adjacent_cues_and_hides_disabled_shows(tmp_path):
    db = make_db(tmp_path)
    results, total = search(db, "blackjack")
    shows = sorted(r["show"] for r in results)
    assert shows == ["futurama", "other"]
    assert total == 2


def test_search_show_filter_stemming_and_prefix(tmp_path):
    db = make_db(tmp_path)
    assert [r["show"] for r in search(db, "blackjack", ["other"])[0]] == ["other"]
    assert search(db, "run")[0][0]["text"] == "Running is the best."
    assert search(db, "blackj")[1] == 2
    assert search(db, "")[1] == 0


def test_map_path_and_build_srt_roundtrip():
    assert map_path("/data/TV/x.mkv", []) == "/data/TV/x.mkv"
    assert map_path("/tv/Show/x.mkv", [("/tv", "/media/tv")]) == "/media/tv/Show/x.mkv"
    assert map_path("D:\\TV\\Show\\x.mkv", [("D:\\TV", "/tv")]) == "/tv/Show/x.mkv"
    cues = [Cue(0, 1500, "One"), Cue(3_723_004, 3_724_000, "Two\nlines")]
    assert [(c.start_ms, c.end_ms, c.text) for c in parse_srt(build_srt(cues))] == \
        [(0, 1500, "One"), (3_723_004, 3_724_000, "Two\nlines")]
