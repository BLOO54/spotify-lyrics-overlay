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
    s.undo()                                     # oops
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
