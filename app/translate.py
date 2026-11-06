"""Translation and pronunciation (romaji) for foreign-language lyrics.

Plain English:
* First we guess the song's language from the letters it uses (Japanese kana,
  Korean Hangul, Cyrillic...) or, for languages written in the Latin
  alphabet, from common little words ("el", "la", "und", "le"...).
* If the song isn't already in your language, each unique line is sent to a
  translation service, and the results are saved on your PC so a song is only
  translated once.
    - MyMemory (default): free, no account. ~5,000 characters/day, or ~50,000
      if you enter an email address in Settings.
    - DeepL (optional): better quality, 500,000 characters/month free, but you
      need a free DeepL account and paste its "API key" into Settings.
* Romaji (how Japanese is pronounced, in English letters) is worked out on
  your own PC with the pykakasi library - no internet needed.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME, APP_VERSION

log = logging.getLogger(__name__)

USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION}"
TIMEOUT = 12

# Target languages offered in Settings: code -> (name, MyMemory code, DeepL code)
LANGUAGES = {
    "en": ("English", "en", "EN-US"),
    "es": ("Spanish", "es", "ES"),
    "fr": ("French", "fr", "FR"),
    "de": ("German", "de", "DE"),
    "pt": ("Portuguese", "pt", "PT-BR"),
    "it": ("Italian", "it", "IT"),
    "nl": ("Dutch", "nl", "NL"),
    "pl": ("Polish", "pl", "PL"),
    "tr": ("Turkish", "tr", "TR"),
    "ru": ("Russian", "ru", "RU"),
    "ja": ("Japanese", "ja", "JA"),
    "ko": ("Korean", "ko", "KO"),
    "zh": ("Chinese", "zh-CN", "ZH"),
    "id": ("Indonesian", "id", "ID"),
    "ar": ("Arabic", "ar", "AR"),
}


class TranslationError(Exception):
    """Network problem or bad response."""


class TranslationQuotaError(TranslationError):
    """Daily / monthly free allowance used up."""


class TranslationKeyError(TranslationError):
    """DeepL key missing or wrong."""


# --------------------------------------------------------------------------
# Language detection
_SCRIPTS = [
    ("ja", re.compile(r"[぀-ヿ]")),            # hiragana / katakana
    ("ko", re.compile(r"[가-힯ᄀ-ᇿ]")),
    ("zh", re.compile(r"[一-鿿]")),             # CJK (without kana -> Chinese)
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("he", re.compile(r"[֐-׿]")),
    ("el", re.compile(r"[Ͱ-Ͽ]")),
    ("th", re.compile(r"[฀-๿]")),
    ("hi", re.compile(r"[ऀ-ॿ]")),
]
_NON_LATIN_ANY = re.compile(r"[Ͱ-ϿЀ-ӿ֐-ۿऀ-ॿ฀-๿"
                            r"ᄀ-ᇿ぀-ヿ一-鿿가-힯]")

_STOPWORDS = {
    "en": "the you and i to a me my it is in that of on your all be we for love so don't i'm can",
    "es": "el la que de y en los las un una por con mi tu te me es no lo yo se como pero amor más",
    "fr": "le la les de et un une je tu il est que pas des dans pour mon ma moi toi sur qui ne plus",
    "de": "der die das und ich du nicht ist ein eine mit mich dich mein zu es auf wie sie wir dein",
    "pt": "o a os as de que e um uma não eu você meu minha com pra por se é do da no na",
    "it": "il la di che e un una non io tu mi ti sono per con è lo gli le ma come più",
    "nl": "de het een en ik je niet is van dat zijn met mijn op wat te jij",
    "pl": "i w nie się na że to jest ja ty z mnie co jak ale tak",
    "tr": "ve bir bu ben sen de da ne için çok gibi mi beni seni",
    "id": "dan yang aku kau di ini itu tak tidak ke kita dengan untuk cinta",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOPWORDS.items()}
_WORD = re.compile(r"[a-zà-ÿ'’çğışöüñ]+", re.IGNORECASE)


def detect_language(lines: list[str]) -> str | None:
    """Best guess at the main language of the lyrics (ISO code) or None."""
    text = "\n".join(lines)
    if not text.strip():
        return None
    counts = {code: len(rx.findall(text)) for code, rx in _SCRIPTS}
    letters = sum(1 for ch in text if ch.isalpha())
    if counts["ja"] > 0 and (counts["ja"] + counts["zh"]) >= 0.15 * letters:
        return "ja"
    best = max(counts, key=counts.get)
    if counts[best] >= 0.2 * max(letters, 1) and best != "ja":
        return best
    words = [w.lower().replace("’", "'") for w in _WORD.findall(text)]
    if len(words) < 5:
        return None
    scores = {lang: sum(1 for w in words if w in sw) / len(words) for lang, sw in _STOPSETS.items()}
    lang = max(scores, key=scores.get)
    return lang if scores[lang] >= 0.08 else None


def line_needs_translation(line: str, source_lang: str) -> bool:
    """For non-Latin languages, skip lines that are only English/Latin (common in J-pop)."""
    if not line.strip() or not re.search(r"\w", line):
        return False
    if source_lang in {code for code, _ in _SCRIPTS}:
        return bool(_NON_LATIN_ANY.search(line))
    return True


# --------------------------------------------------------------------------
# Romaji (Japanese pronunciation)
_kks = None
_JP_GAP = re.compile(r"(?<=[\u3040-\u30ff\u4e00-\u9fff])\s+(?=[\u3040-\u30ff\u4e00-\u9fff])")


def romaji_available() -> bool:
    try:
        import pykakasi  # noqa: F401
        return True
    except Exception:
        return False


def to_romaji(line: str) -> str:
    """'夜空に光る' -> 'yozora ni hikaru'. Returns '' if pykakasi isn't available."""
    global _kks
    if not line.strip():
        return ""
    # Spaces between Japanese words hide context from the dictionary
    # (e.g. "君の 名前を" reads worse than "君の名前を"), so join them first.
    joined = _JP_GAP.sub("", line)
    try:
        if _kks is None:
            import pykakasi
            _kks = pykakasi.kakasi()
        parts = [item.get("hepburn", "") for item in _kks.convert(joined)]
        out = " ".join(p.strip() for p in parts if p.strip())
        return re.sub(r"\s+([,.!?、。！？])", r"\1", out)
    except Exception:
        log.exception("romaji conversion failed")
        return ""


# --------------------------------------------------------------------------
# Translators
def _http(req: urllib.request.Request, opener):
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            return resp.status if hasattr(resp, "status") else 200, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise TranslationError(f"Translation service unreachable: {e}") from e


class MyMemoryTranslator:
    name = "MyMemory"
    URL = "https://api.mymemory.translated.net/get"

    def __init__(self, email: str = "", opener=None):
        self.email = email.strip()
        self._open = opener or urllib.request.urlopen

    def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        src = LANGUAGES.get(source, (None, source, None))[1]
        tgt = LANGUAGES[target][1]
        out = []
        for t in texts:
            q = t.encode("utf-8")[:480].decode("utf-8", "ignore")   # API limit: 500 bytes
            params = {"q": q, "langpair": f"{src}|{tgt}"}
            if self.email:
                params["de"] = self.email
            req = urllib.request.Request(self.URL + "?" + urllib.parse.urlencode(params),
                                         headers={"User-Agent": USER_AGENT})
            code, body = _http(req, self._open)
            data = body.get("responseData") or {}
            text = data.get("translatedText") or ""
            status = str(body.get("responseStatus", code))
            if status == "429" or code == 429 or body.get("quotaFinished") or "MYMEMORY WARNING" in text.upper():
                raise TranslationQuotaError("Free MyMemory translations used up for today")
            if status != "200" or not text:
                raise TranslationError(f"MyMemory error {status}: {body.get('responseDetails', '')}")
            out.append(html.unescape(text))
        return out


class DeepLTranslator:
    name = "DeepL"

    def __init__(self, key: str, opener=None):
        self.key = key.strip()
        self._open = opener or urllib.request.urlopen
        host = "api-free.deepl.com" if self.key.endswith(":fx") else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"

    def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        if not self.key:
            raise TranslationKeyError("No DeepL key entered in Settings")
        out: list[str] = []
        for i in range(0, len(texts), 50):            # up to 50 lines per request
            chunk = texts[i:i + 50]
            form = [("target_lang", LANGUAGES[target][2])] + [("text", t) for t in chunk]
            req = urllib.request.Request(self.url, data=urllib.parse.urlencode(form).encode(),
                                         headers={"Authorization": f"DeepL-Auth-Key {self.key}",
                                                  "User-Agent": USER_AGENT,
                                                  "Content-Type": "application/x-www-form-urlencoded"})
            code, body = _http(req, self._open)
            if code in (401, 403):
                raise TranslationKeyError("DeepL rejected the key - check it in Settings")
            if code == 456:
                raise TranslationQuotaError("DeepL free monthly allowance used up")
            if code != 200:
                raise TranslationError(f"DeepL error {code}")
            out += [tr.get("text", "") for tr in body.get("translations", [])]
        if len(out) != len(texts):
            raise TranslationError("DeepL returned the wrong number of lines")
        return out


# --------------------------------------------------------------------------
@dataclass
class Extras:
    """Extra rows shown under each lyric line."""
    source_lang: str | None = None
    target_lang: str = ""
    translations: dict[int, str] = field(default_factory=dict)   # line index -> text
    romaji: dict[int, str] = field(default_factory=dict)
    provider: str = ""
    note: str = ""          # e.g. "translation limit reached"

    @property
    def label(self) -> str:
        if self.translations and self.source_lang:
            return f"{self.source_lang.upper()} → {self.target_lang.upper()}"
        return ""


class TranslationService:
    def __init__(self, cache_dir: Path | None, opener=None):
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._open = opener

    def _translator(self, provider: str, email: str, deepl_key: str):
        if provider == "deepl":
            return DeepLTranslator(deepl_key, self._open)
        return MyMemoryTranslator(email, self._open)

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")

    def build(self, lines: list[str], *, translate: bool, romaji: bool, target: str,
              provider: str = "mymemory", email: str = "", deepl_key: str = "") -> Extras:
        """Work out translations / romaji for a song. Raises TranslationError types."""
        src = detect_language(lines)
        ex = Extras(source_lang=src, target_lang=target)

        if romaji and (src == "ja" or any(re.search(r"[぀-ヿ]", ln) for ln in lines)):
            for i, ln in enumerate(lines):
                if re.search(r"[぀-ヿ一-鿿]", ln):
                    r = to_romaji(ln)
                    if r:
                        ex.romaji[i] = r

        if not translate or src is None or src == target:
            return ex
        todo = sorted({ln.strip() for ln in lines if line_needs_translation(ln, src)})
        if not todo:
            return ex

        key = json.dumps([provider, src, target, todo], ensure_ascii=False)
        cache = self._cache_path(key)
        mapping: dict[str, str] | None = None
        if cache and cache.exists():
            try:
                mapping = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                mapping = None
        if mapping is None:
            tr = self._translator(provider, email, deepl_key)
            results = tr.translate(todo, src, target)
            mapping = dict(zip(todo, results))
            if cache:
                try:
                    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    log.exception("could not cache translation")
        ex.provider = provider
        for i, ln in enumerate(lines):
            t = mapping.get(ln.strip())
            if t and t.strip().lower() != ln.strip().lower():
                ex.translations[i] = t
        return ex


def mymemory_probe(opener=None) -> bool:
    """Used by --self-test: can we reach MyMemory?"""
    try:
        MyMemoryTranslator("", opener).translate(["hello"], "en", "es")
        return True
    except TranslationQuotaError:
        return True     # reachable, just out of allowance
    except Exception:
        return False

