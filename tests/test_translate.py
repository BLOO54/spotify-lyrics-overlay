"""Tests for translation / romaji. All lyric lines here are invented test text."""

import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import translate as T  # noqa: E402

JA = ["夜空に光る星を見て", "君の名前を呼んでいる", "Oh yeah", "", "明日もきっと会えるから"]
ES = ["no sé por qué te quiero tanto", "la noche es larga y fría", "y tu voz me lleva lejos"]
EN = ["the night is long and cold", "and I don't know why I love you", "so we keep on moving"]


def test_detect_language():
    assert T.detect_language(JA) == "ja"
    assert T.detect_language(["사랑해 오늘 밤에", "너의 목소리"]) == "ko"
    assert T.detect_language(["我爱你在夜里", "星星在天空"]) == "zh"
    assert T.detect_language(["я люблю тебя", "ночь длинная"]) == "ru"
    assert T.detect_language(ES) == "es"
    assert T.detect_language(EN) == "en"
    assert T.detect_language(["la la la"]) is None
    assert T.detect_language([""]) is None


def test_line_needs_translation():
    assert T.line_needs_translation("夜空に光る", "ja")
    assert not T.line_needs_translation("Oh yeah", "ja")      # English line in a J-pop song
    assert not T.line_needs_translation("", "es")
    assert not T.line_needs_translation("...", "es")
    assert T.line_needs_translation("la noche es larga", "es")


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def mymemory_opener(calls, quota_after=None):
    def opener(req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        calls.append(q)
        if quota_after is not None and len(calls) > quota_after:
            body = {"responseData": {"translatedText": "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY"},
                    "responseStatus": 429}
        else:
            body = {"responseData": {"translatedText": "EN:" + q["q"][0] + " &amp; more"}, "responseStatus": 200}
        return FakeResp(json.dumps(body).encode())
    return opener


def test_mymemory_translates_unique_lines_and_caches(tmp_path):
    calls = []
    svc = T.TranslationService(tmp_path, mymemory_opener(calls))
    lines = JA + ["夜空に光る星を見て"]                        # repeated line (chorus)
    ex = svc.build(lines, translate=True, romaji=False, target="en", email="me@example.com")
    assert ex.source_lang == "ja" and ex.label == "JA → EN"
    assert len(calls) == 3                                    # 3 unique Japanese lines only
    assert calls[0]["langpair"] == ["ja|en"] and calls[0]["de"] == ["me@example.com"]
    assert ex.translations[0] == "EN:夜空に光る星を見て & more"   # html entities decoded
    assert 2 not in ex.translations                           # "Oh yeah" left alone
    assert ex.translations[5] == ex.translations[0]
    n = len(calls)
    svc.build(lines, translate=True, romaji=False, target="en")
    assert len(calls) == n                                    # second time: from cache


def test_no_translation_when_same_language(tmp_path):
    calls = []
    svc = T.TranslationService(tmp_path, mymemory_opener(calls))
    ex = svc.build(EN, translate=True, romaji=False, target="en")
    assert ex.translations == {} and calls == [] and ex.label == ""


def test_quota_error(tmp_path):
    svc = T.TranslationService(tmp_path, mymemory_opener([], quota_after=1))
    with pytest.raises(T.TranslationQuotaError):
        svc.build(JA, translate=True, romaji=False, target="en")


def test_network_error(tmp_path):
    def opener(req, timeout=None):
        raise urllib.error.URLError("offline")
    svc = T.TranslationService(tmp_path, opener)
    with pytest.raises(T.TranslationError):
        svc.build(ES, translate=True, romaji=False, target="en")


def test_deepl_batches_and_errors(tmp_path):
    seen = []

    def opener(req, timeout=None):
        seen.append(req)
        form = urllib.parse.parse_qs(req.data.decode())
        body = {"translations": [{"detected_source_language": "ES", "text": "T:" + t} for t in form["text"]]}
        return FakeResp(json.dumps(body).encode())

    svc = T.TranslationService(tmp_path, opener)
    ex = svc.build(ES, translate=True, romaji=False, target="en", provider="deepl", deepl_key="abc:fx")
    assert seen[0].full_url.startswith("https://api-free.deepl.com/")
    assert seen[0].headers["Authorization"] == "DeepL-Auth-Key abc:fx"
    assert ex.translations[1] == "T:la noche es larga y fría"

    def bad_key(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"{}"))
    with pytest.raises(T.TranslationKeyError):
        T.TranslationService(None, bad_key).build(ES, translate=True, romaji=False, target="en",
                                                  provider="deepl", deepl_key="x")
    with pytest.raises(T.TranslationKeyError):
        T.TranslationService(None, bad_key).build(ES, translate=True, romaji=False, target="en",
                                                  provider="deepl", deepl_key="")


def test_romaji_only_without_internet(tmp_path):
    def opener(req, timeout=None):
        raise AssertionError("romaji must not use the network")
    ex = T.TranslationService(tmp_path, opener).build(JA, translate=False, romaji=True, target="en")
    assert ex.translations == {}
    if T.romaji_available():
        assert "yozora" in ex.romaji[0]
        assert 2 not in ex.romaji                 # English line gets no romaji
    else:
        assert ex.romaji == {}                    # library missing -> quietly nothing


@pytest.mark.skipif(not T.romaji_available(), reason="pykakasi not installed here")
def test_romaji_real():
    assert T.to_romaji("日本").lower() in ("nihon", "nippon")     # both readings are correct
    assert T.to_romaji("ありがとう").lower().startswith("arigat")


def test_mymemory_translates_lines_in_parallel_and_keeps_order(tmp_path):
    import threading
    import time as _time
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_opener(req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["q"][0]
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _time.sleep(0.2)
        with lock:
            active[0] -= 1
        return FakeResp(json.dumps({"responseData": {"translatedText": "T:" + q}, "responseStatus": 200}).encode())

    lines = [f"夜空の歌 その{i}" for i in range(8)]          # 8 different (invented) Japanese lines
    t0 = _time.monotonic()
    ex = T.TranslationService(tmp_path, slow_opener).build(lines, translate=True, romaji=False, target="en")
    took = _time.monotonic() - t0
    assert took < 1.0, f"should run in parallel, took {took:.2f}s"   # one-by-one would be 1.6 s
    assert peak[0] > 1
    assert [ex.translations[i] for i in range(8)] == ["T:" + ln for ln in lines]   # order kept
