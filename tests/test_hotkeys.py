import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import hotkeys as H  # noqa: E402


def test_parse_valid():
    assert H.parse("Ctrl+Alt+L") == (H.MOD_CONTROL | H.MOD_ALT, ord("L"))
    assert H.parse("ctrl + shift + f8") == (H.MOD_CONTROL | H.MOD_SHIFT, 0x77)
    assert H.parse("F9") == (0, 0x78)                       # function keys may stand alone
    assert H.parse("Num5") == (0, 0x65)
    assert H.parse("Ctrl+Shift+Right") == (H.MOD_CONTROL | H.MOD_SHIFT, 0x27)
    assert H.parse("Win+]") == (H.MOD_WIN, 0xDD)


def test_parse_invalid():
    for bad in ("", "   ", "L", "Ctrl", "Ctrl+Shift", "Ctrl+A+B", "Ctrl+Banana", "Ctrl++"):
        assert H.parse(bad) is None, bad
    assert not H.is_valid("Q")                               # plain letter would block typing


def test_normalise():
    assert H.normalise("shift+ctrl+f8") == "Ctrl+Shift+F8"
    assert H.normalise("ALT+ctrl+left") == "Ctrl+Alt+Left"
    assert H.normalise("nonsense") == ""


def test_defaults_are_valid():
    for field, label, default in H.ACTIONS:
        assert H.is_valid(default), field


def test_from_qt():
    assert H.from_qt(0x4C, True, False, True, False) == "Ctrl+Shift+L"
    assert H.from_qt(0x01000037, False, False, False, False) == "F8"
    assert H.from_qt(0x01000014, True, False, True, False) == "Ctrl+Shift+Right"
    assert H.from_qt(0x21, True, False, True, False) == "Ctrl+Shift+1"     # Shift+1 arrives as "!"
    assert H.from_qt(0x35, False, False, False, False, keypad=True) == "Num5"
    assert H.from_qt(0x01000021, True, False, False, False) is None         # just Ctrl held
    assert H.from_qt(0x01000099, True, False, False, False) is None         # unsupported key
    assert H.is_valid(H.from_qt(0x4C, True, False, True, False))
