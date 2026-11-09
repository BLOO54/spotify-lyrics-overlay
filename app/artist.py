"""About the artist: a photo, a short bio, facts, popular songs and links - like Spotify's "About".

Where it comes from (all official, free APIs):
  * Genius       - photo, bio, popular songs, followers, social links (uses your Genius token from
                   Settings → Song meanings; skipped without one)
  * Wikipedia    - a short bio + photo (the official REST API; text is CC BY-SA, so it's credited)
  * MusicBrainz  - where they're from, when they started, genres, official links

Results are cached for a week so opening the same artist again is instant.
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import APP_NAME, APP_VERSION

log = logging.getLogger(__name__)

USER_AGENT = (f"{APP_NAME.replace(' ', '')}/{APP_VERSION} "
              "( https://github.com/BLOO54/spotify-lyrics-overlay ; personal desktop lyrics overlay)")
TIMEOUT = 12
CACHE_DAYS = 7
MUSIC_WORDS = ("singer", "musician", "rapper", "band", "group", "duo", "producer", "songwriter", "composer",
               "vocalist", "dj", "record", "music", "artist", "guitarist", "drummer", "pianist", "vocaloid",
               "utau", "idol", "orchestra", "ensemble")


@dataclass
class ArtistInfo:
    name: str
    photo_url: str = ""
    bio_html: str = ""                 # short, already cleaned
    bio_source: str = ""               # "Genius" / "Wikipedia"
    bio_url: str = ""                  # "read more" link
    tagline: str = ""                  # e.g. "American singer-songwriter" (Wikipedia's one-line description)
    facts: list = field(default_factory=list)      # [(label, value)]
    genres: list = field(default_factory=list)
    popular: list = field(default_factory=list)    # [(title, url, views)]
    links: list = field(default_factory=list)      # [(label, url)]
    followers: int = 0                 # Genius followers
    sources: list = field(default_factory=list)    # which services answered
    problems: list = field(default_factory=list)   # services that couldn't be reached (shown small)

    @property
    def found(self) -> bool:
        return bool(self.bio_html or self.facts or self.popular or self.photo_url)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9À-￿]+", "", (s or "").lower())


def _same_name(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na) and (na == nb or (len(na) > 3 and (nb.startswith(na) or na.startswith(nb))))


class _Http:
    def __init__(self, opener=None):
        self._open = opener or urllib.request.urlopen

    def json(self, url: str, headers: dict | None = None):
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        h.update(headers or {})
        for attempt in range(3):
            try:
                with self._open(urllib.request.Request(url, headers=h), timeout=TIMEOUT) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:      # "too many requests": wait a moment, ask again
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise


# --------------------------------------------------------------------------- Genius
def genius_artist(token: str, artist: str, song_title: str = "", http: _Http | None = None) -> dict:
    """{name, photo, bio_html, url, followers, popular, links} from Genius, or {} if not found."""
    from .meanings import clean_html
    if not (token or "").strip():
        return {}
    http = http or _Http()
    auth = {"Authorization": f"Bearer {token.strip()}"}

    def get(path, **params):
        url = f"https://api.genius.com{path}?{urllib.parse.urlencode(params)}"
        return (http.json(url, auth) or {}).get("response") or {}

    artist_id = None
    for q in ([f"{song_title} {artist}"] if song_title else []) + [artist]:
        for h in get("/search", q=q).get("hits") or []:
            pa = (h.get("result") or {}).get("primary_artist") or {}
            if _same_name(pa.get("name", ""), artist):
                artist_id = pa.get("id")
                break
        if artist_id:
            break
    if not artist_id:
        return {}
    a = get(f"/artists/{artist_id}", text_format="html,plain").get("artist") or {}
    desc_html = (a.get("description") or {}).get("html") or ""
    desc_plain = ((a.get("description") or {}).get("plain") or "").strip()
    out = {"name": a.get("name") or artist, "url": a.get("url") or "",
           "photo": a.get("image_url") if "default_avatar" not in (a.get("image_url") or "") else "",
           "bio_html": clean_html(desc_html) if desc_plain not in ("", "?") else "",
           "followers": int(a.get("followers_count") or 0), "links": [], "popular": [],
           "alt_names": a.get("alternate_names") or []}
    for key, label, base in (("instagram_name", "Instagram", "https://www.instagram.com/"),
                             ("twitter_name", "X / Twitter", "https://x.com/"),
                             ("facebook_name", "Facebook", "https://www.facebook.com/")):
        if a.get(key):
            out["links"].append((label, base + a[key]))
    songs = get(f"/artists/{artist_id}/songs", sort="popularity", per_page=8).get("songs") or []
    for s in songs:
        if (s.get("primary_artist") or {}).get("id") == artist_id or not s.get("primary_artist"):
            views = ((s.get("stats") or {}).get("pageviews")) or 0
            out["popular"].append((s.get("title") or "", s.get("url") or "", int(views)))
    return out


# --------------------------------------------------------------------------- Wikipedia
def _wiki_summary(http: _Http, title: str, lang: str) -> dict:
    try:
        return http.json(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                         f"{urllib.parse.quote(title.replace(' ', '_'), safe='')}") or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def _is_musician(summ: dict) -> bool:
    desc = (summ.get("description") or "").lower()
    extract = (summ.get("extract") or "")[:400].lower()
    return summ.get("type") == "standard" and any(w in desc or w in extract for w in MUSIC_WORDS)


def wikipedia_artist(artist: str, http: _Http | None = None, lang: str = "en") -> dict:
    """{title, tagline, extract_html, photo, url} for the artist's Wikipedia page, or {} if unsure.
    Tries the exact page first ("Daft Punk"), then "Name (band)" etc., then a search."""
    http = http or _Http()
    tried = set()
    candidates = [artist] + [f"{artist} ({k})" for k in ("band", "musician", "singer", "rapper", "group",
                                                         "producer", "DJ")]
    for title in candidates:
        tried.add(title.lower())
        summ = _wiki_summary(http, title, lang)
        if summ and _is_musician(summ):
            return _wiki_result(summ, title)
    q = urllib.parse.urlencode({"action": "query", "list": "search", "format": "json", "srlimit": 6,
                                "srsearch": artist})
    hits = (http.json(f"https://{lang}.wikipedia.org/w/api.php?{q}").get("query") or {}).get("search") or []
    for h in hits:
        title = h.get("title") or ""
        base = re.sub(r"\s*\([^)]*\)$", "", title)                  # "Name (singer)" -> "Name"
        if not _same_name(base, artist) or title.lower() in tried:
            continue
        summ = _wiki_summary(http, title, lang)
        if _is_musician(summ):                                      # same name, but must be a musician
            return _wiki_result(summ, title)
    return {}


def _wiki_result(summ: dict, title: str) -> dict:
    return {"title": summ.get("title") or title, "tagline": summ.get("description") or "",
            "extract_html": summ.get("extract_html") or "",
            "photo": ((summ.get("originalimage") or summ.get("thumbnail") or {}).get("source")) or "",
            "url": ((summ.get("content_urls") or {}).get("desktop") or {}).get("page") or ""}


# --------------------------------------------------------------------------- MusicBrainz
_MB_LINKS = {"official homepage": "Website", "youtube": "YouTube", "bandcamp": "Bandcamp",
             "soundcloud": "SoundCloud", "patreon": "Patreon", "streaming": "Streaming"}


def _service_name(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    for key, name in (("spotify", "Spotify"), ("music.apple", "Apple Music"), ("deezer", "Deezer"),
                      ("tidal", "Tidal"), ("music.youtube", "YouTube Music"), ("music.amazon", "Amazon Music"),
                      ("soundcloud", "SoundCloud"), ("bandcamp", "Bandcamp")):
        if key in host:
            return name
    return ""


def musicbrainz_artist(artist: str, http: _Http | None = None) -> dict:
    """{facts, genres, links, url} from MusicBrainz, or {} if not found."""
    http = http or _Http()
    q = urllib.parse.urlencode({"query": f'artist:"{artist}"', "fmt": "json", "limit": 5})
    found = http.json(f"https://musicbrainz.org/ws/2/artist/?{q}").get("artists") or []
    same = [a for a in found if int(a.get("score") or 0) >= 90 and _same_name(a.get("name", ""), artist)
            and not (a.get("disambiguation") or "").strip().lower().startswith("not ")]
    if not same:
        return {}
    if len(same) > 1 and int(same[0].get("score") or 0) < int(same[1].get("score") or 0) + 10:
        return {}                      # several artists with this name and no clear winner: don't guess
    pick = same[0]
    time.sleep(1.0)                    # MusicBrainz asks for at most one request a second
    a = http.json(f"https://musicbrainz.org/ws/2/artist/{pick['id']}?inc=genres+url-rels+tags&fmt=json")
    facts = []
    kind = a.get("type") or ""
    if kind:
        facts.append(("Type", {"Person": "Solo artist"}.get(kind, kind)))
    where = (a.get("begin-area") or {}).get("name") or (a.get("area") or {}).get("name")
    if where:
        facts.append(("From", where))
    life = a.get("life-span") or {}
    if life.get("begin"):
        label = "Born" if kind == "Person" else "Started"
        facts.append((label, life["begin"][:4] if kind != "Person" else life["begin"]))
    if life.get("ended") and life.get("end"):
        facts.append(("Ended", life["end"][:4]))
    if a.get("disambiguation"):
        facts.append(("Known as", a["disambiguation"]))
    genres = [g["name"] for g in sorted(a.get("genres") or [], key=lambda g: -int(g.get("count") or 0))][:6]
    if not genres:
        genres = [t["name"] for t in sorted(a.get("tags") or [], key=lambda t: -int(t.get("count") or 0))
                  if int(t.get("count") or 0) > 0][:5]
    links, seen = [], set()
    for rel in a.get("relations") or []:
        label = _MB_LINKS.get(rel.get("type") or "")
        url = ((rel.get("url") or {}).get("resource")) or ""
        if label == "Streaming":
            label = _service_name(url)
            if not label:
                continue                 # only the well-known streaming services
        if label and url and label not in seen:
            seen.add(label)
            links.append((label, url))
    return {"facts": facts, "genres": genres, "links": links,
            "url": f"https://musicbrainz.org/artist/{pick['id']}"}


# --------------------------------------------------------------------------- all together
def _shorten_html(html_text: str, max_chars: int = 1400) -> str:
    """Keep the first few paragraphs of a long bio (the window offers 'read more')."""
    paras = re.findall(r"<p\b.*?</p>", html_text, flags=re.S | re.I) or [html_text]
    out, n = [], 0
    for p in paras:
        text_len = len(re.sub(r"<[^>]+>", "", p))
        if out and n + text_len > max_chars:
            break
        out.append(p)
        n += text_len
    return "".join(out)


def about_artist(artist: str, song_title: str = "", genius_token: str = "", cache_dir: Path | None = None,
                 http: _Http | None = None) -> ArtistInfo:
    """Everything we can find about an artist. Never raises: missing services are noted in .problems."""
    key = hashlib.sha1(f"artist2|{_norm(artist)}|{bool(genius_token)}".encode()).hexdigest()
    path = (cache_dir / f"{key}.json") if cache_dir else None
    if path and path.exists() and time.time() - path.stat().st_mtime < CACHE_DAYS * 86400:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return ArtistInfo(**{**d, "facts": [tuple(x) for x in d.get("facts", [])],
                                 "popular": [tuple(x) for x in d.get("popular", [])],
                                 "links": [tuple(x) for x in d.get("links", [])]})
        except Exception:
            pass
    http = http or _Http()
    info = ArtistInfo(name=artist)
    results = {}
    for name, fn in (("Genius", lambda: genius_artist(genius_token, artist, song_title, http)),
                     ("Wikipedia", lambda: wikipedia_artist(artist, http)),
                     ("MusicBrainz", lambda: musicbrainz_artist(artist, http))):
        try:
            results[name] = fn() or {}
            if results[name]:
                info.sources.append(name)
        except Exception as e:                     # one service down shouldn't hide the others
            log.warning("about the artist: %s failed: %s", name, e)
            info.problems.append(name)
            results[name] = {}
    g, w, m = results["Genius"], results["Wikipedia"], results["MusicBrainz"]
    info.name = g.get("name") or w.get("title") or artist
    info.photo_url = g.get("photo") or w.get("photo") or ""
    info.tagline = w.get("tagline", "")
    if g.get("bio_html"):
        info.bio_html, info.bio_source, info.bio_url = _shorten_html(g["bio_html"]), "Genius", g.get("url", "")
    elif w.get("extract_html"):
        info.bio_html, info.bio_source, info.bio_url = w["extract_html"], "Wikipedia", w.get("url", "")
    info.facts = list(m.get("facts") or [])
    info.genres = list(m.get("genres") or [])
    info.popular = list(g.get("popular") or [])
    info.followers = int(g.get("followers") or 0)
    links = list(m.get("links") or []) + list(g.get("links") or [])
    for label, url in (("Genius", g.get("url")), ("Wikipedia", w.get("url")), ("MusicBrainz", m.get("url"))):
        if url:
            links.append((label, url))
    seen = set()
    info.links = [(lbl, u) for lbl, u in links if not (u in seen or lbl in seen or seen.add(u) or seen.add(lbl))]
    info.links = info.links[:10]
    if path and info.found and not info.problems:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(info), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return info
