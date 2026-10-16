from quotarr.subtitles import Cue, choose_subtitle, clean_text, decode_bytes, describe_missing, normalize_cues, parse_srt

SRT = """1
00:00:01,000 --> 00:00:03,500
<i>Good news, everyone!</i>

2
00:00:04,000 --> 00:00:06,000
FRY: I'm gonna build my own theme park

3
00:00:09,000 --> 00:00:11,000
[LAUGHING]

4
00:00:11,500 --> 00:00:14,000
- Shut up and take my money!
- Bite my shiny metal ass.
"""


def test_parse_srt_timings_and_text():
    cues = parse_srt(SRT)
    assert len(cues) == 4
    assert (cues[0].start_ms, cues[0].end_ms) == (1000, 3500)
    assert cues[3].text.splitlines() == ["- Shut up and take my money!", "- Bite my shiny metal ass."]


def test_parse_srt_handles_crlf_and_vtt_style_times():
    cues = parse_srt("1\r\n00:01.5 --> 00:02.25\r\nHi\r\n")
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (1500, 2250, "Hi")


def test_clean_text_strips_markup_sdh_speakers_and_dashes():
    assert clean_text("<i>Good news, everyone!</i>") == "Good news, everyone!"
    assert clean_text("FRY: I'm gonna build") == "I'm gonna build"
    assert clean_text("[LAUGHING]") == ""
    assert clean_text("{\\an8}Look at me,\\NI'm the captain now.") == "Look at me,\nI'm the captain now."
    assert clean_text("- One.\n- Two.") == "One.\nTwo."
    assert clean_text("♪ Theme song ♪") == "Theme song"
    assert clean_text("MAN (V.O.): Hello") == "Hello"
    assert clean_text("AT 10:30 we go") == "AT 10:30 we go"


def test_clean_text_keeps_sdh_when_disabled():
    assert clean_text("[LAUGHING] ha", strip_sdh=False) == "[LAUGHING] ha"


def test_normalize_drops_empty_and_merges_repeats():
    cues = [Cue(0, 1000, "Hi"), Cue(1000, 2000, "Hi"), Cue(3000, 2000, "[MUSIC]"), Cue(5000, 4000, "Bye")]
    out = normalize_cues(cues)
    assert [(c.start_ms, c.end_ms, c.text) for c in out] == [(0, 2000, "Hi"), (5000, 6000, "Bye")]


def test_decode_bytes_falls_back_to_cp1252():
    assert decode_bytes("café".encode("cp1252")) == "café"
    assert decode_bytes("﻿hi".encode("utf-8")) == "hi"


def _sub(**kw):
    return {"streamType": 3, "codec": "srt", "languageCode": "eng", **kw}


def test_choose_subtitle_prefers_language_then_non_sdh_then_external():
    streams = [
        {"streamType": 1, "codec": "h264"},
        _sub(index=2, languageCode="spa"),
        _sub(index=3, hearingImpaired=True),
        _sub(index=4),
        _sub(key="/library/streams/9"),
    ]
    assert choose_subtitle(streams, ["eng", "en"])["key"] == "/library/streams/9"
    assert choose_subtitle(streams[:4], ["eng"])["index"] == 4
    assert choose_subtitle(streams[:2], ["eng"]) is None
    assert choose_subtitle(streams[:2], ["*"])["index"] == 2


def test_choose_subtitle_skips_forced_signs_and_image_tracks():
    streams = [_sub(index=2, forced=True), _sub(index=3, codec="ass", title="Signs & Songs"),
               _sub(index=4, codec="pgs"), _sub(index=5, codec="ass", title="Full")]
    assert choose_subtitle(streams, ["eng"])["index"] == 5
    assert choose_subtitle(streams[:3], ["eng"]) is None


def test_choose_subtitle_matches_language_tags_and_unknown():
    assert choose_subtitle([_sub(index=1, languageCode=None, languageTag="en-US")], ["en"])["index"] == 1
    assert choose_subtitle([_sub(index=1, languageCode="")], ["eng"])["index"] == 1


def test_describe_missing_mentions_ocr_for_image_subs():
    assert "image-based" in describe_missing([_sub(codec="pgs")])
    assert "No subtitle tracks" in describe_missing([{"streamType": 1}])
