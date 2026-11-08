import random

from app.ai_sync import Heard, Recording, align, tokens_of, to_lrc
from app.lrc import parse_lrc

LINES = ["Hold the line and don't let go", "Every pixel burning bright", "",
         "We're the final boss of the show", "Lights are racing through the night"]
TRUE = [10.0, 14.0, None, 22.0, 26.5]


def speak(lines, starts, drop=(), noise=0.0, garble=()):
    """What a perfect listener would hear, word by word."""
    out = []
    rnd = random.Random(1)
    for i, (ln, t) in enumerate(zip(lines, starts)):
        if t is None or i in drop:
            continue
        for k, w in enumerate(ln.split()):
            s = t + 0.35 * k + rnd.uniform(-noise, noise)
            out.append(Heard("zzz" if (i, k) in garble else w, s, s + 0.3))
    return out


def test_tokens():
    assert tokens_of("Don't  STOP, me-now!") == ["dont", "stop", "menow"]
    assert tokens_of("明日も きっと") == ["明", "日", "も", "き", "っ", "と"]


def test_finds_true_times_when_lrclib_is_off():
    orig = [t + 1.7 if t is not None else 18.0 for t in TRUE]       # website timing 1.7 s late
    res = align(LINES, speak(LINES, TRUE), orig)
    for got, want in zip(res.starts, TRUE):
        if want is not None:
            assert abs(got - want) < 0.05
    assert res.matched_lines == 4 and res.quality == 1.0


def test_unheard_line_follows_its_neighbours():
    orig = [t + 2.0 if t is not None else 18.0 for t in TRUE]
    res = align(LINES, speak(LINES, TRUE, drop={3}), orig)
    assert abs(res.starts[3] - 22.0) < 0.05          # 24.0 shifted by the same -2 s
    assert res.matched_lines == 3


def test_plain_lyrics_get_timing():
    res = align(LINES, speak(LINES, TRUE, drop={1}), None)
    assert abs(res.starts[0] - 10) < 0.05 and abs(res.starts[3] - 22) < 0.05
    assert 10 < res.starts[1] < 22


def test_garbled_first_word_estimated_from_second():
    res = align(LINES, speak(LINES, TRUE, garble={(1, 0)}), None)
    assert abs(res.starts[1] - (14.35 - 0.22)) < 0.05


def test_far_away_repeat_ignored():
    lines = ["Say my name", "something else here", "Say my name"]
    heard = [Heard(w, 60 + 0.3 * k, 60.25 + 0.3 * k) for k, w in enumerate("Say my name".split())]
    res = align(lines, heard, [5.0, 30.0, 60.0])
    assert res.starts[0] == 5.0 or abs(res.starts[0] - 5.0) < 1   # not dragged to 60 s
    assert abs(res.starts[2] - 60.0) < 0.05


def test_lrc_round_trip_has_word_timing():
    res = align(LINES, speak(LINES, TRUE), None)
    text = to_lrc(LINES, res, "Neon", "Demo", "AI heard 4/4 lines")
    parsed = [ln for ln in parse_lrc(text) if ln.text.strip()]
    assert [ln.text for ln in parsed] == [ln for ln in LINES if ln]
    assert parsed[0].word_timed and abs(parsed[0].words[2].start - 10.7) < 0.02
    assert abs(parsed[1].start - 14.0) < 0.02


def test_japanese_line_level():
    lines = ["君の 名前を 呼んでいる", "明日も きっと 会えるから"]
    heard = [Heard("君の名前を呼んでいる", 5.0, 8.0), Heard("明日もきっと会えるから", 9.5, 12.5)]
    res = align(lines, heard, [6.0, 11.0])
    assert abs(res.starts[0] - 5.0) < 0.05 and abs(res.starts[1] - 9.5) < 0.05
    assert res.word_times[0] is None


def test_recording_segments():
    r = Recording("k", 200)
    f = [0.0] * 8000
    r.add(f, 10.5); r.add(f, 11.0); r.add(f, 30.5)      # jump -> new piece
    assert len(r.segments) == 2 and abs(r.segments[0].start - 10.0) < 1e-6
    assert abs(r.covered() - 1.5) < 1e-6


def test_stretched_first_word_is_fixed():
    from app.ai_sync import fix_stretched
    # what Whisper really reported on Windows for line 2 of the CI test song (truth 7.4 s)
    words = [Heard("go", 4.22, 4.50), Heard("Every", 4.50, 7.64), Heard("pixel", 7.64, 8.00),
             Heard("burning", 8.00, 8.38), Heard("bright", 8.38, 8.80), Heard("the", 3.20, 3.36),
             Heard("line", 3.36, 3.60)]
    fixed = fix_stretched(words)
    assert abs(fixed[1].start - 7.4) < 0.35 and fixed[1].end == 7.64
    assert fixed[2].start == 7.64          # normal words untouched


def _fake_sound(monkeypatch, app_ok: bool, app_crash: bool = False):
    import sys
    import time as _t
    import types

    import numpy as np

    import app.ai_sync as AS
    import app.app_audio as AA

    class Rec:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def record(self, numframes):
            _t.sleep(0.005)
            return np.full((numframes, 1), 0.2, dtype=np.float32)

    class Spk:
        id, name = "spk", "Speakers"
    sc = types.SimpleNamespace(default_speaker=lambda: Spk(),
                               get_microphone=lambda id, include_loopback: types.SimpleNamespace(
                                   recorder=lambda **k: Rec()))
    monkeypatch.setitem(sys.modules, "soundcard", sc)

    class FakeApp:
        def __init__(self, pid): self.pid = pid
        def __enter__(self):
            if app_crash:
                raise OSError("Windows refused app audio (0x80070005)")
            return self
        def __exit__(self, *a): return False
        def read(self, n):
            _t.sleep(0.005)
            return np.full(n, 0.5, dtype=np.float32)
    monkeypatch.setattr(AA, "AppAudio", FakeApp)
    monkeypatch.setattr(AS, "run_probe", lambda pid, timeout=25: {"ok": app_ok, "error": None if app_ok else "too old"})
    return AS


def _listen(AS, target):
    import time as _t
    pos = {"t": 0.0}

    def state():
        pos["t"] += 0.5
        return "song", pos["t"], True
    lis = AS.Listener(state, lambda: target)
    lis.arm("song", 200)
    for _ in range(200):
        r = lis.peek()
        if r is not None and r.seconds >= 2:
            break
        _t.sleep(0.02)
    rec = lis.take()
    lis.stop()
    return lis, rec


def test_listens_to_spotify_only_when_windows_allows(monkeypatch):
    AS = _fake_sound(monkeypatch, app_ok=True)
    lis, rec = _listen(AS, 1234)
    assert lis.method == "Spotify only" and rec.seconds >= 2
    assert float(rec.segments[0].frames[0][0]) == 0.5        # came from the app, not the speakers


def test_falls_back_to_whole_pc(monkeypatch):
    AS = _fake_sound(monkeypatch, app_ok=False)
    lis, rec = _listen(AS, 1234)
    assert lis.method == "whole PC" and rec.seconds >= 2 and "too old" in lis.app_note


def test_falls_back_when_app_audio_fails_later(monkeypatch):
    AS = _fake_sound(monkeypatch, app_ok=True, app_crash=True)
    lis, rec = _listen(AS, 1234)
    assert lis.method == "whole PC" and rec.seconds >= 2


def test_whole_pc_setting(monkeypatch):
    AS = _fake_sound(monkeypatch, app_ok=True)
    lis, rec = _listen(AS, None)
    assert lis.method == "whole PC"


def test_notification_history(tmp_path):
    from datetime import datetime

    from app.history import NotificationLog, when_text
    log = NotificationLog(tmp_path / "n.json")
    for i in range(105):
        log.add(f"song {i}", "success")
    again = NotificationLog(tmp_path / "n.json")
    assert len(again.entries) == 100 and again.newest_first()[0]["text"] == "song 104"
    again.clear()
    assert NotificationLog(tmp_path / "n.json").entries == []
    now = datetime(2026, 11, 7, 20, 0)
    assert when_text("2026-11-07T19:52:00", now) == "Today 7:52 PM"
    assert when_text("2026-11-06T09:03:00", now) == "Yesterday 9:03 AM"
    assert when_text("2026-11-03T19:52:00", now) == "Nov 3, 7:52 PM"
