"""All lyric text here is invented test text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import lrc  # noqa: E402
from app.my_lyrics import MyLyrics, TapSession, fmt_ts, lines_for_tapping, safe_name  # noqa: E402


def test_find_by_artist_and_title(tmp_path):
    m = MyLyrics(tmp_path)
    (tmp_path / "Jamie Paige - birdbrain.lrc").write_text("[00:01.00]hi\n", encoding="utf-8")
    assert m.find("BIRDBRAIN", "Jamie Paige").name == "Jamie Paige - birdbrain.lrc"
    assert m.find("birdbrain", "jamie paige") is not None             # case doesn't matter
    assert m.find("birdbrain", "Someone Else") is None                 # different artist
    assert m.read("birdbrain", "Jamie Paige") == "[00:01.00]hi\n"
    assert (tmp_path / "README.txt").exists()


def test_title_only_file_and_bom(tmp_path):
    m = MyLyrics(tmp_path)
    (tmp_path / "Night Drive.lrc").write_bytes("﻿[00:02.00]x\n".encode("utf-8"))   # Notepad-style BOM
    assert m.read("night drive", "Any Artist") == "[00:02.00]x\n"


def test_save_and_overwrite(tmp_path):
    m = MyLyrics(tmp_path)
    p = m.save("Song: Part 2?", "A/B", "[00:01.00]a\n")
    assert p.name == "AB - Song Part 2.lrc"                             # unsafe characters removed
    p2 = m.save("Song: Part 2?", "A/B", "[00:02.00]b\n")
    assert p2 == p and m.read("Song: Part 2?", "A/B") == "[00:02.00]b\n"
    assert safe_name("", "Solo") == "Solo.lrc"


def test_fmt_ts():
    assert fmt_ts(0) == "[00:00.00]"
    assert fmt_ts(65.437) == "[01:05.44]"
    assert fmt_ts(-3) == "[00:00.00]"


def test_tap_session_makes_valid_lrc():
    s = TapSession(["first line", "second line", "third line"])
    assert s.tap(10.15) and s.tap(14.0)
    s.undo()                                     # oops - back to line 2
    assert s.cursor == 1
    assert s.tap(14.65) and s.tap(14.60)         # accidental double tap still stays in order
    assert s.done and not s.tap(30)
    text = s.to_lrc("Song", "Artist", song_end=200)
    parsed = lrc.parse_lrc(text)
    starts = [round(l.start, 2) for l in parsed]
    assert starts[:3] == [10.0, 14.5, 14.55]
    assert [l.text for l in parsed[:3]] == ["first line", "second line", "third line"]
    assert parsed[-1].is_break                   # an ending gap so the last line clears


def test_lines_for_tapping():
    synced = "[ar:X]\n[00:01.00]<00:01.00>hello <00:01.50>there\n[00:03.00]\n[00:04.00]  second   line \n"
    assert lines_for_tapping(synced, None) == ["hello there", "second line"]
    assert lines_for_tapping(None, "one\n\n two \n") == ["one", "two"]
    assert lines_for_tapping(None, None) == []


def test_fix_only_the_wrong_lines():
    # start from existing timing, then fix line 2 and nudge line 3
    s = TapSession(["a", "b", "c", "d"], times=[1.0, 5.0, 9.0, 13.0])
    assert s.timed_count == 4 and s.cursor == 0
    s.go_to(1)
    assert s.rewind_target(1) == 0.0             # 1.5 s before line "a" -> clamps at 0
    assert abs(s.rewind_target(2) - 3.5) < 1e-9  # before line "b"
    s.tap(6.15)                                  # re-tap line b at the right moment
    assert s.times == [1.0, 6.0, 9.0, 13.0] and s.cursor == 2
    s.nudge(2, +0.1); s.nudge(2, -0.5)
    assert abs(s.times[2] - 8.6) < 1e-9
    s.nudge(9, 1.0)                              # out of range: ignored
    text = s.to_lrc("T", "A", song_end=60, shift=0.25)
    starts = [round(l.start, 2) for l in lrc.parse_lrc(text)]
    assert starts[:4] == [1.25, 6.25, 8.85, 13.25]


def test_untimed_lines_are_left_out_and_order_kept():
    s = TapSession(["a", "b", "c"], times=[None, 7.0, 2.0])
    starts = [(round(l.start, 1), l.text) for l in lrc.parse_lrc(s.to_lrc("T", "A"))]
    assert starts[:2] == [(2.0, "c"), (7.0, "b")]


def test_list_synced(tmp_path):
    from app.my_lyrics import list_synced
    (tmp_path / "Jamie Paige - birdbrain.lrc").write_text("[ti:birdbrain]\n[ar:Jamie Paige]\n[00:01.00]hi\n",
                                                          encoding="utf-8")
    (tmp_path / "Someone - Song.lrc").write_text("[#:AI heard 30/34 lines]\n[00:01.00]x\n", encoding="utf-8")
    (tmp_path / "README.txt").write_text("x")
    got = list_synced(tmp_path, "ai")
    assert [(g["title"], g["artist"]) for g in got] == [("birdbrain", "Jamie Paige"), ("Song", "Someone")]
    assert got[1]["note"] == "AI heard 30/34 lines" and got[0]["kind"] == "ai" and got[0]["modified"]
