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


def test_run_job_reports_progress(monkeypatch):
    import numpy as np

    import app.ai_sync as AS
    rec = AS.Recording("k", 60)
    for k in range(80):                           # 40 s of sound in one piece
        rec.add(np.full(8000, 0.1, dtype=np.float32), 0.5 * (k + 1))

    def fake_transcribe(model_dir, audio, lang, prompt, threads, progress=None):
        for f in (0.25, 0.5, 1.0):
            if progress:
                progress(f)
        return [AS.Heard(w, 1.0 + 0.3 * i, 1.2 + 0.3 * i) for i, w in enumerate("Hold the line and let go".split())]
    monkeypatch.setattr(AS, "transcribe", fake_transcribe)
    seen = []
    job = AS.Job("k", "T", "A", ["Hold the line and let go", "x y z", "a b c"], None, rec)
    AS.run_job(job, None, 2, progress=seen.append)
    assert seen[0] == 0.0 and seen[-1] == 1.0 and seen == sorted(seen) and any(0.4 < x < 0.6 for x in seen)


def test_heard_flags_and_details(tmp_path):
    from app.ai_sync import load_details, save_details
    orig = [t + 2.0 if t is not None else 18.0 for t in TRUE]
    res = align(LINES, speak(LINES, TRUE, drop={3}), orig)
    assert res.heard == [True, True, False, False, True]          # line 3 not heard, the empty line never is
    lrc = tmp_path / "Demo - Neon.lrc"
    lrc.write_text("x", encoding="utf-8")
    save_details(lrc, LINES, res)
    d = load_details(lrc)
    assert d["lines"] == LINES and d["heard"] == res.heard and d["matched"] == 3 and d["total"] == 4


def test_drifting_website_timing_still_matched():
    """Like BIRDBRAIN: the website's timing is for a longer version, so it drifts further off as the
    song goes on (40 s by the end). Every heard line must still be used."""
    lines = [f"line number {k} sings words {k} here" for k in range(40)]
    truth = [5.0 + 5.5 * k for k in range(40)]
    orig = [t * 1.2 for t in truth]                        # 0 s off at the start, ~43 s off at the end
    heard = []
    for t, ln in zip(truth, lines):
        for j, w in enumerate(ln.split()):
            heard.append(Heard(w, t + 0.3 * j, t + 0.3 * j + 0.25))
    res = align(lines, heard, orig)
    assert res.matched_lines == 40, res.matched_lines
    assert max(abs(a - b) for a, b in zip(res.starts, truth)) < 0.05


def test_chorus_matched_to_wrong_repeat_is_still_rejected():
    lines = ["intro words start here", "chorus line goes like this", "verse words in the middle",
             "more verse words here now", "chorus line goes like this", "outro words end here"]
    truth = [2.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    heard = []
    for i in (0, 1, 2, 3, 5):            # the second chorus wasn't heard, the first was heard twice
        for j, w in enumerate(lines[i].split()):
            heard.append(Heard(w, truth[i] + 0.3 * j, truth[i] + 0.3 * j + 0.2))
    res = align(lines, heard, list(truth))
    assert abs(res.starts[1] - 10.0) < 0.05 and abs(res.starts[4] - 40.0) < 1.0


def _chorus_song():
    chorus = ["But I falter on the forethought", "I keep on running like a chicken with its head cut off",
              "A birdbrain baby I don't know when I'm supposed to stop", "Hit me with the flap down",
              "I put my neck up on the chopping block", "A birdbrain baby I don't think my head is coming off",
              "Baby I don't know when I'm supposed to stop", "Baby I don't know when I'm supposed to stop"]
    verse1 = ["So unsure before it's even heard", "Never confident in a single word"]
    verse2 = ["This bird constantly overturned", "Ten time unwitting champion of being spurned"]
    bridge = ["Is it all for a show", "In the golden hour you seem to know no fear"]
    lines = verse1 + chorus + verse2 + chorus + bridge + chorus
    truth, t = [], 5.0
    for ln in lines:
        truth.append(t)
        t += 0.3 * len(ln.split()) + 1.0
    return lines, truth


def test_repeated_choruses_all_matched():
    """BIRDBRAIN again: the chorus repeats word for word three times. The AI hears every chorus,
    but some words are misheard and it adds a few words that aren't sung."""
    lines, truth = _chorus_song()
    rnd = random.Random(7)
    heard = []
    for i, (ln, t) in enumerate(zip(lines, truth)):
        for j, w in enumerate(ln.split()):
            if rnd.random() < 0.12:
                continue                                   # missed word
            word = w if rnd.random() > 0.08 else w + "s"   # misheard word
            heard.append(Heard(word, t + 0.3 * j, t + 0.3 * j + 0.25))
        if i % 5 == 0:
            heard.append(Heard("yeah", t + 0.3 * len(ln.split()), t + 0.3 * len(ln.split()) + 0.2))
    orig = [x + 0.8 for x in truth]                        # the website's timing is a little off
    res = align(lines, heard, orig)
    assert res.matched_lines >= len(lines) - 2, (res.matched_lines, len(lines),
                                                 [lines[i] for i in range(len(lines)) if not res.heard[i]])
    assert max(abs(a - b) for a, b in zip(res.starts, truth)) < 1.0


def test_repeated_choruses_without_website_timing():
    lines, truth = _chorus_song()
    heard = [Heard(w, t + 0.3 * j, t + 0.3 * j + 0.25) for ln, t in zip(lines, truth)
             for j, w in enumerate(ln.split())]
    res = align(lines, heard, None)
    assert res.matched_lines == len(lines)


def test_rematch_from_saved_words(tmp_path):
    from app.ai_sync import load_details, rematch, save_details
    lines, truth = _chorus_song()
    heard = [Heard(w, t + 0.3 * j, t + 0.3 * j + 0.25) for ln, t in zip(lines, truth)
             for j, w in enumerate(ln.split())]
    res = align(lines, heard, [x + 0.5 for x in truth])
    lrc = tmp_path / "Jamie Paige - BIRDBRAIN.lrc"
    lrc.write_text("x", encoding="utf-8")
    save_details(lrc, lines, res, [x + 0.5 for x in truth])
    assert len(load_details(lrc)["words"]) == len(heard)
    text, res2, note = rematch(lrc, "BIRDBRAIN", "Jamie Paige")
    assert text and res2.matched_lines == len(lines) and note == f"AI heard {len(lines)}/{len(lines)} lines"
    assert rematch(tmp_path / "missing.lrc", "x", "y")[0] is None


def _job_with_audio(lines, seconds=60):
    import numpy as np

    import app.ai_sync as AS
    rec = AS.Recording("k", seconds)
    for k in range(int(seconds * 2)):
        rec.add(np.full(8000, 0.1, dtype=np.float32), 0.5 * (k + 1))
    return AS.Job("k", "T", "A", lines, None, rec)


def test_second_listen_finds_missed_lines(monkeypatch):
    """First listen misses the chorus; the second listen (told which lyrics to expect) hears it."""
    import app.ai_sync as AS
    lines = ["one two three four five", "six seven eight nine ten", "the chorus goes like this tonight",
             "and then it goes like that", "eleven twelve thirteen fourteen", "fifteen sixteen seventeen"]
    truth = [2.0, 8.0, 14.0, 20.0, 26.0, 32.0]
    calls = []

    def fake_transcribe(model, audio, lang, prompt, threads=2, progress=None):
        calls.append(prompt)
        span = len(audio) / AS.SAMPLE_RATE
        if span > 50:                                   # first listen: the whole song, chorus missed
            out = []
            for i in (0, 1, 4, 5):
                out += [AS.Heard(w, truth[i] + 0.4 * j, truth[i] + 0.4 * j + 0.3)
                        for j, w in enumerate(lines[i].split())]
            return out
        if prompt and "chorus" in prompt:              # second listen of the missed part (times from clip start)
            out = []
            for i in (2, 3):
                out += [AS.Heard(w, truth[i] - CLIP[0] + 0.4 * j, truth[i] - CLIP[0] + 0.4 * j + 0.3)
                        for j, w in enumerate(lines[i].split())]
            return out
        return []
    real_clip = AS._clip
    CLIP = [0.0]

    def spy_clip(pieces, t0, t1):
        got = real_clip(pieces, t0, t1)
        if got:
            CLIP[0] = got[0]
        return got
    monkeypatch.setattr(AS, "transcribe", fake_transcribe)
    monkeypatch.setattr(AS, "_clip", spy_clip)
    monkeypatch.setattr(AS, "load_whisper", lambda *a, **k: "model")
    job = _job_with_audio(lines)
    text, res, note = AS.run_job(job, None, 2)
    assert res.matched_lines == 4 and "2nd listen" not in note          # off by default
    job.second_pass = True
    text, res, note = AS.run_job(job, None, 2)
    assert res.matched_lines == 6, (res.heard, note)
    assert "2nd listen found 2 more" in note
    assert abs(res.starts[2] - 14.0) < 0.1 and abs(res.starts[3] - 20.0) < 0.1


def test_missed_windows():
    from app.ai_sync import AlignResult, missed_windows
    lines = ["a b", "c d", "e f", "g h", "i j"]
    res = AlignResult([1.0, 5.0, 9.0, 13.0, 40.0], [None] * 5, [2.0, None, None, 14.0, None], 2, 5, 0.4,
                      [True, False, False, True, False])
    w = missed_windows(res, lines)
    assert [x[2] for x in w] == [[1, 2], [4]]
    assert w[0][0] >= 1.5 and w[0][1] <= 14.5 and w[1][0] >= 37.0


def test_vocal_separation_failure_falls_back(monkeypatch, tmp_path):
    import app.ai_sync as AS
    lines = ["one two three four five", "six seven eight nine ten", "eleven twelve thirteen fourteen"]
    monkeypatch.setattr(AS, "load_whisper", lambda *a, **k: "model")
    monkeypatch.setattr(AS, "transcribe", lambda model, audio, *a, **k: [
        AS.Heard(w, 2.0 + 6 * i + 0.4 * j, 2.3 + 6 * i + 0.4 * j) for i, ln in enumerate(lines)
        for j, w in enumerate(ln.split())])
    job = _job_with_audio(lines)
    job.vocals_dir = tmp_path                     # no model files there -> separation can't start
    text, res, note = AS.run_job(job, None, 2)
    assert text and res.matched_lines == 3 and "vocal separation didn't work" in note


def test_voiced_parts_and_normalise():
    import numpy as np

    from app.vocals import normalise, voiced_parts
    rate = 16000
    y = np.zeros(rate * 10, dtype=np.float32)
    t = np.arange(rate) / rate
    for start in (1.0, 2.3, 6.0):                 # three sung bits, the first two close together
        a = int(start * rate)
        y[a:a + rate] = 0.02 * np.sin(2 * np.pi * 220 * t)
    parts = voiced_parts(normalise(y), rate)
    assert len(parts) == 2, parts                 # the 0.3 s gap is joined, the long one isn't
    assert abs(parts[0][0] - 0.65) < 0.1 and abs(parts[0][1] - 3.65) < 0.1
    assert abs(parts[1][0] - 5.65) < 0.1 and abs(parts[1][1] - 7.35) < 0.1
    assert abs(float(np.abs(normalise(y)).max()) - 0.6) < 0.01
    assert voiced_parts(np.zeros(rate * 3, dtype=np.float32), rate) == []
