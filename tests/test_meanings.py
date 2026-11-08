import io
import json
import urllib.error

import pytest

from app.meanings import (AiExplainer, Explanation, GeniusClient, MeaningsError, SongHit,
                          line_explanations)

LINES = ["[Verse 1]", "Hold the line and don't let go", "Every pixel burning bright", "",
         "We're the final boss of the show", "Lights are racing through the night"]


def test_matching_parts_whole_and_multi_line():
    ex = [Explanation("Hold the line", "keep going"),
          Explanation("Every pixel burning bright\nWe're the final boss of the show", "gaming metaphor"),
          Explanation("racing through the night", "driving"),
          Explanation("not in this song at all", "x")]
    got = line_explanations(LINES, ex)
    assert got == [[], [0], [1], [], [1], [2]]


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_genius(routes, seen=None):
    def opener(req, timeout=0):
        url = req.full_url
        if seen is not None:
            seen.append(req)
        for key, payload in routes.items():
            if key in url:
                if isinstance(payload, int):
                    raise urllib.error.HTTPError(url, payload, "err", {}, io.BytesIO(b"{}"))
                return Resp(json.dumps({"meta": {"status": 200}, "response": payload}).encode())
        raise AssertionError(url)
    return opener


SEARCH = {"hits": [
    {"type": "song", "result": {"id": 9, "title": "Other Song", "url": "u9", "primary_artist": {"name": "Someone"}}},
    {"type": "song", "result": {"id": 42, "title": "Neon Horizon", "url": "https://genius.com/x",
                                "song_art_image_thumbnail_url": "t", "primary_artist": {"name": "Demo Band"}}}]}
REFS = {"referents": [
    {"fragment": "Hold the line", "classification": "accepted",
     "annotations": [{"body": {"plain": "Keep going no matter what."}, "votes_total": 12, "verified": True,
                      "state": "accepted", "url": "a1"}]},
    {"fragment": "Every pixel burning bright", "classification": "unreviewed",
     "annotations": [{"body": {"plain": "A gamer's view of the city."}, "votes_total": 2, "state": "pending"}]}]}


def test_genius_find_and_meanings(tmp_path):
    seen = []
    routes = {"/search": SEARCH, "/songs/42": {"song": {"description": {"plain": "A song about games."}}},
              "/referents": REFS}
    g = GeniusClient("tok", tmp_path, opener=fake_genius(routes, seen))
    hit = g.find("Neon Horizon - Remastered 2024", "Demo Band")
    assert hit.genius_id == 42
    assert "q=Neon+Horizon+Demo+Band" in seen[0].full_url
    assert seen[0].get_header("Authorization") == "Bearer tok"
    m = g.meanings(hit)
    assert m.about == "A song about games."
    assert [e.text for e in m.explanations] == ["Keep going no matter what.", "A gamer's view of the city."]
    assert m.explanations[0].verified and not m.explanations[1].accepted
    # second time comes from the cache - no network
    g2 = GeniusClient("tok", tmp_path, opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    assert g2.meanings(hit).about == "A song about games."


def test_genius_wrong_song_not_matched():
    g = GeniusClient("tok", None, opener=fake_genius({"/search": SEARCH}))
    assert g.find("Completely Different", "Nobody") is None


def test_genius_errors_are_friendly():
    with pytest.raises(MeaningsError, match="access token"):
        GeniusClient("", None).search("x")
    with pytest.raises(MeaningsError, match="didn't accept"):
        GeniusClient("bad", None, opener=fake_genius({"/search": 401})).search("x")


def test_ai_backup_asks_once_and_remembers(tmp_path):
    calls = []

    def opener(req, timeout=0):
        calls.append(json.loads(req.data.decode()))
        assert req.get_header("X-api-key") == "sk-test"
        return Resp(json.dumps({"content": [{"type": "text", "text": "It means: don't give up."}]}).encode())
    ai = AiExplainer("sk-test", tmp_path / "ai.json", opener=opener)
    lines = [ln for ln in LINES if ln]
    assert ai.explain("Neon Horizon", "Demo Band", lines, 1) == "It means: don't give up."
    assert "→ Hold the line" in calls[0]["messages"][0]["content"]
    ai2 = AiExplainer("sk-test", tmp_path / "ai.json", opener=opener)
    assert ai2.explain("Neon Horizon", "Demo Band", lines, 1) == "It means: don't give up."
    assert len(calls) == 1


def test_ai_backup_needs_key():
    with pytest.raises(MeaningsError, match="API key"):
        AiExplainer("", None).explain("a", "b", ["x"], 0)


def test_hit_label():
    assert SongHit(1, "T", "A").label == "T – A"


def test_clean_html_keeps_pictures_and_drops_scripts():
    from app.meanings import clean_html, image_urls
    raw = ('<p>Look at <a href="https://genius.com/x" onclick="evil()">this</a>:</p>'
           '<img src="https://images.genius.com/abc.jpg" width="600" height="400" alt="x" onerror="evil()">'
           '<script>alert(1)</script><iframe src="https://youtube.com/embed/1"></iframe>'
           '<blockquote style="color:red">quote</blockquote><img src="javascript:bad">')
    out = clean_html(raw)
    assert out == ('<p>Look at <a href="https://genius.com/x">this</a>:</p>'
                   '<img src="https://images.genius.com/abc.jpg"><blockquote>quote</blockquote>')
    assert image_urls(out) == ["https://images.genius.com/abc.jpg"]


def test_genius_html_bodies(tmp_path):
    refs = {"referents": [{"fragment": "Hold the line", "annotations": [
        {"body": {"plain": "See https://images.genius.com/p.png",
                  "html": '<p>See</p><img src="https://images.genius.com/p.png" width="300">'},
         "state": "accepted"}]}]}
    seen = []
    g = GeniusClient("tok", tmp_path, opener=fake_genius(
        {"/songs/42": {"song": {"description": {"plain": "About", "html": "<p><b>About</b></p>"}}},
         "/referents": refs}, seen))
    m = g.meanings(SongHit(42, "Neon Horizon", "Demo Band"))
    assert m.explanations[0].html == '<p>See</p><img src="https://images.genius.com/p.png">'
    assert m.about_html == "<p><b>About</b></p>"
    assert "text_format=plain%2Chtml" in seen[0].full_url


def test_clean_html_finds_pictures_in_other_forms():
    from app.meanings import clean_html, image_urls
    out = clean_html("<img src='//images.genius.com/a.gif'><img data-src=\"https://x.com/b.png\">"
                     '<img srcset="https://x.com/c.webp 2x">'
                     '<a href="https://media.giphy.com/media/AbC123/giphy.gif">gif</a>'
                     '<a href="https://giphy.com/gifs/chicken-running-XyZ789">giphy page</a>'
                     '<a href="https://imgur.com/q1W2e3R">imgur</a>'
                     '<a href="https://example.com/article">normal link</a>')
    assert image_urls(out) == ["https://images.genius.com/a.gif", "https://x.com/b.png", "https://x.com/c.webp",
                               "https://media.giphy.com/media/AbC123/giphy.gif",
                               "https://media.giphy.com/media/XyZ789/giphy.gif", "https://i.imgur.com/q1W2e3R.jpg"]
    assert '<a href="https://example.com/article">normal link</a>' in out


def test_x_posts_found_and_read():
    from app.meanings import fetch_x_post, x_posts
    html_text = ('<p>See <a href="https://x.com/jamiepaige/status/1234567890123">this</a> and '
                 '<a href="https://twitter.com/jamiepaige/status/1234567890123?s=20">again</a></p>')
    assert x_posts(html_text) == ["https://twitter.com/jamiepaige/status/1234567890123"]
    seen = []

    def opener(req, timeout=0):
        seen.append(req.full_url)
        return Resp(json.dumps({"author_name": "Jamie Paige", "author_url": "https://twitter.com/jamiepaige",
                                "html": '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">the chicken '
                                        'never stops running <a href="https://t.co/abc">pic.twitter.com/abc</a>'
                                        '</p>&mdash; Jamie Paige (@jamiepaige)</blockquote>'}).encode())
    post = fetch_x_post("https://twitter.com/jamiepaige/status/1234567890123", opener)
    assert post == {"author": "Jamie Paige", "handle": "jamiepaige", "text": "the chicken never stops running",
                    "url": "https://x.com/jamiepaige/status/1234567890123", "pictures": [], "picture": True}
    assert seen[0].startswith("https://cdn.syndication.twimg.com/tweet-result?")     # tried the embed data first
    assert seen[1].startswith("https://publish.twitter.com/oembed?")                 # then the official text


def test_x_post_with_pictures():
    from app.meanings import fetch_x_post, x_token

    def opener(req, timeout=0):
        assert "token=" + x_token("440322224407314432") in req.full_url
        return Resp(json.dumps({"text": "If only Bradley's arm was longer. https://t.co/C9JjEHlHqU",
                                "user": {"name": "Ellen DeGeneres", "screen_name": "TheEllenShow"},
                                "mediaDetails": [{"type": "photo",
                                                  "media_url_https": "https://pbs.twimg.com/media/BhxWutnCEAAtEQ6.jpg"}]
                                }).encode())
    post = fetch_x_post("https://x.com/TheEllenShow/status/440322224407314432", opener)
    assert post["pictures"] == ["https://pbs.twimg.com/media/BhxWutnCEAAtEQ6.jpg?name=small"]
    assert post["text"] == "If only Bradley's arm was longer." and post["author"] == "Ellen DeGeneres"
