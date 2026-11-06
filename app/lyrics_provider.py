"""Fetching lyrics from LRCLIB (https://lrclib.net).

Plain English: LRCLIB is a free, open lyrics database. We send it the song
title, artist, album and length, and it sends back time-stamped lyrics.
No account or API key is required. Following LRCLIB's guidance, every
request identifies this app with a User-Agent header. Results are cached on
disk so the same song isn't downloaded twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME, APP_VERSION

log = logging.getLogger(__name__)

BASE_URL = "https://lrclib.net/api"
USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION} (personal desktop lyrics overlay)"
TIMEOUT = 10
NOT_FOUND_TTL = 24 * 3600     # re-check songs without lyrics once a day


class LyricsNetworkError(Exception):
    """Internet problem (offline, timeout, server error). Worth retrying later."""


@dataclass
class LyricsResult:
    synced: str | None
    plain: str | None
    instrumental: bool
    source: str = "LRCLIB"

    @property
    def found(self) -> bool:
        return bool(self.synced or self.plain or self.instrumental)


# --------------------------------------------------------------------------
# Title / artist cleanup. Spotify titles often carry extras like
# "Song - Remastered 2011" or "Song (feat. Someone)" that lyric sites omit.
_TITLE_JUNK = [
    r"\s+-\s+.*\b(remaster(ed)?|live|version|edit|mix|mono|stereo|demo|acoustic|radio|single|bonus|deluxe|instrumental|from|recorded)\b.*$",
    r"\s*[\(\[]\s*(feat\.?|ft\.?|with)\s+[^\)\]]*[\)\]]",
    r"\s*[\(\[][^\)\]]*\b(remaster(ed)?|version|edit|mono|stereo|bonus|deluxe)\b[^\)\]]*[\)\]]",
]


def clean_title(title: str) -> str:
    t = title
    for pat in _TITLE_JUNK:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    return t.strip() or title.strip()


def primary_artist(artist: str) -> str:
    parts = re.split(r",|&|\bfeat\.?\b|\bft\.?\b|\bx\b|;|/", artist, flags=re.IGNORECASE)
    first = parts[0].strip() if parts else artist
    return first or artist.strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
FIT_SLACK = 2.5      # seconds of lyrics allowed after the song "ends" (rounding, fades)


def lyrics_end(synced: str | None) -> float:
    """Time of the last timestamp in LRC text (0 if none)."""
    last = 0.0
    for m in _TS.finditer(synced or ""):
        mm, ss, frac = m.groups()
        t = int(mm) * 60 + int(ss) + (int(frac) / 10 ** len(frac) if frac else 0)
        last = max(last, t)
    return last


_WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


def has_word_timing(synced: str | None) -> bool:
    """True for "enhanced" LRC, which times every word (best for karaoke)."""
    return bool(synced) and len(_WORD_TAG.findall(synced)) >= 3


def fits_song(synced: str | None, duration: float) -> bool:
    """False when the lyrics keep going after the song has ended - a sign they were
    timed against a different version (edit / remaster / upload) of the song."""
    if not synced or not duration:
        return True
    return lyrics_end(synced) <= duration + FIT_SLACK


# --------------------------------------------------------------------------
class LrclibClient:
    def __init__(self, cache_dir: Path | None = None, opener=None):
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        # `opener` lets the tests swap the network for fake responses
        self._open = opener or urllib.request.urlopen

    # ---- HTTP ----------------------------------------------------------
    def _get_json(self, path: str, params: dict):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{BASE_URL}{path}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with self._open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                raise LyricsNetworkError(f"LRCLIB server error {e.code}") from e
            log.warning("LRCLIB HTTP %s for %s", e.code, url)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LyricsNetworkError(str(e)) from e
        except ValueError as e:  # bad JSON
            raise LyricsNetworkError(f"Bad response from LRCLIB: {e}") from e

    # ---- cache ---------------------------------------------------------
    def _cache_file(self, title, artist, duration) -> Path | None:
        if not self.cache_dir:
            return None
        key = f"v2|{_norm(title)}|{_norm(artist)}|{int(round(duration or 0))}"
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _cache_read(self, f: Path | None) -> LyricsResult | None:
        if not f or not f.exists():
            return None
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            r = LyricsResult(d.get("synced"), d.get("plain"), bool(d.get("instrumental")), d.get("source", "LRCLIB"))
            if not r.found and time.time() - d.get("saved", 0) > NOT_FOUND_TTL:
                return None  # stale "not found" -> ask again
            return r
        except Exception:
            return None

    def _cache_write(self, f: Path | None, r: LyricsResult) -> None:
        if not f:
            return
        try:
            f.write_text(json.dumps({"synced": r.synced, "plain": r.plain, "instrumental": r.instrumental,
                                     "source": r.source, "saved": time.time()}), encoding="utf-8")
        except Exception:
            log.exception("Could not write lyrics cache")

    # ---- matching ------------------------------------------------------
    @staticmethod
    def _to_result(item: dict) -> LyricsResult:
        return LyricsResult(item.get("syncedLyrics") or None, item.get("plainLyrics") or None,
                            bool(item.get("instrumental")))

    @staticmethod
    def _rank(items, title: str, artist: str, duration: float):
        """Score every candidate; best first. Returns [(score, item)]."""
        ranked = []
        nt, na = _norm(clean_title(title)), _norm(primary_artist(artist))
        for it in items or []:
            if not isinstance(it, dict):
                continue
            score = 0.0
            if it.get("syncedLyrics"):
                score += 50
                if has_word_timing(it["syncedLyrics"]):
                    score += 20       # word-by-word timing beats line timing
            elif it.get("plainLyrics") or it.get("instrumental"):
                score += 5
            else:
                continue
            it_title = _norm(clean_title(it.get("trackName") or ""))
            it_artist = _norm(it.get("artistName") or "")
            if it_title == nt:
                score += 30
            elif nt and (nt in it_title or it_title in nt):
                score += 10
            if na and na in it_artist:
                score += 25
            d = it.get("duration")
            if duration and isinstance(d, (int, float)):
                diff = abs(d - duration)
                if diff > 15:
                    score -= 60       # very likely a different recording
                else:
                    score -= diff * 2
            if it.get("syncedLyrics") and not fits_song(it["syncedLyrics"], duration):
                score -= 45           # lyrics run past the end of the song: timed for another version
            ranked.append((score, it))
        ranked.sort(key=lambda x: -x[0])
        return ranked

    @classmethod
    def _pick_best(cls, items, title: str, artist: str, duration: float):
        ranked = cls._rank(items, title, artist, duration)
        return ranked[0][1] if ranked and ranked[0][0] > 0 else None

    def _search_all(self, title: str, artist: str) -> list:
        ct, pa = clean_title(title), primary_artist(artist)
        items = list(self._get_json("/search", {"track_name": ct, "artist_name": pa}) or [])
        if not any(isinstance(i, dict) and i.get("syncedLyrics") for i in items):
            items += list(self._get_json("/search", {"q": f"{ct} {pa}"}) or [])
        seen, out = set(), []
        for it in items:
            if isinstance(it, dict) and it.get("id", id(it)) not in seen:
                seen.add(it.get("id", id(it)))
                out.append(it)
        return out

    # ---- public --------------------------------------------------------
    def fetch(self, title: str, artist: str, album: str = "", duration: float = 0.0) -> LyricsResult:
        """Return lyrics for a track. Raises LyricsNetworkError when offline."""
        cache = self._cache_file(title, artist, duration)
        cached = self._cache_read(cache)
        if cached is not None:
            return cached

        result = LyricsResult(None, None, False)
        dur = int(round(duration)) if duration else None

        # 1) exact lookup (fastest and most accurate)
        if dur:
            item = self._get_json("/get", {"track_name": title, "artist_name": artist,
                                           "album_name": album, "duration": dur})
            if item:
                result = self._to_result(item)

        # 2) search if we still lack synced lyrics, or the ones we got were timed for
        #    a different version of the song (they run past its end)
        suspect = bool(result.synced) and not fits_song(result.synced, duration)
        if suspect:
            log.info("Lyrics for %s - %s run past the song's end; looking for a better version", artist, title)
        # Order of preference: word-timed > line-timed > untimed (plain) lyrics.
        want_better = (not result.synced and not result.instrumental) or suspect \
            or (bool(result.synced) and not has_word_timing(result.synced))
        if want_better:
            ranked = [(s, it) for s, it in self._rank(self._search_all(title, artist), title, artist, duration)
                      if s > 0]
            fitting = [it for s, it in ranked if it.get("syncedLyrics") and fits_song(it["syncedLyrics"], duration)]

            def same_length(it):    # only swap to a word-timed version of the *same* recording
                d = it.get("duration")
                return not duration or not isinstance(d, (int, float)) or abs(d - duration) <= 3

            worded = [it for it in fitting if has_word_timing(it["syncedLyrics"]) and same_length(it)]
            if worded:
                result = self._to_result(worded[0])
            elif fitting and (not result.synced or suspect):
                result = self._to_result(fitting[0])
            elif ranked and not result.synced:
                cand = self._to_result(ranked[0][1])
                if cand.synced or not result.found:
                    result = cand

        self._cache_write(cache, result)
        return result

    def alternatives(self, title: str, artist: str, album: str = "", duration: float = 0.0) -> list[LyricsResult]:
        """All the different timed versions LRCLIB has for this song, best match first."""
        items = self._search_all(title, artist)
        if duration:
            got = self._get_json("/get", {"track_name": title, "artist_name": artist,
                                          "album_name": album, "duration": int(round(duration))})
            if isinstance(got, dict):
                items.insert(0, got)
        out, seen = [], set()
        for score, it in self._rank(items, title, artist, duration):
            synced = it.get("syncedLyrics")
            if score > 0 and synced and synced not in seen:
                seen.add(synced)
                out.append(self._to_result(it))
        return out

    def remember_choice(self, title: str, artist: str, duration: float, result: LyricsResult) -> None:
        """Use this version from now on for this song."""
        self._cache_write(self._cache_file(title, artist, duration), result)
