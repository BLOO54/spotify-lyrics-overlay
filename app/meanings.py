"""Song meanings: Genius explanations for lyric lines, with an optional AI backup.

Plain English:
* Genius (genius.com) lets apps read its explanations ("annotations") through its
  official API. You make a free Genius account, create an "API client" and copy its
  access token into Settings → Song meanings. The token isn't a password - it only
  lets the app read public Genius data.
* Each explanation is attached to a piece of the lyrics. We match those pieces to the
  lyric lines (from LRCLIB, as in the overlay) so you can click a line to read it.
* Lines Genius hasn't explained can be explained by an AI (Claude) if you turn on the
  AI backup and add your own Anthropic API key. That costs a tiny amount per line on
  your Anthropic account, so it only runs when you click such a line, and every answer
  is remembered so the same line is never paid for twice.

Nothing here needs Qt, so it's unit-tested anywhere.
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
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME, APP_VERSION

log = logging.getLogger(__name__)

GENIUS_API = "https://api.genius.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
AI_MODEL = "claude-haiku-5-5"
USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION} (personal desktop lyrics overlay)"
TIMEOUT = 15
CACHE_DAYS = 7


class MeaningsError(Exception):
    """Something went wrong; the message is written for the user."""


# --------------------------------------------------------------------------- data

@dataclass
class SongHit:
    genius_id: int | None
    title: str
    artist: str
    url: str = ""
    thumb: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} – {self.artist}" if self.artist else self.title


@dataclass
class Explanation:
    fragment: str            # the piece of lyrics it explains
    text: str                # the explanation
    votes: int = 0
    verified: bool = False   # written / approved by the artist
    accepted: bool = True    # reviewed by Genius editors (False = "unreviewed")
    url: str = ""


@dataclass
class SongMeanings:
    hit: SongHit
    about: str = ""
    explanations: list[Explanation] = field(default_factory=list)


# --------------------------------------------------------------------------- matching

def _norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'")
    s = re.sub(r"\[[^\]]*\]", " ", s)                    # [Chorus], [Verse 1: ...]
    return re.sub(r"[^\w]+", "", s)


def line_explanations(lines: list[str], explanations: list[Explanation]) -> list[list[int]]:
    """For every lyric line, the explanations (indexes) that cover it.

    A Genius fragment can be part of a line, exactly a line, or several lines in a row.
    """
    out: list[list[int]] = [[] for _ in lines]
    normed = [_norm(ln) for ln in lines]
    for k, ex in enumerate(explanations):
        frag_lines = [_norm(x) for x in ex.fragment.splitlines() if _norm(x)]
        frag = "".join(frag_lines)
        if not frag:
            continue
        hit_any = False
        # several lines in a row
        if len(frag_lines) > 1:
            for i in range(len(lines)):
                j, ok = i, True
                for fl in frag_lines:
                    while j < len(lines) and not normed[j]:
                        j += 1
                    if j >= len(lines) or not (fl == normed[j] or (len(fl) >= 6 and (fl in normed[j] or normed[j] in fl))):
                        ok = False
                        break
                    j += 1
                if ok:
                    for m in range(i, j):
                        if normed[m] and k not in out[m]:
                            out[m].append(k)
                    hit_any = True
        if hit_any:
            continue
        for i, n in enumerate(normed):
            if not n:
                continue
            if frag == n or (len(frag) >= 6 and frag in n) or (len(n) >= 6 and n in frag):
                out[i].append(k)
    return out


def html_to_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# --------------------------------------------------------------------------- Genius

class GeniusClient:
    def __init__(self, token: str, cache_dir: Path | None = None, opener=None):
        self.token = (token or "").strip()
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._open = opener or urllib.request.urlopen

    def _get(self, path: str, params: dict) -> dict:
        if not self.token:
            raise MeaningsError("Add your Genius access token in Settings → Song meanings first.")
        url = f"{GENIUS_API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}",
                                                   "User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with self._open(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise MeaningsError("Genius didn't accept your access token - check it in Settings → "
                                    "Song meanings.") from e
            if e.code == 429:
                raise MeaningsError("Genius is busy (too many requests) - try again in a minute.") from e
            if e.code == 404:
                return {}
            raise MeaningsError(f"Genius error {e.code} - try again later.") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise MeaningsError("Can't reach Genius right now (internet?).") from e
        return data.get("response") or {}

    def search(self, query: str) -> list[SongHit]:
        resp = self._get("/search", {"q": query})
        hits = []
        for h in resp.get("hits") or []:
            if h.get("type") != "song":
                continue
            r = h.get("result") or {}
            hits.append(SongHit(r.get("id"), r.get("title") or "", (r.get("primary_artist") or {}).get("name") or "",
                                r.get("url") or "", r.get("song_art_image_thumbnail_url") or ""))
        return hits

    def find(self, title: str, artist: str) -> SongHit | None:
        """The Genius page for a song, matched on title and artist."""
        clean = re.sub(r"\s+-\s+.*$", "", title)                          # "Song - Remastered 2011"
        clean = re.sub(r"\s*[(\[][^)\]]*\b(feat|ft|with|remaster\w*|version|edit|mix|live)\b[^)\]]*[)\]]", "",
                       clean, flags=re.I).strip() or title
        hits = self.search(f"{clean} {artist}")
        t, a = _norm(clean), _norm(artist)
        best, best_score = None, 0
        for h in hits:
            ht, ha = _norm(h.title), _norm(h.artist)
            score = (3 if ht == t else 2 if (t and (t in ht or ht in t)) else 0) \
                + (2 if (a and (a in ha or ha in a)) else 0)
            if score > best_score:
                best, best_score = h, score
        return best if best_score >= 3 else None

    def meanings(self, hit: SongHit) -> SongMeanings:
        cache = self._cache(f"song|{hit.genius_id}")
        got = self._read(cache)
        if got is not None:
            return got
        out = SongMeanings(hit)
        song = self._get(f"/songs/{hit.genius_id}", {"text_format": "plain"}).get("song") or {}
        desc = (song.get("description") or {}).get("plain") or ""
        out.about = "" if desc.strip() in ("?", "") else desc.strip()
        for page in range(1, 11):
            refs = self._get("/referents", {"song_id": hit.genius_id, "text_format": "plain",
                                            "per_page": 50, "page": page}).get("referents") or []
            for ref in refs:
                frag = ref.get("fragment") or ((ref.get("range") or {}).get("content")) or ""
                for an in ref.get("annotations") or []:
                    body = (an.get("body") or {}).get("plain") or html_to_text((an.get("body") or {}).get("html", ""))
                    if not body.strip() or an.get("state") == "rejected":
                        continue
                    out.explanations.append(Explanation(
                        frag, body.strip(), int(an.get("votes_total") or 0), bool(an.get("verified")),
                        (ref.get("classification") or "accepted") != "unreviewed", an.get("url") or ""))
            if len(refs) < 50:
                break
        self._write(cache, out)
        return out

    # cache (a week), so reopening a song is instant and doesn't hit Genius again
    def _cache(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _read(self, path: Path | None) -> SongMeanings | None:
        if not path or not path.exists() or time.time() - path.stat().st_mtime > CACHE_DAYS * 86400:
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return SongMeanings(SongHit(**d["hit"]), d.get("about", ""),
                                [Explanation(**e) for e in d.get("explanations", [])])
        except Exception:
            return None

    def _write(self, path: Path | None, m: SongMeanings) -> None:
        if not path:
            return
        try:
            path.write_text(json.dumps({"hit": vars(m.hit), "about": m.about,
                                        "explanations": [vars(e) for e in m.explanations]},
                                       ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------- AI backup

class AiExplainer:
    """Explains a lyric line with Claude (your own Anthropic API key). Answers are remembered."""

    def __init__(self, key: str, cache_file: Path | None = None, opener=None, model: str = AI_MODEL):
        self.key = (key or "").strip()
        self.model = model
        self.cache_file = cache_file
        self._open = opener or urllib.request.urlopen
        self._cache: dict[str, str] = {}
        if cache_file and cache_file.exists():
            try:
                self._cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    @staticmethod
    def _key(title, artist, line, language) -> str:
        return hashlib.sha1(f"{_norm(title)}|{_norm(artist)}|{_norm(line)}|{language}".encode()).hexdigest()

    def cached(self, title, artist, line, language="English") -> str | None:
        return self._cache.get(self._key(title, artist, line, language))

    def explain(self, title: str, artist: str, lines: list[str], index: int, language: str = "English") -> str:
        line = lines[index]
        got = self.cached(title, artist, line, language)
        if got:
            return got
        if not self.key:
            raise MeaningsError("Add your Anthropic API key in Settings → Song meanings to use the AI backup.")
        lo, hi = max(0, index - 6), min(len(lines), index + 7)
        context = "\n".join(("→ " if i == index else "  ") + lines[i] for i in range(lo, hi) if lines[i].strip())
        prompt = (f"Song: \"{title}\" by {artist}.\n\nNearby lyrics (the arrow marks the line to explain):\n"
                  f"{context}\n\nIn {language}, explain in 2-4 short sentences what the marked line means in "
                  "this song: the feeling, any slang, wordplay or references. If the line isn't in "
                  f"{language}, first give a short translation. If you aren't sure what the artist meant, say "
                  "it's an interpretation. Don't repeat the lyrics back at length.")
        body = json.dumps({"model": self.model, "max_tokens": 350,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        req = urllib.request.Request(ANTHROPIC_API, data=body, method="POST", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json",
            "User-Agent": USER_AGENT})
        try:
            with self._open(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise MeaningsError("Anthropic didn't accept your API key - check it in Settings → "
                                    "Song meanings.") from e
            if e.code == 429:
                raise MeaningsError("The AI is busy (or your Anthropic account hit a limit) - try again soon.") from e
            if e.code == 400:
                try:
                    msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
                except Exception:
                    msg = ""
                if "credit" in msg.lower() or "billing" in msg.lower():
                    raise MeaningsError("Your Anthropic account has no credit left.") from e
                raise MeaningsError(f"The AI couldn't answer ({msg or 'bad request'}).") from e
            raise MeaningsError(f"AI error {e.code} - try again later.") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise MeaningsError("Can't reach the AI right now (internet?).") from e
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            raise MeaningsError("The AI didn't give an answer - try again.")
        self._cache[self._key(title, artist, line, language)] = text
        if self.cache_file:
            try:
                self.cache_file.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return text
