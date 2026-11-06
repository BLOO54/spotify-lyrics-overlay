"""Unit tests for the parts of the app that don't need Windows or Qt.

All lyrics below are invented placeholder text written for testing.
Run with:  python -m pytest -q
"""

import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clock import PlaybackClock, PlaybackReport  # noqa: E402
from app.config import Settings  # noqa: E402
from app import lrc  # noqa: E402
from app.lyrics_provider import (LrclibClient, LyricsNetworkError,  # noqa: E402
                                 clean_title, primary_artist)

LINE_LRC = """[ar:Test Band]
[ti:Test Song]
[00:01.00]neon lights are glowing
[00:04.50]over the quiet city
[00:08.00]
[00:10.25]we keep on moving forward
"""

WORD_LRC = """[00:02.00]<00:02.00>hello <00:02.50>bright <00:03.10>morning <00:04.00>
[00:05.00]<00:05.00>see <00:05.40>you <00:05.80>soon <00:06.60>
"""


# ---------------------------------------------------------------- LRC parsing
def test_parse_line_lrc():
    lines = lrc.parse_lrc(LINE_LRC)
    assert [round(l.start, 2) for l in lines] == [1.0, 4.5, 8.0, 10.25]
    assert lines[0].text == "neon lights are glowing"
    assert lines[0].end == 4.5
    assert lines[2].is_break
    assert lines[-1].end == 10.25 + lrc.LAST_LINE_MAX
    assert not any(l.word_timed for l in lines)


def test_parse_word_lrc():
    lines = lrc.parse_lrc(WORD_LRC)
    assert len(lines) == 2
    first = lines[0]
    assert first.word_timed
    assert [w.text for w in first.words] == ["hello", "bright", "morning"]
    assert first.words[0].start == 2.0 and first.words[0].end == 2.5
    assert first.words[2].end == 4.0
    assert first.text == "hello bright morning"


def test_timestamp_formats_and_repeats():
    text = "[00:05]alpha\n[00:07:50]beta\n[00:20.123][00:40.1]chorus line\n"
    lines = lrc.parse_lrc(text)
    starts = [round(l.start, 3) for l in lines]
    assert starts == [5.0, 7.5, 20.123, 40.1]
    assert lines[2].text == lines[3].text == "chorus line"


def test_offset_tag():
    lines = lrc.parse_lrc("[offset:+500]\n[00:10.00]shifted\n")
    assert lines[0].start == 9.5


def test_garbage_input_does_not_crash():
    assert lrc.parse_lrc("") == []
    assert lrc.parse_lrc("no timestamps here\n[[[broken") == []


def test_build_lyrics_estimates_words():
    ly = lrc.build_lyrics(LINE_LRC, estimate_words=True)
    assert ly.synced and not ly.has_word_timing
    words = ly.lines[0].words
    assert [w.text for w in words] == ["neon", "lights", "are", "glowing"]
    # words are in order, contiguous and inside the line
    assert words[0].start == ly.lines[0].start
    for a, b in zip(words, words[1:]):
        assert abs(a.end - b.start) < 1e-9
    assert words[-1].end <= ly.lines[0].end


def test_build_lyrics_no_estimate_is_line_mode():
    ly = lrc.build_lyrics(LINE_LRC, estimate_words=False)
    assert ly.lines[0].words == []
    assert lrc.word_state(ly.lines[0], 2.0).index == -1


def test_line_and_word_lookup():
    ly = lrc.build_lyrics(WORD_LRC)
    assert ly.line_index_at(0.5) == -1
    assert ly.line_index_at(2.0) == 0
    assert ly.line_index_at(5.5) == 1
    st = lrc.word_state(ly.lines[0], 2.75)
    assert st.index == 1 and 0.4 < st.progress < 0.5
    st = lrc.word_state(ly.lines[0], 4.5)   # after last word
    assert st.index == 2 and st.progress == 1.0


def test_plain_only_lyrics():
    ly = lrc.build_lyrics(None, "just text\nno timing")
    assert not ly.synced and ly.plain_text.startswith("just text")


# ---------------------------------------------------------------- clock
class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.utc = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def mono(self):
        return self.t

    def now(self):
        return self.utc

    def advance(self, s):
        self.t += s
        self.utc += timedelta(seconds=s)


def test_clock_extrapolates_while_playing():
    ft = FakeTime()
    c = PlaybackClock(ft.mono, ft.now)
    c.update(PlaybackReport(10.0, True, ft.utc, 200))
    ft.advance(2.0)
    assert abs(c.position() - 12.0) < 1e-6
    # player keeps sending the *same* stale report: we keep counting
    c.update(PlaybackReport(10.0, True, ft.utc - timedelta(seconds=2), 200))
    ft.advance(1.0)
    assert abs(c.position() - 13.0) < 1e-6


def test_clock_uses_last_updated_time():
    ft = FakeTime()
    c = PlaybackClock(ft.mono, ft.now)
    # Spotify reported 30s, but that report is 5 s old -> we should be at 35 s
    c.update(PlaybackReport(30.0, True, ft.utc - timedelta(seconds=5), 200))
    assert abs(c.position() - 35.0) < 1e-6


def test_clock_pause_and_seek():
    ft = FakeTime()
    c = PlaybackClock(ft.mono, ft.now)
    c.update(PlaybackReport(10.0, True, ft.utc, 200))
    ft.advance(3)
    c.update(PlaybackReport(13.0, False, ft.utc, 200))   # paused
    ft.advance(10)
    assert abs(c.position() - 13.0) < 1e-6              # frozen while paused
    c.update(PlaybackReport(60.0, True, ft.utc, 200))    # resumed after a seek
    ft.advance(1)
    assert abs(c.position() - 61.0) < 1e-6


def test_clock_ignores_tiny_drift_and_bad_timestamps():
    ft = FakeTime()
    c = PlaybackClock(ft.mono, ft.now)
    c.update(PlaybackReport(10.0, True, ft.utc, 200))
    ft.advance(1.0)
    c.update(PlaybackReport(11.1, True, ft.utc, 200))    # 0.1 s off -> no jump
    assert abs(c.position() - 11.0) < 1e-6
    # year-1601 style timestamp must be ignored, not added as centuries
    c.update(PlaybackReport(50.0, True, datetime(1601, 1, 1, tzinfo=timezone.utc), 200))
    assert abs(c.position() - 50.0) < 1e-6


def test_clock_clamps_to_duration():
    ft = FakeTime()
    c = PlaybackClock(ft.mono, ft.now)
    c.update(PlaybackReport(195.0, True, ft.utc, 200))
    ft.advance(30)
    assert c.position() == 200


# ---------------------------------------------------------------- titles
def test_clean_title():
    assert clean_title("Night Drive - Remastered 2011") == "Night Drive"
    assert clean_title("Night Drive (feat. Someone)") == "Night Drive"
    assert clean_title("Night Drive - Live at Somewhere") == "Night Drive"
    assert clean_title("Night Drive (2009 Remaster)") == "Night Drive"
    assert clean_title("Plain Title") == "Plain Title"
    assert clean_title("Half - Half") == "Half - Half"   # don't over-strip


def test_primary_artist():
    assert primary_artist("Alpha, Beta") == "Alpha"
    assert primary_artist("Alpha & Beta") == "Alpha"
    assert primary_artist("Alpha feat. Beta") == "Alpha"
    assert primary_artist("Solo") == "Solo"


# ---------------------------------------------------------------- LRCLIB client
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(routes, calls):
    def opener(req, timeout=None):
        url = req.full_url
        calls.append(url)
        assert "User-Agent" in req.headers or "User-agent" in req.headers
        for prefix, payload in routes:
            if prefix in url:
                if isinstance(payload, Exception):
                    raise payload
                if payload == 404:
                    raise urllib.error.HTTPError(url, 404, "nf", {}, None)
                return FakeResp(json.dumps(payload).encode())
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    return opener


def test_client_exact_hit_and_cache(tmp_path):
    calls = []
    routes = [("/api/get", {"syncedLyrics": "[00:01.00]la la", "plainLyrics": "la la", "instrumental": False})]
    c = LrclibClient(tmp_path, make_opener(routes, calls))
    r = c.fetch("Song", "Artist", "Album", 180)
    assert r.synced == "[00:01.00]la la"
    n = len(calls)
    r2 = c.fetch("Song", "Artist", "Album", 180)   # from disk cache
    assert r2.synced == r.synced and len(calls) == n


def test_client_falls_back_to_search(tmp_path):
    calls = []
    routes = [
        ("/api/get", 404),
        ("/api/search", [
            {"trackName": "Other", "artistName": "Nobody", "duration": 180, "syncedLyrics": "[00:01.00]wrong"},
            {"trackName": "Song", "artistName": "Artist", "duration": 181, "syncedLyrics": "[00:01.00]right"},
            {"trackName": "Song", "artistName": "Artist", "duration": 300, "syncedLyrics": "[00:01.00]other cut"},
        ]),
    ]
    c = LrclibClient(tmp_path, make_opener(routes, calls))
    r = c.fetch("Song - Remastered", "Artist, Friend", "", 180)
    assert r.synced == "[00:01.00]right"


def test_client_not_found(tmp_path):
    c = LrclibClient(tmp_path, make_opener([("/api/search", [])], []))
    r = c.fetch("Unknown", "Nobody", "", 100)
    assert not r.found


def test_client_offline_raises(tmp_path):
    c = LrclibClient(tmp_path, make_opener([("lrclib", urllib.error.URLError("no internet"))], []))
    try:
        c.fetch("Song", "Artist", "", 100)
        assert False, "should raise"
    except LyricsNetworkError:
        pass


def test_client_instrumental(tmp_path):
    routes = [("/api/get", {"syncedLyrics": None, "plainLyrics": None, "instrumental": True})]
    c = LrclibClient(tmp_path, make_opener(routes, []))
    assert c.fetch("Song", "Artist", "", 100).instrumental


# ---------------------------------------------------------------- click-through styles
def test_click_through_style_bits():
    from app import win32
    base = 0x00000100  # some unrelated bit Qt might have set
    on = win32.desired_exstyle(base, True)
    assert on & win32.WS_EX_TRANSPARENT and on & win32.WS_EX_LAYERED and on & base
    assert on & win32.WS_EX_NOACTIVATE and on & win32.WS_EX_TOOLWINDOW
    off = win32.desired_exstyle(on, False)
    assert not off & win32.WS_EX_TRANSPARENT
    assert off & win32.WS_EX_LAYERED          # never strip Qt's layered bit
    assert not win32.desired_exstyle(win32.WS_EX_APPWINDOW, False) & win32.WS_EX_APPWINDOW


# ---------------------------------------------------------------- settings
def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    s = Settings.load()
    s.font_size = 44
    s.highlight_color = "#123456"
    s.save()
    s2 = Settings.load()
    assert s2.font_size == 44 and s2.highlight_color == "#123456"


def test_settings_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Settings.path().write_text("{not json", encoding="utf-8")
    s = Settings.load()
    assert s.font_size == Settings().font_size


def test_settings_bad_values_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Settings.path().write_text(json.dumps({"font_size": 9999, "position": "moon", "bold": "yes"}), encoding="utf-8")
    s = Settings.load()
    assert s.font_size == 120 and s.position == "bottom-center" and s.bold is True
