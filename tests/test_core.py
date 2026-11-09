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


def test_clock_catches_up_smoothly_instead_of_jumping():
    ft = FakeTime()
    c = PlaybackClock(ft.mono, ft.now)
    c.update(PlaybackReport(10.0, True, ft.utc, 200))
    ft.advance(1.0)
    c.update(PlaybackReport(11.9, True, ft.utc, 200))    # Spotify says we're 0.9 s behind
    assert abs(c.position() - 11.0) < 1e-6              # no jump right now...
    last = c.position()
    for _ in range(40):                                  # ...it moves forward a little faster
        ft.advance(0.1)
        pos = c.position()
        assert 0.1 * 0.66 <= pos - last <= 0.1 * 1.34 + 1e-9, pos - last
        last = pos
    assert abs(c.position() - (11.9 + 4.0)) < 1e-6       # and is exactly right afterwards
    c.update(PlaybackReport(100.0, True, ft.utc, 200))   # a seek still jumps straight there
    assert abs(c.position() - 100.0) < 1e-6


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


def test_lines_shown_setting_and_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert Settings().lines_shown == 3
    # someone upgrading from 1.0.0 who had turned the extra lines off
    Settings.path().write_text(json.dumps({"show_context_lines": False}), encoding="utf-8")
    assert Settings.load().lines_shown == 1
    Settings.path().write_text(json.dumps({"lines_shown": 2}), encoding="utf-8")
    assert Settings.load().lines_shown == 2
    Settings.path().write_text(json.dumps({"lines_shown": 9}), encoding="utf-8")
    assert Settings.load().lines_shown == 3


# ---------------------------------------------------------------- wrapping (invented text)
def test_split_tokens_latin_and_japanese():
    assert lrc.split_tokens("hello bright  world") == [("hello", True), ("bright", True), ("world", True)]
    toks = lrc.split_tokens("君の名前を呼んでいる、ずっと")
    assert "".join(t for t, _ in toks) == "君の名前を呼んでいる、ずっと"
    assert all(not gap for _, gap in toks[:-1]) and toks[-1][1]
    assert ("る、", False) in toks              # punctuation stays attached
    assert ("ょ" not in [t for t, _ in lrc.split_tokens("きょう")])  # small kana attached
    mixed = lrc.split_tokens("Oh yeah 一緒に")
    assert mixed[:2] == [("Oh", True), ("yeah", True)]


def test_estimated_japanese_words_have_no_spaces():
    ly = lrc.build_lyrics("[00:01.00]夜空に光る\n[00:05.00]next line\n")
    w = ly.lines[0].words
    assert len(w) == 5 and lrc.join_words(w) == "夜空に光る"
    assert w[0].start == 1.0 and w[-1].end <= 5.0


def test_enhanced_lrc_gaps():
    lines = lrc.parse_lrc("[00:01.00]<00:01.00>夜<00:01.50>空<00:02.00>\n[00:03.00]<00:03.00>hi <00:03.50>there <00:04.00>\n")
    assert lrc.join_words(lines[0].words) == "夜空" and lines[0].text == "夜空"
    assert lines[1].text == "hi there"


def test_wrap_rows():
    widths = [50, 50, 50, 50]
    assert lrc.wrap_rows(widths, [True] * 4, 10, 1000) == [[0, 1, 2, 3]]
    assert lrc.wrap_rows(widths, [True] * 4, 10, 115) == [[0, 1], [2, 3]]
    assert lrc.wrap_rows(widths, [False] * 4, 10, 100) == [[0, 1], [2, 3]]   # no spaces between CJK
    assert lrc.wrap_rows([500, 20], [True, True], 10, 100) == [[0], [1]]     # over-wide piece gets own row
    assert lrc.wrap_rows([], [], 10, 100) == []


def test_timed_copy_follows_original_timing():
    # original line with real word timing: sung from 2.0 s to 4.0 s, next line at 6.0 s
    lines = lrc.parse_lrc("[00:02.00]<00:02.00>夜空に <00:03.00>光る <00:04.00>\n[00:06.00]next\n")
    tr = lrc.timed_copy(lines[0], "Shining in the night sky")
    assert [w.text for w in tr.words] == ["Shining", "in", "the", "night", "sky"]
    assert tr.words[0].start == 2.0
    assert abs(tr.words[-1].end - 4.0) < 1e-6          # same singing span as the original words
    assert tr.start == lines[0].start and tr.end == lines[0].end
    # original without word timing: falls back to a normal estimate inside the line
    plain = lrc.parse_lrc("[00:10.00]君の名前を\n[00:14.00]x\n")[0]
    tr2 = lrc.timed_copy(plain, "Your name")
    assert tr2.words[0].start == 10.0 and tr2.words[-1].end <= 14.0


# ---------------------------------------------------------------- wrong-version lyrics (invented text)
WRONG = "[00:00.70]first line\n[02:00.00]middle\n[04:08.61]too late\n[04:11.14]\n"   # runs past a 4:05 song
RIGHT = "[00:03.20]first line\n[02:02.50]middle\n[04:01.00]last line\n"


def test_fits_song():
    from app.lyrics_provider import fits_song, lyrics_end
    assert abs(lyrics_end(WRONG) - 251.14) < 1e-6
    assert not fits_song(WRONG, 245)
    assert fits_song(RIGHT, 245)
    assert fits_song(WRONG, 0)            # unknown length: can't judge


def test_wrong_version_is_replaced_by_one_that_fits(tmp_path):
    calls = []
    routes = [
        ("/api/get", {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": WRONG}),
        ("/api/search", [
            {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": WRONG},
            {"id": 2, "trackName": "Song", "artistName": "Artist", "duration": 244, "syncedLyrics": RIGHT},
        ]),
    ]
    c = LrclibClient(tmp_path, make_opener(routes, calls))
    assert c.fetch("Song", "Artist", "Album", 245).synced == RIGHT


def test_keeps_only_version_even_if_it_runs_long(tmp_path):
    routes = [("/api/get", {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": WRONG}),
              ("/api/search", [{"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": WRONG}])]
    c = LrclibClient(tmp_path, make_opener(routes, []))
    assert c.fetch("Song", "Artist", "", 245).synced == WRONG      # better than nothing


def test_alternatives_and_remember_choice(tmp_path):
    third = "[00:05.00]first line\n[04:00.00]end\n"
    routes = [
        ("/api/get", {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": RIGHT}),
        ("/api/search", [
            {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": RIGHT},
            {"id": 2, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": WRONG},
            {"id": 3, "trackName": "Song", "artistName": "Artist", "duration": 246, "syncedLyrics": third},
            {"id": 4, "trackName": "Other", "artistName": "Nobody", "duration": 100, "syncedLyrics": "[00:01.00]x"},
        ]),
    ]
    calls = []
    c = LrclibClient(tmp_path, make_opener(routes, calls))
    alts = c.alternatives("Song", "Artist", "", 245)
    assert [a.synced for a in alts][:2] == [RIGHT, third]      # fitting versions first, no duplicates
    assert WRONG in [a.synced for a in alts]                   # the long one is still offered, last
    assert all("[00:01.00]x" != a.synced for a in alts)        # unrelated song excluded
    c.remember_choice("Song", "Artist", 245, alts[1])
    n = len(calls)
    assert c.fetch("Song", "Artist", "", 245).synced == third  # the chosen version sticks
    assert len(calls) == n


def test_song_sync(tmp_path):
    from app.song_sync import SongSync
    p = tmp_path / "song_sync.json"
    s = SongSync(p)
    assert s.get("Song", "Artist") == 0
    s.set("Song", "Artist", 750)
    s.set("Other", "X", 0)
    s2 = SongSync(p)
    assert s2.get("song", "ARTIST") == 750 and s2.get("Other", "X") == 0
    s2.set("Song", "Artist", 0)
    assert SongSync(p).get("Song", "Artist") == 0
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    assert SongSync(tmp_path / "bad.json").get("a", "b") == 0     # corrupt file: no crash


# ---------------------------------------------------------------- smart (syllable) word timing
def test_syllable_counts():
    S = lrc.syllables
    assert S("beautiful") == 3
    assert S("through") == 1
    assert S("make") == 1 and S("love") == 1          # silent e
    assert S("little") == 2 and S("the") == 1 and S("be") == 1
    assert S("going") == 2 and S("I'm") == 1
    assert S("corazón") == 3                         # accented vowels count
    assert S("きょう") == 2                            # small ょ doesn't add a mora
    assert S("がっこう") == 4                          # っ counts
    assert S("東京へ") == 1.6 * 2 + 1                  # kanji in a Japanese word ~1.6 mora
    assert S("사랑해") == 3
    assert S("我爱你") == 3                             # Chinese: one syllable per character
    assert S("...") == 0.5                            # never zero


def test_smart_timing_gives_long_words_more_time():
    lines = lrc.parse_lrc("[00:10.00]through beautiful\n[00:20.00]x\n")
    lrc.estimate_word_timing(lines[0], mode="syllables")
    a, b = lines[0].words
    assert (b.end - b.start) > 2.5 * (a.end - a.start)   # 3 syllables vs 1
    lines2 = lrc.parse_lrc("[00:10.00]through beautiful\n[00:20.00]x\n")
    lrc.estimate_word_timing(lines2[0], mode="letters")
    a2, b2 = lines2[0].words
    assert (b2.end - b2.start) < 1.5 * (a2.end - a2.start)   # letters: much closer together


def test_smart_timing_breathes_after_commas():
    ln = lrc.parse_lrc("[00:10.00]stay, with me\n[00:20.00]x\n")[0]
    lrc.estimate_word_timing(ln, mode="syllables")
    stay, with_, me = ln.words
    assert with_.start - stay.end > 0.1                   # short pause after the comma
    assert abs(me.start - with_.end) < 1e-9               # no pause without punctuation
    assert me.end <= ln.end


def test_smart_timing_respects_span_and_short_lines():
    ln = lrc.parse_lrc("[00:10.00]one two three four five six seven eight\n[00:11.00]x\n")[0]
    lrc.estimate_word_timing(ln)                          # only 1 s until the next line
    assert ln.words[0].start == 10.0 and ln.words[-1].end <= 11.0
    ly = lrc.build_lyrics("[00:01.00]hello there\n[00:05.00]x\n", estimate_mode="letters")
    assert ly.lines[0].words and not ly.lines[0].word_timed


# ---------------------------------------------------------------- preference: word > line > plain
WORDY = "[00:03.00]<00:03.00>first <00:03.50>line <00:04.00>here <00:05.00>\n[02:00.00]<02:00.00>mid <02:01.00>\n"


def test_has_word_timing():
    from app.lyrics_provider import has_word_timing
    assert has_word_timing(WORDY) and not has_word_timing(RIGHT) and not has_word_timing(None)


def test_prefers_word_timed_version_of_same_recording(tmp_path):
    routes = [
        ("/api/get", {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": RIGHT}),
        ("/api/search", [
            {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": RIGHT},
            {"id": 2, "trackName": "Song", "artistName": "Artist", "duration": 246, "syncedLyrics": WORDY},
        ]),
    ]
    assert LrclibClient(tmp_path, make_opener(routes, [])).fetch("Song", "Artist", "", 245).synced == WORDY


def test_word_timed_version_of_other_recording_is_not_used(tmp_path):
    routes = [
        ("/api/get", {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245, "syncedLyrics": RIGHT}),
        ("/api/search", [{"id": 2, "trackName": "Song", "artistName": "Artist", "duration": 252, "syncedLyrics": WORDY}]),
    ]
    assert LrclibClient(tmp_path, make_opener(routes, [])).fetch("Song", "Artist", "", 245).synced == RIGHT


def test_synced_beats_plain(tmp_path):
    routes = [
        ("/api/get", {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 245,
                      "syncedLyrics": None, "plainLyrics": "just text"}),
        ("/api/search", [{"id": 2, "trackName": "Song", "artistName": "Artist", "duration": 247, "syncedLyrics": RIGHT}]),
    ]
    r = LrclibClient(tmp_path, make_opener(routes, [])).fetch("Song", "Artist", "", 245)
    assert r.synced == RIGHT


def test_acrylic_tint_byte_order():
    from app.win32 import acrylic_tint
    # Windows wants 0xAABBGGRR
    assert acrylic_tint("#112233", 1.0) == 0xFF332211
    assert acrylic_tint("#FF0000", 0.0) == 0x000000FF


def test_sizes_carry_over_from_older_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Settings.path().write_text(json.dumps({"font_size": 40}), encoding="utf-8")   # pre-1.10 settings
    s = Settings.load()
    assert (s.font_size, s.side_font_size, s.title_font_size) == (40, 24, 18)    # same look as before
    s.font_size = 70
    s.save()
    s2 = Settings.load()
    assert (s2.font_size, s2.side_font_size, s2.title_font_size) == (70, 24, 18) # only lyrics changed


def test_settings_backup_and_restore(tmp_path, monkeypatch):
    import app.config as C
    monkeypatch.setattr(C, "app_data_dir", lambda: tmp_path)
    s = C.Settings()
    s.width, s.highlight_color, s.genius_token = 700, "#F97316", "tok"
    s.save()
    p = s.backup("before-reset")
    assert p and p.exists()
    s.width, s.highlight_color, s.genius_token = 1000, "#A855F7", "newtok"
    s.save()
    b = C.Settings.list_backups()
    assert b[0]["reason"] == "before-reset" and b[0]["path"] == p
    s.restore_from(p)
    assert s.width == 700 and s.highlight_color == "#F97316"
    assert C.Settings.load().width == 700
    for _ in range(25):
        s.backup("manual")
    assert len(C.Settings.list_backups()) == C.BACKUPS_KEPT
    s.auto_backup(); s.auto_backup()
    assert sum(1 for x in C.Settings.list_backups() if x["reason"] == "auto") == 1


def test_problem_report_recorder_frame_stats():
    from app.diagnostics import Recorder
    t = [0.0]
    rec = Recorder(clock=lambda: t[0])
    for k in range(300):                                 # 5 s at 60 fps, every 10th frame late
        t[0] += 0.033 if k % 10 == 0 else 0.0166
        rec.tick()
        rec.painted(3.0 if k % 10 else 14.0)
    st = rec.frame_stats(60.0)
    assert 9 <= st["late_frames_pct"] <= 11 and st["draw_ms_p95"] == 14.0 and 50 < st["fps"] < 60, st
    assert rec.check_smoothness() is not None            # warns...
    assert rec.check_smoothness() is None                # ...but not again right away
    assert any("uneven" in e for _, e in rec.events)


def test_problem_report_hides_secrets():
    from app.config import Settings
    from app.diagnostics import _sanitised_settings
    s = Settings()
    s.deepl_key = "abc:fx"
    d = _sanitised_settings(s)
    assert d["deepl_key"] == "(set)" and d["anthropic_key"] == "" and "abc" not in str(d)
