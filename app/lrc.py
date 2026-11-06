"""Parsing and timing for LRC lyrics.

Plain English: a ".lrc" file is lyrics text where every line starts with a
timestamp like [01:23.45] saying when that line is sung. Some files also have
"enhanced" timestamps inside the line, like <01:23.80>, which say when each
individual WORD is sung. This module turns that text into Python objects and
answers the question "at time T, which line and which word are being sung?".

Nothing in here depends on Qt or Windows, so it can be unit-tested anywhere.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

# [mm:ss], [mm:ss.xx], [mm:ss.xxx] or [mm:ss:xx]
_LINE_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# <mm:ss.xx> word timestamps (enhanced / "A2" LRC)
_WORD_TS = re.compile(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>")
_META = re.compile(r"^\[([a-zA-Z#]+):(.*)\]\s*$")

# Used when the very last line has nothing after it to tell us when it ends.
LAST_LINE_MAX = 6.0


@dataclass
class Word:
    text: str          # the word itself, without surrounding spaces
    start: float       # seconds
    end: float         # seconds


@dataclass
class Line:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    word_timed: bool = False   # True only if the timing came from the lyrics file

    @property
    def is_break(self) -> bool:
        """An empty line = an instrumental gap in the song."""
        return not self.text.strip()


@dataclass
class Lyrics:
    lines: list[Line]
    synced: bool                 # do we have timestamps at all?
    has_word_timing: bool        # real per-word timestamps from the provider
    instrumental: bool = False
    plain_text: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        self._starts = [ln.start for ln in self.lines]

    # ---- lookups -----------------------------------------------------------
    def line_index_at(self, t: float) -> int:
        """Index of the line being sung at time t, or -1 before the first line."""
        if not self.lines:
            return -1
        return bisect.bisect_right(self._starts, t) - 1


def _ts_to_seconds(m: str, s: str, frac: str | None) -> float:
    value = int(m) * 60 + int(s)
    if frac:
        value += int(frac) / (10 ** len(frac))
    return value


def _parse_words(body: str, line_start: float) -> tuple[str, list[Word]]:
    """Split a line body that contains <mm:ss.xx> word tags into Word objects."""
    pieces = _WORD_TS.split(body)
    # re.split with 3 groups gives: [pre, m, s, frac, text, m, s, frac, text, ...]
    pre = pieces[0]
    stamps: list[tuple[float, str]] = []
    for i in range(1, len(pieces), 4):
        t = _ts_to_seconds(pieces[i], pieces[i + 1], pieces[i + 2])
        text = pieces[i + 3] if i + 3 < len(pieces) else ""
        stamps.append((t, text))

    words: list[Word] = []
    if pre.strip():
        # text before the first tag: treat as starting at the line start
        first_t = stamps[0][0] if stamps else line_start
        for w in pre.split():
            words.append(Word(w, line_start, first_t))

    for idx, (t, text) in enumerate(stamps):
        nxt = stamps[idx + 1][0] if idx + 1 < len(stamps) else None
        tokens = text.split()
        if not tokens:
            continue  # a trailing tag that only marks the end of the previous word
        end = nxt if nxt is not None else t + 0.6 * len(tokens)
        # if one tag covers several space-separated tokens, split its time evenly
        span = max(end - t, 0.01)
        for k, tok in enumerate(tokens):
            ws = t + span * k / len(tokens)
            we = t + span * (k + 1) / len(tokens)
            words.append(Word(tok, ws, we))

    plain = " ".join(w.text for w in words)
    return plain, words


def parse_lrc(text: str) -> list[Line]:
    """Parse LRC text into a sorted list of Lines (end times filled in)."""
    offset = 0.0
    raw: list[tuple[float, str]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        meta = _META.match(line)
        if meta and not _LINE_TS.match(line):
            if meta.group(1).lower() == "offset":
                try:
                    # [offset:+500] means lyrics should appear 0.5 s EARLIER
                    offset = int(meta.group(2).strip()) / 1000.0
                except ValueError:
                    pass
            continue

        stamps = []
        pos = 0
        while True:
            m = _LINE_TS.match(line, pos)
            if not m:
                break
            stamps.append(_ts_to_seconds(*m.groups()))
            pos = m.end()
        if not stamps:
            continue
        body = line[pos:].strip()
        for t in stamps:  # "[00:10.00][00:50.00]chorus" repeats the line
            raw.append((t, body))

    raw.sort(key=lambda x: x[0])
    lines: list[Line] = []
    for t, body in raw:
        t = max(0.0, t - offset)
        if _WORD_TS.search(body):
            # shift word stamps by the same offset
            plain, words = _parse_words(body, t + offset)
            for w in words:
                w.start = max(0.0, w.start - offset)
                w.end = max(0.0, w.end - offset)
            lines.append(Line(t, t, plain, words, word_timed=bool(words)))
        else:
            lines.append(Line(t, t, body))

    for i, ln in enumerate(lines):
        if i + 1 < len(lines):
            ln.end = lines[i + 1].start
        else:
            last_word_end = ln.words[-1].end if ln.words else ln.start
            ln.end = max(ln.start + LAST_LINE_MAX, last_word_end)
    return lines


def estimate_word_timing(line: Line) -> None:
    """Guess per-word timing for a line that only has a start time.

    We spread the words over the time the line is likely being sung,
    giving longer words more time. If the gap to the next line is long
    (e.g. an instrumental pause), we don't stretch the words across all of it.
    """
    tokens = line.text.split()
    if not tokens:
        line.words = []
        return
    gap = max(line.end - line.start, 0.2)
    weights = [len(t) + 1.5 for t in tokens]
    total = sum(weights)
    # ~0.075 s per weight unit is a comfortable singing pace; never longer than the gap
    sing = min(gap * 0.95, max(0.8, total * 0.075))
    words = []
    t = line.start
    for tok, w in zip(tokens, weights):
        d = sing * w / total
        words.append(Word(tok, t, t + d))
        t += d
    line.words = words
    line.word_timed = False


def build_lyrics(synced_text: str | None, plain_text: str | None = None,
                 instrumental: bool = False, estimate_words: bool = True,
                 source: str = "") -> Lyrics:
    lines = parse_lrc(synced_text) if synced_text else []
    has_words = any(ln.word_timed for ln in lines)
    if lines and estimate_words:
        for ln in lines:
            if not ln.word_timed:
                estimate_word_timing(ln)
    return Lyrics(
        lines=lines,
        synced=bool(lines),
        has_word_timing=has_words,
        instrumental=instrumental,
        plain_text=plain_text or "",
        source=source,
    )


@dataclass
class WordState:
    index: int        # index of the active word, -1 = none started yet
    progress: float   # 0..1 progress through the active word


def word_state(line: Line, t: float) -> WordState:
    """Which word in `line` is active at time t, and how far through it we are."""
    if not line.words:
        return WordState(-1, 0.0)
    starts = [w.start for w in line.words]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        return WordState(-1, 0.0)
    w = line.words[i]
    dur = max(w.end - w.start, 0.01)
    p = (t - w.start) / dur
    if p >= 1.0:
        # we're between words (or past the last one): treat word i as fully sung
        return WordState(i, 1.0)
    return WordState(i, max(0.0, p))
