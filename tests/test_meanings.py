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
