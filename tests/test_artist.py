import io
import json

from app import artist as A


class FakeHttp(A._Http):
    def __init__(self, answers):
        self.answers = answers
        self.urls = []

    def json(self, url, headers=None):
        self.urls.append(url)
        for key, val in self.answers.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        return {}


GENIUS = {
    "api.genius.com/search": {"response": {"hits": [
        {"result": {"primary_artist": {"id": 77, "name": "Jamie Paige"}}}]}},
    "api.genius.com/artists/77/songs": {"response": {"songs": [
        {"title": "BIRDBRAIN", "url": "https://genius.com/b", "stats": {"pageviews": 52000},
         "primary_artist": {"id": 77}},
        {"title": "Someone else's", "url": "x", "stats": {}, "primary_artist": {"id": 5}}]}},
    "api.genius.com/artists/77": {"response": {"artist": {
        "name": "Jamie Paige", "url": "https://genius.com/artists/Jamie-paige",
        "image_url": "https://images.genius.com/jp.jpg", "followers_count": 321,
        "instagram_name": "jamiepaige", "twitter_name": None,
        "description": {"plain": "Jamie Paige is a producer.", "html": "<p>Jamie Paige is a producer.</p>"}}}},
}
WIKI_NO = {"wikipedia.org/w/api.php": {"query": {"search": [{"title": "Jamie Paige (footballer)"}]}},
           "rest_v1/page/summary": {"type": "standard", "description": "English footballer",
                                     "extract": "Jamie Paige is a football player."}}
MB = {"ws/2/artist/?": {"artists": [{"id": "abc", "name": "Jamie Paige", "score": 100}]},
      "ws/2/artist/abc": {"type": "Person", "area": {"name": "United States"},
                          "life-span": {"begin": "2001-03-04"},
                          "genres": [{"name": "electropop", "count": 3}, {"name": "vocaloid", "count": 5}],
                          "relations": [{"type": "youtube", "url": {"resource": "https://youtube.com/@jp"}},
                                        {"type": "official homepage", "url": {"resource": "https://jp.example"}}]}}


def test_about_artist_combines_sources(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    http = FakeHttp({**GENIUS, **WIKI_NO, **MB})
    info = A.about_artist("Jamie Paige", "BIRDBRAIN", "tok", None, http)
    assert info.name == "Jamie Paige" and info.photo_url.endswith("jp.jpg")
    assert info.bio_source == "Genius" and "producer" in info.bio_html
    assert info.popular == [("BIRDBRAIN", "https://genius.com/b", 52000)]        # only their own songs
    assert info.followers == 321
    assert ("From", "United States") in info.facts and ("Born", "2001-03-04") in info.facts
    assert info.genres[:2] == ["vocaloid", "electropop"]
    labels = [l for l, _ in info.links]
    assert labels[:2] == ["YouTube", "Website"] and "Instagram" in labels and "Genius" in labels
    assert "Wikipedia" not in labels and info.tagline == ""           # the footballer was not used


def test_wikipedia_bio_when_no_genius_token(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    wiki = {"wikipedia.org/w/api.php": {"query": {"search": [{"title": "Kasane Teto"}]}},
            "rest_v1/page/summary": {"type": "standard", "title": "Kasane Teto",
                                     "description": "UTAU and Synthesizer V voicebank",
                                     "extract": "Kasane Teto is a voice...", "extract_html": "<p>Kasane Teto is a voice…</p>",
                                     "thumbnail": {"source": "https://upload.wikimedia.org/t.png"},
                                     "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Kasane_Teto"}}}}
    http = FakeHttp({**wiki, "musicbrainz.org": {}})
    info = A.about_artist("Kasane Teto", "", "", None, http)
    assert info.bio_source == "Wikipedia" and info.photo_url.endswith("t.png")
    assert info.tagline.startswith("UTAU") and ("Wikipedia", "https://en.wikipedia.org/wiki/Kasane_Teto") in info.links
    assert not any("genius" in u for u in http.urls)                  # no token: Genius not asked


def test_one_service_down_still_shows_the_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    http = FakeHttp({**GENIUS, "wikipedia.org": OSError("offline"), **MB})
    info = A.about_artist("Jamie Paige", "", "tok", tmp_path, http)
    assert info.found and info.problems == ["Wikipedia"] and info.bio_source == "Genius"
    assert not list(tmp_path.iterdir())                               # not cached while a service was down
    http2 = FakeHttp({**GENIUS, **WIKI_NO, **MB})
    A.about_artist("Jamie Paige", "", "tok", tmp_path, http2)
    again = A.about_artist("Jamie Paige", "", "tok", tmp_path, FakeHttp({}))   # from the cache
    assert again.popular and again.facts


def test_long_genius_bio_is_shortened():
    long = "".join(f"<p>{'word ' * 60}{i}</p>" for i in range(10))
    short = A._shorten_html(long, 700)
    assert short.count("<p>") == 2
