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
