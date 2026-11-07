"""CI helper: build a test "song" for AI sync.

Each lyric line is spoken by Windows' built-in voice (made beforehand as line0.wav,
line1.wav, ...) and placed at a known time over a loud synth backing track with a
beat. The true start times go into truth.lrc so the AI's answer can be checked.

    python tools/make_ai_test_song.py FOLDER
"""

import sys
import wave
from pathlib import Path

import numpy as np

LINES = [
    "Hold the line and don't let go",
    "Every pixel burning bright",
    "We're the final boss tonight",
    "Racing shadows through the rain",
    "Calling out your name again",
    "Neon rivers in the sky",
    "Never stop and never die",
    "Light it up and say goodbye",
]
STARTS = [3.0, 7.4, 11.6, 16.0, 29.5, 33.8, 38.1, 42.7]    # with an instrumental break after line 4
RATE = 16000


def read(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def trim(a, thr=0.01):
    idx = np.where(np.abs(a) > thr)[0]
    return a[idx[0]:idx[-1] + 1] if len(idx) else a


def backing(seconds):
    t = np.arange(int(seconds * RATE)) / RATE
    out = np.zeros_like(t)
    chords = [(220.0, 277.2, 329.6), (196.0, 246.9, 293.7), (174.6, 220.0, 261.6), (196.0, 246.9, 311.1)]
    for k in range(int(seconds // 2) + 1):
        seg = (t >= 2 * k) & (t < 2 * k + 2)
        for f in chords[k % 4]:
            out[seg] += 0.05 * np.sign(np.sin(2 * np.pi * f * t[seg])) * 0.6 + 0.04 * np.sin(2 * np.pi * f * 2 * t[seg])
    rng = np.random.default_rng(1)
    for beat in np.arange(0, seconds, 0.5):                       # kick + hi-hat
        i = int(beat * RATE)
        n = min(2400, len(out) - i)
        if n > 0:
            env = np.exp(-np.arange(n) / 500)
            out[i:i + n] += 0.35 * np.sin(2 * np.pi * 60 * np.arange(n) / RATE) * env
            out[i:i + n] += 0.05 * rng.standard_normal(n) * np.exp(-np.arange(n) / 150)
    return out


def main(folder):
    folder = Path(folder)
    total = STARTS[-1] + 6
    mix = backing(total)
    truth = []
    for i, (line, start) in enumerate(zip(LINES, STARTS)):
        voice = trim(read(folder / f"line{i}.wav"))
        a = int(start * RATE)
        mix[a:a + len(voice)] += voice * 1.4
        m = int(start // 60)
        truth.append(f"[{m:02d}:{start - 60 * m:05.2f}]{line}")
    truth.insert(4, "[00:21.00]")                                 # the instrumental break
    mix /= max(1.0, float(np.max(np.abs(mix))) / 0.95)
    with wave.open(str(folder / "test.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((mix * 32767).astype(np.int16).tobytes())
    (folder / "truth.lrc").write_text("\n".join(truth) + "\n", encoding="utf-8")
    (folder / "lines.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
    print(f"test song: {total:.1f} s, {len(LINES)} lines")


if __name__ == "__main__":
    main(sys.argv[1])
