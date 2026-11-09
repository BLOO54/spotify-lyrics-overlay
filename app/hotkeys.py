"""Keyboard shortcuts: turning text like "Ctrl+Shift+F8" into what Windows needs, and back.

Plain English: shortcuts are stored as readable text in settings.json. When the
app starts (or you change one in Settings) each one is converted into a Windows
"modifier + key code" pair and registered with RegisterHotKey, so it works even
while a game has focus. An empty shortcut means "turned off".
"""

from __future__ import annotations

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

MODIFIERS = [("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)]

# key name -> Windows virtual-key code
KEYS: dict[str, int] = {}
KEYS.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(d): 0x30 + d for d in range(10)})
KEYS.update({f"F{n}": 0x6F + n for n in range(1, 25)})              # F1 = 0x70
KEYS.update({f"Num{d}": 0x60 + d for d in range(10)})
KEYS.update({
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Insert": 0x2D, "Delete": 0x2E, "Space": 0x20, "Pause": 0x13,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "Num*": 0x6A, "Num+": 0x6B, "Num-": 0x6D, "Num.": 0x6E, "Num/": 0x6F,
})
_NAME_OF = {}
for _k, _v in KEYS.items():
    _NAME_OF.setdefault(_v, _k)
# keys that are fine on their own (no Ctrl/Alt/...) because they don't type text
STANDALONE = {f"F{n}" for n in range(1, 25)} | {"Pause", "Insert", "Home", "End", "PageUp", "PageDown"} \
    | {f"Num{d}" for d in range(10)} | {"Num*", "Num+", "Num-", "Num.", "Num/"}

ACTIONS = [
    # (settings field, label, default)
    ("hk_toggle", "Show / hide overlay", "Ctrl+Alt+L"),
    ("hk_click", "Click-through on / off", "Ctrl+Alt+K"),
    ("hk_settings", "Open settings", "Ctrl+Alt+O"),
    ("hk_earlier", "This song: lyrics earlier", "Ctrl+Alt+Left"),
    ("hk_later", "This song: lyrics later", "Ctrl+Alt+Right"),
    ("hk_version", "Try another lyrics version", "Ctrl+Alt+V"),
    ("hk_report", "Save a problem report (for Claude)", "Ctrl+Alt+R"),
    ("hk_mini", "Small modes: off → 2 lines → 1 line", "Ctrl+Alt+M"),
]
DEFAULTS = {field: default for field, _, default in ACTIONS}


def parse(text: str):
    """'Ctrl+Shift+F8' -> (modifier flags, virtual-key code); None if empty/invalid."""
    if not text or not text.strip():
        return None
    parts = [p.strip() for p in text.replace(" ", "").split("+")]
    # a "+" key would split into empty parts; we don't offer it
    if any(not p for p in parts):
        return None
    mods = 0
    key = None
    names = {n.lower(): f for n, f in MODIFIERS}
    for p in parts:
        if p.lower() in names:
            mods |= names[p.lower()]
        elif key is None:
            match = next((k for k in KEYS if k.lower() == p.lower()), None)
            if match is None:
                return None
            key = match
        else:
            return None            # two normal keys
    if key is None:
        return None
    if mods == 0 and key not in STANDALONE:
        return None                # a plain letter would block normal typing
    return mods, KEYS[key]


def is_valid(text: str) -> bool:
    return parse(text) is not None


def normalise(text: str) -> str:
    """Tidy capitalisation/order: 'shift+ctrl+f8' -> 'Ctrl+Shift+F8'."""
    p = parse(text)
    if p is None:
        return ""
    mods, vk = p
    return "+".join([n for n, f in MODIFIERS if mods & f] + [_NAME_OF[vk]])


# --- turning a Qt key press into text (used by the "press a key" button) ---
QT_KEY_SPECIAL = {
    0x01000012: "Left", 0x01000013: "Up", 0x01000014: "Right", 0x01000015: "Down",
    0x01000010: "Home", 0x01000011: "End", 0x01000016: "PageUp", 0x01000017: "PageDown",
    0x01000006: "Insert", 0x01000007: "Delete", 0x01000008: "Pause", 0x20: "Space",
}
QT_MODIFIER_KEYS = {0x01000020, 0x01000021, 0x01000022, 0x01000023, 0x01001103}  # Shift, Ctrl, Meta, Alt, AltGr
QT_ESCAPE = 0x01000000
QT_BACKSPACE = 0x01000003
# Shift+number/punctuation arrives as the shifted symbol; map back to the key (US layout)
_SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
            ")": "0", ":": ";", "+": "=", "<": ",", "_": "-", ">": ".", "?": "/", "~": "`",
            "{": "[", "|": "\\", "}": "]", '"': "'"}


def from_qt(key: int, ctrl: bool, alt: bool, shift: bool, win: bool, keypad: bool = False) -> str | None:
    """Text for a key press, or None if it's only a modifier / not supported."""
    if key in QT_MODIFIER_KEYS:
        return None
    name = None
    if 0x01000030 <= key <= 0x01000047:                   # F1..F24
        name = f"F{key - 0x01000030 + 1}"
    elif key in QT_KEY_SPECIAL:
        name = QT_KEY_SPECIAL[key]
    elif 0x21 <= key <= 0x7E:
        ch = chr(key).upper()
        ch = _SHIFTED.get(ch, ch)
        if keypad and ch in "0123456789*+-./":
            name = "Num" + ch
        elif ch in KEYS:
            name = ch
    if name is None:
        return None
    mods = [n for n, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift), ("Win", win)) if on]
    return "+".join(mods + [name])
