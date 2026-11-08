"""AI sync: listen to the song once, hear when each lyric line is sung, and remember it.

Plain English:
* Optional. Turn it on in Settings -> Lyrics & translation -> AI sync and download a
  speech-recognition model (Light ~0.5 GB or Best ~1.5 GB). Remove it any time.
* While a song plays, the app listens to what your PC is playing (the same sound you
  hear) and keeps it in memory only - it is never written to disk or sent anywhere.
* When the song ends, a speech-recognition model (OpenAI's Whisper, run locally with
  faster-whisper) works out when each word was heard. Those words are matched to the
  lyrics, and the timing is saved as a small .lrc file (a few KB) in "ai_lyrics".
  From the next play on, that song uses the AI timing.
* Lines the AI couldn't hear (shouted, buried under the music, game noise) keep the
  normal timing, shifted by how far off the nearby lines were.

Nothing in the matching part needs Windows or Qt, so it's unit-tested anywhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.5
MAX_RECORD_SECONDS = 12 * 60

# name -> (Hugging Face repo, label, approx size)
MODELS = {
    "light": ("Systran/faster-whisper-small", "Light", "about 0.5 GB"),
    "best": ("Systran/faster-whisper-medium", "Best", "about 1.5 GB"),
    "max": ("Systran/faster-whisper-large-v3", "Max", "about 3 GB"),
    "tiny": ("Systran/faster-whisper-tiny", "Tiny (testing)", "about 75 MB"),
    # not a speech model: separates the voice from the music (Spleeter by Deezer, via sherpa-onnx)
    "vocals": ("csukuangfj/sherpa-onnx-spleeter-2stems-fp16", "Vocal separation", "about 40 MB"),
}
SPEECH_MODELS = ("light", "best", "max")
NEEDED_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "vocabulary.json",
                "preprocessor_config.json", "vocals.fp16.onnx", "accompaniment.fp16.onnx")
HF = "https://huggingface.co"
USER_AGENT = "SpotifyLyricsOverlay (https://github.com/BLOO54/spotify-lyrics-overlay)"


# --------------------------------------------------------------------------- matching

_CJK_CHAR = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ가-힯]")
_KEEP = re.compile(r"[\w]+", re.UNICODE)


def has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR.search(text or ""))


def norm_word(w: str) -> str:
    w = (w or "").lower().replace("’", "'").replace("'", "")
    return "".join(_KEEP.findall(w)).replace("_", "")


def tokens_of(text: str) -> list[str]:
    """Words for matching: normal words lower-cased without punctuation; Japanese,
    Chinese and Korean are compared one character at a time (they have no spaces)."""
    out: list[str] = []
    for chunk in (text or "").split():
        if has_cjk(chunk):
            buf = ""
            for ch in chunk:
                if _CJK_CHAR.match(ch):
                    if buf:
                        out.append(norm_word(buf))
                        buf = ""
                    out.append(ch)
                elif ch.isalnum():
                    buf += ch
                else:
                    if buf:
                        out.append(norm_word(buf))
                        buf = ""
            if buf:
                out.append(norm_word(buf))
        else:
            n = norm_word(chunk)
            if n:
                out.append(n)
    return [t for t in out if t]


@dataclass
class Heard:
    text: str
    start: float
    end: float


def heard_tokens(words: list[Heard]) -> list[tuple[str, float, float]]:
    """Split what the AI heard into the same kind of tokens, sharing each word's time."""
    out = []
    for w in words:
        toks = tokens_of(w.text)
        if not toks:
            continue
        span = max(w.end - w.start, 0.02)
        for k, t in enumerate(toks):
            out.append((t, w.start + span * k / len(toks), w.start + span * (k + 1) / len(toks)))
    return out


def fix_stretched(words: list[Heard]) -> list[Heard]:
    """Whisper often stretches the first word after a pause back to the end of the word
    before it ("Every" said at 7.4 s gets 4.5-7.6 s). Its END is still right, so give such
    words a normal length for this singer, ending where Whisper said they end."""
    def size(w):
        t = norm_word(w.text)
        return max(1, len(t) * (3 if has_cjk(t) else 1))
    rates = sorted((w.end - w.start) / size(w) for w in words if 0.03 < w.end - w.start < 1.0)
    rate = rates[len(rates) // 2] if rates else 0.07
    rate = min(max(rate, 0.04), 0.2)
    out = []
    for w in words:
        typical = max(rate * size(w) + 0.05, 0.15)
        dur = w.end - w.start
        if dur > 0.6 and dur > 2.0 * typical:
            out.append(Heard(w.text, w.end - typical, w.end))
        else:
            out.append(w)
    return out


def _similar(a: str, b: str) -> float:
    """1 = same word, ~0.6 = close (a misheard ending, a plural), 0 = different."""
    if a == b:
        return 1.0
    if has_cjk(a) or has_cjk(b) or min(len(a), len(b)) < 3:
        return 0.0
    if a.startswith(b) or b.startswith(a):
        return 0.6 if abs(len(a) - len(b)) <= 2 else 0.0
    from difflib import SequenceMatcher as _SM
    r = _SM(None, a, b).ratio()
    return 0.6 if r >= 0.8 else 0.0


_SOUND = str.maketrans("aeiouyhw", "        ")


def _sound_key(w: str) -> str:
    """Roughly how a word sounds: first letter + consonants (so 'nothin' ~ 'nothing', 'gonna' ~ 'gona')."""
    w = w.replace("ph", "f").replace("ck", "k").replace("qu", "kw").replace("z", "s").replace("x", "ks")
    if w.endswith("ing"):
        w = w[:-3] + "in"
    rest = w[1:].translate(_SOUND).replace(" ", "")
    out = w[:1]
    for ch in rest:
        if not out.endswith(ch):
            out += ch
    return out


def _similar_loose(a: str, b: str) -> float:
    """For the parts the AI is told to listen for: also accept words that only sound alike."""
    s = _similar(a, b)
    if s or has_cjk(a) or has_cjk(b) or min(len(a), len(b)) < 3:
        return s
    from difflib import SequenceMatcher as _SM
    if _sound_key(a) == _sound_key(b) or _SM(None, a, b).ratio() >= 0.66:
        return 0.5
    return 0.0


def align_tokens(lyr: list[str], heard: list[tuple[str, float]], expect: list | None = None,
                 window: float = 60.0, loose: list | None = None,
                 loose_span: tuple | None = None) -> list[tuple[int, int]]:
    """Line the lyric words up with the heard words over the WHOLE song at once, keeping both in
    order (like lining up two DNA strands). Unlike "find the longest identical stretch first",
    a chorus that repeats word for word can't get paired with the wrong repeat. When the
    website's timing is known, matches near the expected time are preferred.
    Returns (lyric word index, heard word index) pairs."""
    n, m = len(lyr), len(heard)
    if not n or not m:
        return []
    GAP_L, GAP_H = -0.35, -0.25           # skipping a lyric word / a heard word
    neg = float("-inf")
    # score[i][j]: best score lining up lyr[:i] with heard[:j]; move: 0 diag, 1 skip lyric, 2 skip heard
    prev = [GAP_H * j for j in range(m + 1)]
    moves = [bytearray(m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        moves[0][j] = 2
    for i in range(1, n + 1):
        cur = [0.0] * (m + 1)
        cur[0] = GAP_L * i
        mv = moves[i]
        mv[0] = 1
        w = lyr[i - 1]
        e = expect[i - 1] if expect else None
        for j in range(1, m + 1):
            best, how = prev[j] + GAP_L, 1
            alt = cur[j - 1] + GAP_H
            if alt > best:
                best, how = alt, 2
            hw, ht_ = heard[j - 1]
            if hw == w:
                sim = 1.0
            elif loose is not None and loose[i - 1] and loose_span[0] <= ht_ < loose_span[1]:
                sim = _similar_loose(w, hw)
            else:
                sim = _similar(w, hw) if hw[:1] == w[:1] or len(w) > 4 else 0.0
            if sim > 0:
                score = 2.0 * sim
                if e is not None:
                    dt = abs(ht_ - e)
                    score = neg if dt > window else score - 0.8 * min(dt / window, 1.0)
                alt = prev[j - 1] + score
                if alt > best:
                    best, how = alt, 0
            cur[j] = best
            mv[j] = how
        prev = cur
    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        how = moves[i][j]
        if how == 0:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif how == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    # a lone match of a short word with nothing matched around it is usually an accident
    keep = []
    matched = {a for a, _ in pairs}
    for a, b in pairs:
        lone = (a - 1 not in matched) and (a + 1 not in matched)
        if lone and (len(lyr[a]) < 4 or has_cjk(lyr[a])):
            continue
        keep.append((a, b))
    return keep


@dataclass
class AlignResult:
    starts: list            # new start time for every line (None for empty lines without a time)
    word_times: list        # per line: list of (start, end) per space-separated word, or None
    line_ends: list         # per line: when its last heard word ended (None if unknown)
    matched_lines: int
    lyric_lines: int
    coverage: float         # share of lyric words the AI heard
    heard: list = field(default_factory=list)   # per line: True = the AI heard it, False = timing was filled in
    words: list = field(default_factory=list)   # what the AI heard: (word, start, end) - kept so it can re-match
    matched_words: int = 0                      # lyric words matched to something the AI heard
    new_lines: list = field(default_factory=list)   # after "listen to missed parts": lines it newly heard
    new_words: int = 0                              # ... and how many more lyric words it matched

    @property
    def quality(self) -> float:
        return self.matched_lines / max(self.lyric_lines, 1)


def align(lines: list[str], heard: list[Heard], orig_starts: list | None = None,
          max_shift: float = 150.0, focus: tuple | None = None) -> AlignResult:
    """Match the heard words to the lyric lines and work out when each line starts.
    focus = (line numbers, (from, to) seconds): lines the AI was specifically told to listen for
    there - words that only sound alike count for them."""
    n = len(lines)
    # lyric tokens, remembering which line / which word of the line each one came from
    lyr: list[str] = []
    owner: list[tuple[int, int]] = []
    line_tok: list[list[int]] = [[] for _ in range(n)]
    for i, text in enumerate(lines):
        cjk = has_cjk(text)
        for wi, word in enumerate(text.split()):
            for t in tokens_of(word):
                line_tok[i].append(len(lyr))
                owner.append((i, -1 if cjk else wi))
                lyr.append(t)
    ht = heard_tokens(fix_stretched(heard))
    hear = [t for t, _, _ in ht]

    # when each lyric word is expected (from the website's timing), to prefer nearby matches
    expect: list = [None] * len(lyr)
    if orig_starts is not None:
        for i in range(n):
            if i < len(orig_starts) and orig_starts[i] is not None:
                for k, t in enumerate(line_tok[i]):
                    expect[t] = orig_starts[i] + 0.3 * k
    anchors: dict[int, tuple[float, float]] = {}
    loose = None
    if focus:
        want = set(focus[0])
        loose = [owner[k][0] in want for k in range(len(lyr))]
    for a, b in align_tokens(lyr, [(t, s_) for t, s_, _ in ht], expect,
                             loose=loose, loose_span=focus[1] if focus else None):
        anchors[a] = (ht[b][1], ht[b][2])

    starts: list = [None] * n
    ends: list = [None] * n
    words_t: list = [None] * n
    matched = 0
    lyric_lines = sum(1 for i in range(n) if line_tok[i])
    for i in range(n):
        toks = line_tok[i]
        if not toks:
            continue
        hit = [(k, anchors[t]) for k, t in enumerate(toks) if t in anchors]
        if not hit:
            continue
        k0, (t0, _) = hit[0]
        if k0 > 2:
            # the start wasn't heard - fine if plenty of the rest was: work back from how fast it's sung
            if len(hit) < 3 or len(hit) < 0.4 * len(toks):
                continue
            k1, (t1_, _) = hit[-1]
            per = (t1_ - t0) / max(k1 - k0, 1)
            per = min(max(per, 0.15), 0.6)
            s = max(0.0, t0 - per * k0)
        else:
            s = max(0.0, t0 - 0.22 * k0)
        starts[i] = s
        ends[i] = hit[-1][1][1] if hit[-1][0] >= len(toks) - 2 else None
        matched += 1
        if not has_cjk(lines[i]) and len(hit) >= 0.6 * len(toks):
            words_t[i] = {owner[toks[k]][1]: tt for k, tt in hit}

    # A heard line whose shift from the website's timing doesn't fit the lines around it was matched
    # to the wrong place (e.g. a repeated chorus). Comparing with the NEIGHBOURS (not a fixed limit)
    # still works when the website's timing drifts further off as the song goes on.
    if orig_starts is not None:
        offs = {i: starts[i] - orig_starts[i] for i in range(n)
                if starts[i] is not None and i < len(orig_starts) and orig_starts[i] is not None}
        bad = []
        everyone = sorted(offs.values())
        for i, o in offs.items():
            near = sorted(offs[j] for j in offs if j != i and abs(j - i) <= 6)
            if abs(o) > max_shift:
                bad.append(i)
            elif len(near) >= 2:                       # enough lines around it to compare with
                if abs(o - near[len(near) // 2]) > 6.0:
                    bad.append(i)
            elif len(everyone) >= 4:                   # few neighbours: compare with the whole song
                if abs(o - everyone[len(everyone) // 2]) > 12.0:
                    bad.append(i)
            elif abs(o) > 25.0:                        # hardly anything heard: be strict
                bad.append(i)
        for i in bad:
            starts[i] = None
            words_t[i] = None
            ends[i] = None
            matched -= 1

    # keep lines in order: drop any AI start that isn't after the previous one
    last = -1.0
    for i in range(n):
        if starts[i] is None:
            continue
        if starts[i] <= last + 0.1:
            starts[i] = None
            words_t[i] = None
            ends[i] = None
            matched -= 1
        else:
            last = starts[i]

    heard_flags = [starts[i] is not None for i in range(n)]
    _fill_gaps(lines, starts, orig_starts, line_tok)
    word_times = [_line_word_times(lines[i], starts[i], _next_start(starts, i), words_t[i])
                  if words_t[i] else None for i in range(n)]
    coverage = len(anchors) / max(len(lyr), 1)
    return AlignResult(starts, word_times, ends, matched, lyric_lines, coverage, heard_flags,
                       [(w.text, round(w.start, 2), round(w.end, 2)) for w in heard], len(anchors))


def _next_start(starts, i):
    return next((starts[j] for j in range(i + 1, len(starts)) if starts[j] is not None), None)


def _fill_gaps(lines, starts, orig, line_tok):
    """Lines the AI didn't hear: use the normal timing moved by the nearby AI correction,
    or (for lyrics without any timing) spread them evenly between heard lines."""
    n = len(lines)
    known = [i for i in range(n) if starts[i] is not None]
    if orig is not None and any(t is not None for t in orig):
        offs = {i: starts[i] - orig[i] for i in known if i < len(orig) and orig[i] is not None}
        keys = sorted(offs)
        for i in range(n):
            if starts[i] is not None or i >= len(orig) or orig[i] is None:
                continue
            if not keys:
                starts[i] = orig[i]
                continue
            before = max((k for k in keys if k < i), default=None)
            after = min((k for k in keys if k > i), default=None)
            if before is None:
                off = offs[after]
            elif after is None:
                off = offs[before]
            else:
                w = (i - before) / (after - before)
                off = offs[before] * (1 - w) + offs[after] * w
            starts[i] = max(0.0, orig[i] + off)
    else:
        if not known:
            return
        weight = [max(len(line_tok[i]), 1) if line_tok[i] else 0 for i in range(n)]
        # average seconds per word from the heard lines
        spans = [(starts[b] - starts[a]) / max(sum(weight[a:b]), 1) for a, b in zip(known, known[1:])]
        per = sorted(spans)[len(spans) // 2] if spans else 0.45
        per = min(max(per, 0.2), 1.2)
        for a, b in zip([None] + known, known + [None]):
            lo = (a + 1) if a is not None else 0
            hi = b if b is not None else n
            if lo >= hi:
                continue
            if a is not None and b is not None:
                total = max(sum(weight[a:b]), 1)
                acc = sum(weight[a:lo])
                for i in range(lo, hi):
                    if line_tok[i]:
                        starts[i] = starts[a] + (starts[b] - starts[a]) * acc / total
                    acc += weight[i]
            elif b is not None:          # before the first heard line
                t = starts[b]
                for i in range(hi - 1, lo - 1, -1):
                    if line_tok[i]:
                        t -= weight[i] * per + 0.3
                        starts[i] = max(0.0, t)
            else:                        # after the last heard line
                t = starts[a] + weight[a] * per + 0.3
                for i in range(lo, hi):
                    if line_tok[i]:
                        starts[i] = t
                        t += weight[i] * per + 0.3
    # never go backwards
    last = -1.0
    for i in range(n):
        if starts[i] is None:
            continue
        if starts[i] <= last:
            starts[i] = last + 0.05
        last = starts[i]


def _line_word_times(text: str, start, next_start, heard: dict):
    """Start/end of every word in the line: heard words exactly, the rest filled in between."""
    words = text.split()
    if not words or start is None:
        return None
    times: list = [heard.get(k) for k in range(len(words))]
    end_limit = (next_start if next_start is not None else start + 8.0)
    last_end = max((t[1] for t in times if t), default=start + 0.3 * len(words))
    end_limit = min(end_limit, last_end + 1.2)
    k = 0
    while k < len(words):
        if times[k] is not None:
            k += 1
            continue
        j = k
        while j < len(words) and times[j] is None:
            j += 1
        lo = times[k - 1][1] if k > 0 else start
        hi = times[j][0] if j < len(words) else end_limit
        hi = max(hi, lo + 0.05 * (j - k))
        step = (hi - lo) / (j - k)
        for m in range(k, j):
            times[m] = (lo + step * (m - k), lo + step * (m - k + 1))
        k = j
    times[0] = (start, max(times[0][1], start + 0.05))
    out, prev = [], start
    for s, e in times:
        s = max(s, prev)
        e = max(e, s + 0.05)
        out.append((s, e))
        prev = s
    return out


def rematch(lrc_path: Path, title: str, artist: str) -> tuple[str | None, AlignResult | None, str]:
    """Match the words the AI heard to the lyrics again (e.g. after the app got better at it),
    without listening to the song again."""
    d = load_details(lrc_path)
    if not d or not d.get("words"):
        return None, None, "this song has no saved words - play it once more with AI sync on"
    heard = [Heard(w[0], float(w[1]), float(w[2])) for w in d["words"]]
    res = align(d["lines"], heard, d.get("orig"))
    note = f"AI heard {res.matched_lines}/{res.lyric_lines} lines"
    return to_lrc(d["lines"], res, title, artist, note), res, note


def details_path(lrc_path: Path) -> Path:
    return lrc_path.with_suffix(".ai.json")


def save_details(lrc_path: Path, lines: list[str], res: AlignResult, orig_starts: list | None = None,
                 extra: dict | None = None) -> None:
    """Next to an AI-synced song: which lines the AI heard and which it had to fill in, plus the
    words it heard (a few KB) so the song can be matched again later without listening again."""
    try:
        data = {"lines": [ln for ln in lines], "heard": [bool(h) for h in res.heard],
                "matched": res.matched_lines, "total": res.lyric_lines,
                "words": [list(w) for w in res.words], "orig": orig_starts, **(extra or {})}
        details_path(lrc_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.exception("could not save AI sync details")


def load_details(lrc_path: Path) -> dict | None:
    try:
        return json.loads(details_path(lrc_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ts(t: float, br="[]") -> str:
    t = max(0.0, t)
    m = int(t // 60)
    return f"{br[0]}{m:02d}:{t - m * 60:05.2f}{br[1]}"


def to_lrc(lines: list[str], res: AlignResult, title: str, artist: str, quality_note: str = "") -> str:
    """The AI timing as an .lrc file (with word timing where the words were heard)."""
    out = [f"[ti:{title}]", f"[ar:{artist}]", "[by:Spotify Lyrics Overlay AI sync]"]
    if quality_note:
        out.append(f"[#:{quality_note}]")
    timed = [(res.starts[i], i) for i in range(len(lines)) if res.starts[i] is not None]
    for pos, (t, i) in enumerate(timed):
        text = lines[i].strip()
        wt = res.word_times[i]
        if text and wt and len(wt) == len(text.split()):
            body = " ".join(f"{_ts(s, '<>')}{w}" for (s, _), w in zip(wt, text.split()))
            body += " " + _ts(wt[-1][1], "<>")
            out.append(f"{_ts(t)}{body}")
        else:
            out.append(f"{_ts(t)}{text}")
        # a long silence before the next line = an instrumental break
        nxt = timed[pos + 1][0] if pos + 1 < len(timed) else None
        end = res.line_ends[i]
        if text and end is not None and (nxt is None or nxt - end > 4.0):
            out.append(_ts(end + 0.6))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- models

def models_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "SpotifyLyricsOverlay" / "ai_models"


class ModelStore:
    def __init__(self, folder: Path | None = None, opener=None):
        self.folder = folder or models_dir()
        self.opener = opener or urllib.request.build_opener()

    def path(self, name: str) -> Path:
        return self.folder / name

    def installed(self, name: str) -> bool:
        """Downloaded completely (every file the download listed is there)."""
        p = self.path(name)
        try:
            info = json.loads((p / "ready.json").read_text(encoding="utf-8"))
            files = [f["path"] for f in info.get("files", [])]
        except (OSError, ValueError):
            return False
        return bool(files) and all((p / f).exists() for f in files)

    def size_on_disk(self, name: str) -> int:
        p = self.path(name)
        if not p.exists():
            return 0
        return sum(f.stat().st_size for f in p.iterdir() if f.is_file())

    def remove(self, name: str) -> None:
        shutil.rmtree(self.path(name), ignore_errors=True)

    def _get(self, url: str, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self.opener.open(req, timeout=timeout)

    def file_list(self, name: str) -> list[dict]:
        repo = MODELS[name][0]
        with self._get(f"{HF}/api/models/{repo}/tree/main") as r:
            items = json.loads(r.read().decode("utf-8"))
        files = []
        for it in items:
            if it.get("type") == "file" and it.get("path") in NEEDED_FILES:
                lfs = it.get("lfs") or {}
                files.append({"path": it["path"], "size": int(lfs.get("size") or it.get("size") or 0),
                              "sha256": lfs.get("oid") if lfs else None})
        if not any(f["path"] in ("model.bin", "vocals.fp16.onnx") for f in files):
            raise RuntimeError("the model files weren't found on Hugging Face")
        return files

    def download(self, name: str, progress=None, cancel: threading.Event | None = None) -> Path:
        """Download a model. progress(done_bytes, total_bytes). Raises on failure."""
        files = self.file_list(name)
        total = sum(f["size"] for f in files)
        target = self.path(name)
        target.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target).free
        if free < total + 200 * 1024 * 1024:
            raise RuntimeError(f"not enough free space (needs {total / 1e9:.1f} GB)")
        (target / "ready.json").unlink(missing_ok=True)
        repo = MODELS[name][0]
        done = 0
        for f in files:
            dest = target / f["path"]
            if dest.exists() and dest.stat().st_size == f["size"] and f["size"] > 0:
                done += f["size"]
                if progress:
                    progress(done, total)
                continue
            part = dest.with_suffix(dest.suffix + ".part")
            h = hashlib.sha256()
            with self._get(f"{HF}/{repo}/resolve/main/{f['path']}", timeout=60) as r, open(part, "wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        out.close()
                        part.unlink(missing_ok=True)
                        raise InterruptedError("download cancelled")
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if f["sha256"] and h.hexdigest() != f["sha256"]:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{f['path']} didn't download correctly (checksum mismatch)")
            os.replace(part, dest)
        (target / "ready.json").write_text(json.dumps({"repo": repo, "files": files}), encoding="utf-8")
        log.info("AI model %s downloaded (%.0f MB)", name, total / 1e6)
        return target


def runtime_available() -> str | None:
    """None if the AI parts are installed in this build, otherwise the reason."""
    try:
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
    except Exception as e:      # pragma: no cover - depends on the build
        return f"speech recognition isn't included in this build ({e})"
    return None


# --------------------------------------------------------------------------- listening

@dataclass
class Segment:
    start: float                       # song position where this piece of recording starts
    frames: list = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return sum(len(f) for f in self.frames) / SAMPLE_RATE


class Recording:
    """Song audio kept in memory, split wherever the song was paused or jumped."""

    def __init__(self, key, duration: float):
        self.key = key
        self.duration = duration
        self.segments: list[Segment] = []
        self._expect = None

    def add(self, frame, end_pos: float):
        secs = len(frame) / SAMPLE_RATE
        start = end_pos - secs
        if self._expect is None or abs(start - self._expect) > 1.0 or not self.segments:
            self.segments.append(Segment(max(0.0, start)))
        self.segments[-1].frames.append(frame)
        self._expect = end_pos

    def gap(self):
        self._expect = None

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.segments)

    def covered(self) -> float:
        """How much of the song (seconds) has been heard at least once."""
        spans = sorted((s.start, s.start + s.seconds) for s in self.segments)
        total, cur_s, cur_e = 0.0, None, None
        for a, b in spans:
            if cur_e is None or a > cur_e:
                if cur_e is not None:
                    total += cur_e - cur_s
                cur_s, cur_e = a, b
            else:
                cur_e = max(cur_e, b)
        if cur_e is not None:
            total += cur_e - cur_s
        return total


def run_probe(pid: int, timeout: float = 25.0) -> dict:
    """Check, in a separate process, that Windows lets us hear just this app.
    A separate process means a problem there can never crash the overlay."""
    import subprocess
    import sys
    import tempfile
    out = Path(tempfile.gettempdir()) / f"slo-probe-{os.getpid()}-{pid}.json"
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py")]
    cmd += ["--app-audio-probe", str(pid), str(out)]
    try:
        out.unlink(missing_ok=True)
        subprocess.run(cmd, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rep = json.loads(out.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        rep = {"ok": False, "error": "the check took too long"}
    except Exception as e:
        rep = {"ok": False, "error": f"the check didn't finish ({e})"}
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
    return rep


class Listener:
    """Records the song on one long-lived thread: just the music app when Windows allows
    it ("app only"), otherwise everything the PC plays (WASAPI loopback).

    get_state() -> (track_key, position_seconds, playing) is asked after every half
    second of sound so the audio can be lined up with the song.
    get_target() -> process id of the music app to listen to, or None for the whole PC.
    """

    def __init__(self, get_state, get_target=None):
        self.get_state = get_state
        self.get_target = get_target
        self.method = ""                 # "Spotify only" / "whole PC" - shown in Settings
        self.app_note = ""               # why app-only isn't used, if it isn't
        self._probed: dict[int, dict] = {}
        self._hold_until = 0.0           # ignore sound until then (just after Spotify was told to jump)
        self._tail = None                # (recording, song position, clock time, end) - see start_tail
        self.recording: Recording | None = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._quit = False
        self._thread = threading.Thread(target=self._run, name="ai-listen", daemon=True)
        self._thread.start()

    def arm(self, key, duration: float):
        with self._lock:
            if self.recording is None or self.recording.key != key:
                self.recording = Recording(key, duration)
        self._wake.set()

    def hold(self, seconds: float):
        """Ignore the sound for a moment - right after a jump, Spotify may still be playing the old spot."""
        self._hold_until = time.monotonic() + seconds

    def start_tail(self, rec: Recording, position: float, end: float):
        """Spotify's Crossfade starts the next song while the last seconds of this one still play
        (mixed together). Keep adding those seconds to this song's recording, at the time they'd have."""
        with self._lock:
            self._tail = (rec, position, time.monotonic(), end)
        self._wake.set()

    def tail_active(self) -> bool:
        return self._tail is not None

    def take(self) -> Recording | None:
        with self._lock:
            rec, self.recording = self.recording, None
        return rec

    def peek(self) -> Recording | None:
        return self.recording

    def stop(self):
        self._quit = True
        self._wake.set()

    def _run(self):
        try:
            import numpy as np
            import soundcard as sc          # imported here so COM is set up on this thread
        except Exception as e:
            self.error = f"can't listen to the PC's sound ({e})"
            log.warning("AI listener unavailable: %s", e)
            return
        while not self._quit:
            self._wake.wait(1.0)
            self._wake.clear()
            if self._quit:
                return
            if self.recording is None and self._tail is None:
                continue
            pid = None
            try:
                pid = self.get_target() if self.get_target else None
            except Exception:
                log.exception("finding the music app failed")
            if pid and self._app_allowed(pid):
                try:
                    from .app_audio import AppAudio
                    with AppAudio(pid) as src:
                        self.method, self.error = "Spotify only", None
                        self._pump(lambda n: src.read(n), np,
                                   still_ok=lambda: (self.get_target() if self.get_target else None) == pid)
                    continue
                except Exception as e:
                    self._probed[pid] = {"ok": False, "error": str(e)}
                    self.app_note = f"couldn't hear the app by itself ({e})"
                    log.warning("AI listener (app only): %s - using the whole PC", e)
            try:
                speaker = sc.default_speaker()
                mic = sc.get_microphone(id=str(speaker.id), include_loopback=True)
                log.info("AI sync listening to the whole PC: %s", speaker.name)
                with mic.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=1024) as rec:
                    self.method, self.error = "whole PC", None
                    want_app = bool(pid) and pid not in self._probed

                    def same_output():
                        if want_app or ((self.get_target() if self.get_target else None) != pid):
                            return False      # the music app appeared / changed: try app-only again
                        return sc.default_speaker().id == speaker.id
                    self._pump(lambda n: rec.record(numframes=n), np, still_ok=same_output)
            except Exception as e:
                self.error = f"couldn't listen to the PC's sound ({e})"
                log.warning("AI listener: %s", e)
                time.sleep(3)

    def _app_allowed(self, pid: int) -> bool:
        """First time for this app: check in a separate process that app-only listening works."""
        if pid not in self._probed:
            rep = run_probe(pid)
            self._probed[pid] = rep
            log.info("app-only listening check: %s", rep)
            self.app_note = "" if rep.get("ok") else f"couldn't hear the app by itself ({rep.get('error')})"
        return bool(self._probed[pid].get("ok"))

    def _pump(self, read, np, still_ok):
        """Read half-second pieces into the current recording until something changes."""
        checks = 0
        while not self._quit:
            r = self.recording
            if r is None and self._tail is None:
                return
            data = read(int(SAMPLE_RATE * FRAME_SECONDS))
            frame = np.asarray(data, dtype=np.float32).reshape(-1).copy()
            tail = self._tail
            if tail is not None:                # the end of the last song, still fading out
                trec, p0, m0, end = tail
                p = p0 + (time.monotonic() - m0)
                if p <= end + 0.3:
                    trec.add(frame, min(p, end))
                else:
                    self._tail = None
            key, pos, playing = self.get_state()
            if r is None or r is not self.recording:
                continue
            if key != r.key or not playing or time.monotonic() < self._hold_until:
                r.gap()
            elif r.seconds <= MAX_RECORD_SECONDS:
                r.add(frame, pos)
            checks += 1
            if checks % 10 == 0 and not still_ok():
                log.info("AI sync: sound source changed - switching")
                return


# --------------------------------------------------------------------------- the AI

def load_whisper(model_dir: Path, threads: int = 2):
    from faster_whisper import WhisperModel
    return WhisperModel(str(model_dir), device="cpu", compute_type="int8", cpu_threads=threads)


def transcribe(model, audio, language: str | None = None, prompt: str | None = None,
               threads: int = 2, progress=None, parts: list | None = None,
               hotwords: str | None = None) -> list[Heard]:
    """Run speech recognition on 16 kHz mono float audio; returns every word with its time.
    `model` is a loaded model (load_whisper) or a model folder."""
    own = isinstance(model, (str, Path))
    if own:
        model = load_whisper(Path(model), threads)
    try:
        extra = {}
        if parts:                          # voice-only audio: only listen where someone is singing
            extra = {"clip_timestamps": [x for ab in parts for x in ab], "hallucination_silence_threshold": 2.0}
        if hotwords:                       # words it should expect to hear (makes them easier to catch)
            extra["hotwords"] = hotwords
        kw = dict(language=language, word_timestamps=True, vad_filter=False,
                  condition_on_previous_text=False, initial_prompt=prompt, beam_size=5,
                  no_speech_threshold=None,      # never skip a part just because the music is louder than the voice
                  **extra)
        try:
            segments, info = model.transcribe(audio, **kw)
        except TypeError:                  # an older speech library without "hotwords"
            kw.pop("hotwords", None)
            segments, info = model.transcribe(audio, **kw)
        words: list[Heard] = []
        total = max(len(audio) / SAMPLE_RATE, 0.1)
        for seg in segments:
            for w in (seg.words or []):
                words.append(Heard(w.word.strip(), float(w.start), float(w.end)))
            if progress:
                progress(min(1.0, float(seg.end) / total))
        return words
    finally:
        if own:
            del model


def cli_test(model: str, wav: str, lrc: str, out: str, shift: float = 2.0, store_dir: str | None = None,
             options: str = "") -> int:
    """CI: download `model`, hear `wav`, line it up with `lrc` whose times were moved by
    `shift` seconds (like a badly-timed lyrics website) and write what happened to `out`."""
    import wave

    import numpy as np

    from .lrc import parse_lrc
    t0 = time.time()
    store = ModelStore(Path(store_dir) if store_dir else None)
    opts = {o.strip() for o in (options or "").split(",") if o.strip()}
    global DEBUG_DUMP
    DEBUG_DUMP = str(Path(out).with_suffix(".vocals.wav")) if "vocals" in opts else None
    report: dict = {"model": model, "options": sorted(opts)}
    try:
        if not store.installed(model):
            store.download(model)
        if "vocals" in opts and not store.installed("vocals"):
            t_v = time.time()
            store.download("vocals")
            report["vocals_download_s"] = round(time.time() - t_v, 1)
            report["vocals_size_mb"] = round(store.size_on_disk("vocals") / 1e6)
        report["download_s"] = round(time.time() - t0, 1)
        report["size_mb"] = round(store.size_on_disk(model) / 1e6)
        with wave.open(wav, "rb") as w:
            raw = w.readframes(w.getnframes())
            rate, ch = w.getframerate(), w.getnchannels()
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            audio = audio.reshape(-1, ch).mean(axis=1)
        if rate != SAMPLE_RATE:
            idx = np.arange(0, len(audio), rate / SAMPLE_RATE)
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        parsed = parse_lrc(Path(lrc).read_text(encoding="utf-8"))
        truth = [ln.start for ln in parsed]
        rec = Recording("test", len(audio) / SAMPLE_RATE)
        for k in range(0, len(audio), 8000):
            rec.add(audio[k:k + 8000], (k + len(audio[k:k + 8000])) / SAMPLE_RATE)
        job = Job("test", "CI song", "CI", [ln.text for ln in parsed],
                  [t + shift for t in truth], rec, second_pass="second" in opts,
                  vocals_dir=store.path("vocals") if "vocals" in opts else None)
        t1 = time.time()
        text, res, msg = run_job(job, store.path(model))
        report.update(hear_s=round(time.time() - t1, 1), message=msg, lrc=text)
        if res is not None:
            errs = [abs(a - b) for a, b, ln in zip(res.starts, truth, parsed) if ln.text.strip() and a is not None]
            report.update(matched=res.matched_lines, lines=res.lyric_lines,
                          max_error=round(max(errs), 2) if errs else None,
                          mean_error=round(sum(errs) / len(errs), 2) if errs else None,
                          starts=[round(x, 2) if x is not None else None for x in res.starts], truth=truth)
        if res is not None:
            report["missed"] = [ln.text for ln, h in zip(parsed, res.heard) if ln.text.strip() and not h]
        if res is not None and "patch" in opts:
            # pretend the first listen missed the middle of the song, then use "listen to missed parts"
            dur = len(audio) / SAMPLE_RATE
            gone = (0.3 * dur, 0.65 * dur)
            kept = [w for w in res.words if not (gone[0] <= w[1] < gone[1])]
            lines_ = [ln.text for ln in parsed]
            details = {"lines": lines_, "words": [list(w) for w in kept], "orig": [t + shift for t in truth]}
            before, plan = plan_missed_parts(details)
            prec = Recording("test", dur)
            for a, b in plan:
                for k in range(int(a * SAMPLE_RATE), min(int(b * SAMPLE_RATE), len(audio)), 8000):
                    piece = audio[k:min(k + 8000, int(b * SAMPLE_RATE))]
                    prec.add(piece, (k + len(piece)) / SAMPLE_RATE)
                prec.gap()
            t2 = time.time()
            ptext, pres, pmsg = patch_job(details, prec, store.path(model), "CI song", "CI")
            perrs = [abs(a - b) for a, b, ln, h in zip(pres.starts, truth, parsed, pres.heard)
                     if ln.text.strip() and a is not None and h]
            report.update(patch_before=before.matched_lines, patch_after=pres.matched_lines, patch_msg=pmsg,
                          patch_plan=[[round(a, 1), round(b, 1)] for a, b in plan],
                          patch_s=round(time.time() - t2, 1), patch_new_words=pres.new_words,
                          patch_max_error=round(max(perrs), 2) if perrs else None)
            report["message"] = (f"missed-parts listen: {before.matched_lines} -> {pres.matched_lines} lines, "
                                 f"{pmsg}, worst {report['patch_max_error']} s, {report['patch_s']} s")
        report["ok"] = bool(text) and report.get("max_error") is not None and report["max_error"] < 0.8
    except Exception as e:
        log.exception("AI test failed")
        report.update(ok=False, error=repr(e))
    Path(out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0 if report.get("ok") else 1


def listen_test(out: str, seconds: float = 6.0) -> int:
    """CI: can this PC's sound be recorded? Writes how loud the recording was."""
    rep: dict = {}
    state = {"pos": 0.0}
    t0 = time.monotonic()

    def get_state():
        return "t", time.monotonic() - t0, True
    try:
        import soundcard as sc
        rep["speakers"] = [s.name for s in sc.all_speakers()]
    except Exception as e:
        rep["speakers_error"] = repr(e)
    lis = Listener(get_state)
    lis.arm("t", 60)
    time.sleep(seconds)
    rec = lis.take()
    lis.stop()
    rep["error"] = lis.error
    if rec is not None and rec.segments:
        import numpy as np
        a = np.concatenate([f for s in rec.segments for f in s.frames])
        rep.update(seconds=round(len(a) / SAMPLE_RATE, 2), rms=float(np.sqrt(np.mean(a * a))) if len(a) else 0.0)
    rep["ok"] = rep.get("seconds", 0) > 1
    del state
    if sys.platform == "win32":
        rep["app_only_check_on_this_process"] = run_probe(os.getpid())
    Path(out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return 0


def whisper_language(lines: list[str]) -> str | None:
    try:
        from .translate import detect_language
        code = detect_language(lines)
    except Exception:
        return None
    return {"zh-CN": "zh", "zh-TW": "zh"}.get(code, code)


@dataclass
class Job:
    key: object
    title: str
    artist: str
    lines: list[str]
    orig_starts: list | None
    recording: Recording
    second_pass: bool = False          # re-listen to the parts it missed, with those lyrics as a hint
    vocals_dir: Path | None = None     # separate the voice from the music first (model folder), or None


DEBUG_DUMP: str | None = None       # CI only: save the separated voice here, to check it


def _dump_wav(path: Path, audio) -> None:
    import wave

    import numpy as np
    a = np.clip(np.asarray(audio, dtype=np.float32), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((a * 32767).astype(np.int16).tobytes())


def missed_windows(res: AlignResult, lines: list[str], max_len: float = 26.0) -> list[tuple[float, float, list]]:
    """Stretches of the song where lines weren't heard: (start, end, line numbers), each short
    enough for one listen (speech recognition works on up to 30 s at a time)."""
    n = len(lines)
    heard = res.heard or [False] * n
    sung = [i for i in range(n) if lines[i].strip() and res.starts[i] is not None]
    missed = [i for i in sung if not heard[i]]
    if not missed:
        return []

    def prev_heard_end(i):
        for k in range(i - 1, -1, -1):
            if heard[k] and res.starts[k] is not None:
                e = res.line_ends[k] if res.line_ends and res.line_ends[k] is not None else res.starts[k] + 1.0
                return e
        return None

    def next_start(i):
        for k in range(i + 1, n):
            if lines[k].strip() and res.starts[k] is not None:
                return res.starts[k]
        return None

    out = []
    chunk: list[int] = []
    t0 = 0.0
    for i in missed:
        start = res.starts[i]
        if chunk and (start - t0 > max_len - 4 or any(heard[k] for k in range(chunk[-1] + 1, i)
                                                       if lines[k].strip())):
            nxt = next_start(chunk[-1])
            out.append((t0, min((nxt if nxt is not None else res.starts[chunk[-1]] + 6) + 1.0, t0 + max_len), chunk))
            chunk = []
        if not chunk:
            pe = prev_heard_end(i)
            t0 = max(0.0, start - 2.5 if pe is None else max(start - 2.5, pe - 0.5))
        chunk.append(i)
    if chunk:
        nxt = next_start(chunk[-1])
        out.append((t0, min((nxt if nxt is not None else res.starts[chunk[-1]] + 6) + 1.0, t0 + max_len), chunk))
    return out


def run_job(job: Job, model_dir: Path, threads: int = 2, progress=None) -> tuple[str | None, AlignResult | None, str]:
    """Hear the recorded song and build the AI-timed .lrc. Returns (lrc, result, message).
    progress(fraction 0..1) is called as the AI works through the song.

    Steps: (optional) separate the voice from the music -> listen to the whole song ->
    match the words to the lyrics -> (optional) listen again to just the parts it missed,
    this time told which lyrics to expect there -> match again, keeping whatever is better."""
    import numpy as np
    lang = whisper_language(job.lines)
    prompt = " ".join(ln for ln in job.lines if ln.strip())[:200] or None
    pieces = [s for s in job.recording.segments if s.seconds >= 8]
    total = max(sum(s.seconds for s in pieces), 0.1)
    # how much of the progress bar each step gets
    w_sep = 0.35 if job.vocals_dir else 0.0
    w_pass2 = 0.25 if (job.second_pass or job.vocals_dir) else 0.0
    w_pass1 = 1.0 - w_sep - w_pass2

    def report(x):
        if progress:
            progress(min(1.0, max(0.0, x)))
    report(0.0)
    model = load_whisper(model_dir, threads) if isinstance(model_dir, (str, Path)) else model_dir
    extras: list[str] = []

    # ---- 1. the song as recorded, and (if asked) a voice-only copy
    orig_pieces: list[tuple[float, object]] = []
    voc_pieces: list[tuple[float, object]] = []
    loud = 0.0
    for seg in pieces:
        audio = np.concatenate(seg.frames).astype(np.float32)
        loud = max(loud, float(np.sqrt(np.mean(audio * audio))) if len(audio) else 0.0)
        orig_pieces.append((seg.start, audio))
    if loud < 1e-4:
        return None, None, "the recording was silent (is Spotify playing on a different sound device?)"
    if job.vocals_dir:
        try:
            from .vocals import VocalSeparator
            separator = VocalSeparator(job.vocals_dir, threads)
            done = 0.0
            for start, audio in orig_pieces:
                n = len(audio) / SAMPLE_RATE
                voice = separator.isolate(audio, SAMPLE_RATE,
                                          lambda f, d=done, n=n: report(w_sep * (d + f * n) / total))
                voc_pieces.append((start, voice))
                if DEBUG_DUMP:
                    _dump_wav(Path(DEBUG_DUMP), voice)
                done += n
        except Exception as e:
            log.warning("vocal separation didn't work: %s", e)
            voc_pieces = []
            extras.append("vocal separation didn't work")
    report(w_sep)

    # ---- 2. listen to the whole song (the normal recording - on its own the voice-only copy loses
    #         too much, so it's only used below to catch lines this listen missed)
    heard: list[Heard] = []
    done = 0.0
    for start, audio in orig_pieces:
        n = len(audio) / SAMPLE_RATE
        part = lambda f, d=done, n=n: report(w_sep + w_pass1 * (d + f * n) / total)
        for w in transcribe(model, audio, lang, prompt, threads, part):
            heard.append(Heard(w.text, w.start + start, w.end + start))
        done += n
    heard.sort(key=lambda w: w.start)
    log.info("AI heard %d words in %s (from %.0f s to %.0f s)", len(heard), job.title,
             heard[0].start if heard else 0, heard[-1].end if heard else 0)
    res = align(job.lines, heard, job.orig_starts)
    report(w_sep + w_pass1)

    # ---- 3. go back over the parts it missed, told which lyrics to expect there: in the normal
    #         recording ("second listen") and/or the voice-only copy. A result is only kept if
    #         more lines are matched, so these can't make the timing worse.
    sources = []
    if job.second_pass:
        sources.append(("2nd listen", orig_pieces, False))
    if voc_pieces:
        sources.append(("voice track", voc_pieces, True))
    if sources and res.matched_lines < res.lyric_lines:
        heard, res, gains = repair(job.lines, job.orig_starts, heard, res, sources, model, lang, threads,
                                   lambda f: report(w_sep + w_pass1 + w_pass2 * f))
        for name, g in gains.items():
            if g:
                extras.append(f"{name} found {g} more")
    elif voc_pieces:
        extras.append("voice track not needed")
    report(1.0)

    need = 0.3 if job.orig_starts else 0.55
    if res.quality < need:
        return None, res, (f"only heard {res.matched_lines} of {res.lyric_lines} lines clearly - "
                           "kept the normal timing")
    note = f"AI heard {res.matched_lines}/{res.lyric_lines} lines" + (f" ({', '.join(extras)})" if extras else "")
    return to_lrc(job.lines, res, job.title, job.artist, note), res, note


def _hint_for(lines, res, idxs):
    """What to tell the AI it's about to hear: the line before (for context) and the missed lines,
    plus their less common words as 'hotwords'."""
    prev = next((lines[k] for k in range(idxs[0] - 1, -1, -1) if lines[k].strip()), "")
    want = " ".join(lines[i] for i in idxs)
    prompt = (prev + " " + want).strip()[-220:]
    seen, hot = set(), []
    for t in tokens_of(want):
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            hot.append(t)
    return prompt, " ".join(hot[:30]) or None


def _line_spans(lines, res, idxs, t0, t1):
    """Short stretches, one per missed line (about where it should be), for a closer listen."""
    out = []
    for i in idxs:
        s = res.starts[i]
        if s is None:
            continue
        nxt = _next_start(res.starts, i)
        e = min(nxt + 0.8 if nxt is not None else s + 6.0, s + 12.0)
        a, b = max(t0, s - 1.5), min(t1, max(e, s + 3.0))
        if b - a >= 2.5:
            out.append((a, b, [i]))
    return out


def repair(lines, orig, heard, res, sources, model, lang, threads, progress=None, thorough: bool = False):
    """Go over the parts where lines weren't matched again, told which lyrics to expect there.
    sources: (name, audio pieces [(song time, samples)], voice_only). Keeps a new result only when
    it matches MORE lines. thorough: also try each missed line on its own (short clips are easier
    for the AI). Returns (heard words, result, {source name: lines gained})."""
    from .vocals import voiced_parts
    windows = missed_windows(res, lines)[:24]
    before = res.matched_lines
    gains = {name: 0 for name, _, _ in sources}
    steps = max(len(windows) * len(sources) * (2 if thorough else 1), 1)
    step = 0

    def attempt(name, src, voice_only, t0, t1, idxs, keep_until):
        nonlocal heard, res
        clip = _clip(src, t0, t1)
        if clip is None:
            return
        c0, audio = clip
        span = len(audio) / SAMPLE_RATE
        parts = None
        if voice_only:                              # only where someone sings: no words "heard" in silence
            parts = voiced_parts(audio, SAMPLE_RATE)
            if not parts:
                return
        prompt, hot = _hint_for(lines, res, idxs)
        try:
            if hot:
                again = transcribe(model, audio, lang, prompt, threads, None, parts, hotwords=hot)
            else:
                again = transcribe(model, audio, lang, prompt, threads, None, parts)
        except Exception as e:
            log.warning("%s failed: %s", name, e)
            return
        if not again or len(again) > 7 * max(span, 1.0):        # nonsense (far too many words)
            return
        # only the missed part itself is replaced: the clip runs a little into the next (already
        # heard) line so a late start isn't cut off, but those words are kept from the first listen
        core_end = min(c0 + span, keep_until)
        got = [Heard(w.text, w.start + c0, w.end + c0) for w in again if w.start + c0 < core_end]
        cand = sorted([w for w in heard if not (c0 <= w.start < core_end)] + got, key=lambda w: w.start)
        res2 = align(lines, cand, orig, focus=(idxs, (c0 - 0.5, core_end + 0.5)))
        if res2.matched_lines > res.matched_lines:
            gains[name] += res2.matched_lines - res.matched_lines
            heard, res = cand, res2

    for t0, t1, idxs in windows:
        for name, src, voice_only in sources:
            if progress:
                progress(step / steps)
            step += 1
            if all(res.heard[i] for i in idxs):
                break                               # an earlier source already got these lines
            attempt(name, src, voice_only, t0, t1, idxs, t1 - 1.0)
        if thorough:
            for a, b, one in _line_spans(lines, res, [i for i in idxs if not res.heard[i]], t0, t1):
                for name, src, voice_only in sources:
                    if res.heard[one[0]]:
                        break
                    k = next((j for j in range(one[0] + 1, len(lines)) if lines[j].strip()), None)
                    nxt = res.starts[k] if k is not None else None
                    keep = (nxt - 0.2 if res.heard[k] else nxt + 0.3) if nxt is not None else b
                    attempt(name, src, voice_only, a, b, one, keep)
            if progress:
                progress(step / steps)
            step += len(sources)
    if progress:
        progress(1.0)
    log.info("going over missed parts: %d -> %d lines %s", before, res.matched_lines, gains)
    return heard, res, gains


def plan_missed_parts(details: dict, merge_gap: float = 6.0, lead: float = 2.0, tail: float = 1.0):
    """For a song the AI already synced: the stretches of the song to play again so it can hear the
    lines it missed (close ones joined, so Spotify doesn't jump back and forth).
    Returns (current result, [(start, end) in song seconds])."""
    lines = details.get("lines") or []
    words = [Heard(w[0], float(w[1]), float(w[2])) for w in details.get("words") or []]
    res = align(lines, words, details.get("orig"))
    plan: list[list[float]] = []
    for t0, t1, _ in missed_windows(res, lines):
        a, b = max(0.0, t0 - lead), t1 + tail
        if plan and a - plan[-1][1] < merge_gap:
            plan[-1][1] = max(plan[-1][1], b)
        else:
            plan.append([a, b])
    return res, [(a, b) for a, b in plan]


def patch_job(details: dict, recording: Recording, model_dir, title: str, artist: str, threads: int = 2,
              vocals_dir=None, progress=None) -> tuple[str | None, AlignResult | None, str]:
    """Listen to just the recorded missed parts (with the expected lyrics as a hint, and the voice
    track if it's on) and merge what's heard into the song's saved words."""
    import numpy as np
    lines, orig = details["lines"], details.get("orig")
    heard = [Heard(w[0], float(w[1]), float(w[2])) for w in details.get("words") or []]
    res = align(lines, heard, orig)
    before = res.matched_lines
    pieces = [(seg.start, np.concatenate(seg.frames).astype(np.float32))
              for seg in recording.segments if seg.seconds >= 2]
    if not pieces:
        return None, res, "nothing was recorded"
    if progress:
        progress(0.0)
    model = load_whisper(model_dir, threads) if isinstance(model_dir, (str, Path)) else model_dir
    sources = [("new listen", pieces, False)]
    if vocals_dir:
        try:
            from .vocals import VocalSeparator
            sep = VocalSeparator(vocals_dir, threads)
            sources.append(("voice track", [(a, sep.isolate(x, SAMPLE_RATE)) for a, x in pieces], True))
        except Exception as e:
            log.warning("vocal separation didn't work: %s", e)
    lang = whisper_language(lines)
    was_heard = list(res.heard)
    words_before = res.matched_words
    heard, res, gains = repair(lines, orig, heard, res, sources, model, lang, threads,
                               (lambda f: progress(0.1 + 0.9 * f)) if progress else None, thorough=True)
    found = res.matched_lines - before
    new_words = max(0, res.matched_words - words_before)
    res.new_lines = [i for i, h in enumerate(res.heard) if h and not (i < len(was_heard) and was_heard[i])]
    res.new_words = new_words
    note = f"AI heard {res.matched_lines}/{res.lyric_lines} lines"
    if found:
        msg = (f"found {found} more line{'s' if found != 1 else ''} "
               f"({new_words} new word{'s' if new_words != 1 else ''})")
    elif new_words:
        msg = f"caught {new_words} more word{'s' if new_words != 1 else ''}, but no whole new lines"
    else:
        msg = "didn't find any more lines"
    return to_lrc(lines, res, title, artist, note), res, msg


def _clip(audio_pieces, t0: float, t1: float):
    """The recorded audio between two song times: (actual start, samples) or None."""
    for start, audio in audio_pieces:
        end = start + len(audio) / SAMPLE_RATE
        if start <= t0 < end or (t0 < start < t1):
            a = max(t0, start)
            b = min(t1, end)
            if b - a < 1.0:
                return None
            i, j = int((a - start) * SAMPLE_RATE), int((b - start) * SAMPLE_RATE)
            return a, audio[i:j]
    return None
