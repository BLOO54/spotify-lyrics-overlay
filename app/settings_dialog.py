"""The settings window. Every change is applied instantly and saved automatically."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QFontComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from . import APP_NAME, APP_VERSION, hotkeys
from .config import POSITIONS, Settings
from .themes import CUSTOM, HIGHLIGHT_STYLES, THEME_KEYS, THEMES, apply_theme
from .translate import LANGUAGES, romaji_available

STYLE = """
QDialog, QTabWidget::pane, QWidget#page { background: #12121c; color: #e8e8f0; }
QTabWidget::pane { border: 1px solid #2c2c40; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #1c1c2b; color: #b8b8cc; padding: 7px 14px; border: 1px solid #2c2c40;
               border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #2a1f48; color: #fff; border-color: #a855f7; }
QLabel, QCheckBox { color: #e8e8f0; }
QComboBox, QSpinBox, QFontComboBox, QLineEdit { background: #1c1c2b; color: #fff; border: 1px solid #34344c;
            border-radius: 5px; padding: 3px 6px; min-height: 22px; }
QComboBox QAbstractItemView { background: #1c1c2b; color: #fff; selection-background-color: #7c3aed; }
QPushButton { background: #2a2a40; color: #fff; border: 1px solid #3a3a58; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #3b2a66; border-color: #a855f7; }
QSlider::groove:horizontal { height: 4px; background: #2c2c40; border-radius: 2px; }
QSlider::handle:horizontal { background: #a855f7; width: 14px; margin: -6px 0; border-radius: 7px; }
QSlider::sub-page:horizontal { background: #7c3aed; border-radius: 2px; }
"""

LINE_CHOICES = [
    "1 line – current line only",
    "2 lines – current + next",
    "3 lines – previous, current, next",
]
LONG_CHOICES = [("widen", "Make the overlay wider (up to the screen edge)"),
                ("shrink", "Shrink the text to fit on one row"),
                ("wrap", "Wrap onto more rows")]
ART_CHOICES = [("auto", "Auto – small when the overlay is narrow"), ("left", "Left side (big)"),
               ("header", "Next to the song title (small)")]
TIME_CHOICES = [("off", "Off"), ("left", "Time left  (-2:13)"), ("elapsed", "Elapsed / total  (1:32 / 3:45)")]
TRANSLATION_LAYOUTS = [("big", "Translation big (karaoke), original small underneath"),
                       ("small", "Original big, translation small underneath")]
WORD_TIMING_CHOICES = [("syllables", "Smart – by syllables (recommended)"),
                       ("letters", "Simple – by number of letters")]
BG_STYLES = [("solid", "Solid (see-through colour)"),
             ("frosted", "Frosted glass (blurs what's behind)")]
PROVIDERS = [("mymemory", "MyMemory – free, no account needed"),
             ("deepl", "DeepL – better quality, needs a free API key")]

def hotkey_help(s) -> str:
    rows = [f"<b>{getattr(s, f)}</b> {label.lower()}" for f, label, _ in hotkeys.ACTIONS if getattr(s, f)]
    return " &nbsp;·&nbsp; ".join(rows) if rows else "No keyboard shortcuts set – use the tray icon menu."


class HotkeyButton(QPushButton):
    """Click it, then press the key combination you want."""
    captured = Signal(str)        # new shortcut text ("" = turned off)
    capture_started = Signal()
    capture_finished = Signal()

    def __init__(self, text: str):
        super().__init__()
        self._value = text
        self._capturing = False
        self.setMinimumWidth(170)
        self.clicked.connect(self._start)
        self._show()

    def value(self) -> str:
        return self._value

    def set_value(self, text: str):
        self._value = text
        self._show()

    def _show(self):
        if self._capturing:
            self.setText("Press keys…  (Esc = cancel)")
            self.setStyleSheet("background:#3b2a66; border:1px solid #a855f7;")
        else:
            self.setText(self._value or "(off)")
            self.setStyleSheet("")

    def _start(self):
        self._capturing = True
        self._show()
        self.capture_started.emit()
        self.setFocus()
        self.grabKeyboard()

    def _stop(self):
        self._capturing = False
        self.releaseKeyboard()
        self._show()
        self.capture_finished.emit()

    def focusOutEvent(self, e):
        if self._capturing:
            self._stop()
        super().focusOutEvent(e)

    def keyPressEvent(self, e):
        if not self._capturing:
            return super().keyPressEvent(e)
        key = int(e.key())
        if key == hotkeys.QT_ESCAPE:
            self._stop()
            return
        if key == hotkeys.QT_BACKSPACE:
            self._value = ""
            self.captured.emit("")
            self._stop()
            return
        m = e.modifiers()
        text = hotkeys.from_qt(
            key,
            bool(m & Qt.KeyboardModifier.ControlModifier),
            bool(m & Qt.KeyboardModifier.AltModifier),
            bool(m & Qt.KeyboardModifier.ShiftModifier),
            bool(m & Qt.KeyboardModifier.MetaModifier),
            bool(m & Qt.KeyboardModifier.KeypadModifier),
        )
        if text is None:
            return                      # just a modifier so far - keep waiting
        if not hotkeys.is_valid(text):
            self.setText("Add Ctrl / Shift / Win (or use an F-key)")
            return
        self._value = hotkeys.normalise(text)
        self.captured.emit(self._value)
        self._stop()


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setOpenExternalLinks(True)
    lab.setStyleSheet("color:#9a9ab0; font-size:11px;")
    return lab


class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, color: str):
        super().__init__()
        self.setFixedWidth(90)
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, color: str):
        self._color = color
        c = QColor(color)
        fg = "#000" if c.lightness() > 140 else "#fff"
        self.setText(color.upper())
        self.setStyleSheet(f"background:{color}; color:{fg}; border:1px solid #555; border-radius:6px;")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Choose colour")
        if c.isValid():
            self.set_color(c.name())
            self.changed.emit(c.name())


class SettingsDialog(QDialog):
    changed = Signal()
    capture_started = Signal()     # a shortcut button is waiting for keys
    capture_finished = Signal()    # done - shortcuts should be registered again

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle(f"{APP_NAME} – Settings")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(520)
        self._building = True
        self._w = {}      # setting name -> widget, for refreshing

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)
        tabs.addTab(self._look_tab(), "Look")
        tabs.addTab(self._layout_tab(), "Layout")
        tabs.addTab(self._lyrics_tab(), "Lyrics && translation")
        tabs.addTab(self._behaviour_tab(), "Behaviour")
        tabs.addTab(self._shortcuts_tab(), "Shortcuts")

        self.keys_label = QLabel(hotkey_help(self.s))
        self.keys_label.setWordWrap(True)
        self.keys_label.setStyleSheet("color:#b8b8cc; font-size:11px; padding:4px;")
        root.addWidget(self.keys_label)

        btns = QHBoxLayout()
        ver = QLabel(f"v{APP_VERSION} · lyrics from LRCLIB.net")
        ver.setStyleSheet("color:#6c6c88; font-size:10px;")
        btns.addWidget(ver)
        btns.addStretch(1)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        btns.addWidget(reset)
        btns.addWidget(close)
        root.addLayout(btns)
        self._building = False

    # ------------------------------------------------------------ tabs
    def _page(self):
        w = QWidget()
        w.setObjectName("page")
        f = QFormLayout(w)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        return w, f

    def _look_tab(self):
        w, f = self._page()
        self.theme_box = QComboBox()
        self.theme_box.addItems(list(THEMES) + [CUSTOM])
        self.theme_box.setCurrentText(self.s.theme if self.s.theme in THEMES else CUSTOM)
        self.theme_box.currentTextChanged.connect(self._theme_chosen)
        f.addRow("Theme", self.theme_box)
        f.addRow(_hint("Pick a theme, then fine-tune anything below. Changing a colour or font "
                       "switches the theme to “Custom”."))

        self._w["font_family"] = fb = QFontComboBox()
        fb.setCurrentFont(QFont(self.s.font_family))
        fb.currentFontChanged.connect(lambda fnt: self._set("font_family", fnt.family()))
        f.addRow("Font", fb)
        self._w["font_size"] = self._spin(10, 120, "font_size", " px")
        f.addRow("Font size", self._w["font_size"])
        self._w["bold"] = self._check("Bold", "bold")
        f.addRow(self._w["bold"])

        for key, label in (("text_color", "Lyrics colour"), ("highlight_color", "Highlight colour"),
                           ("highlight_color2", "Highlight (gradient end)"),
                           ("background_color", "Background colour")):
            b = ColorButton(getattr(self.s, key))
            b.changed.connect(lambda c, k=key: self._set(k, c))
            self._w[key] = b
            f.addRow(label, b)

        self._w["highlight_style"] = hs = QComboBox()
        for key, label in HIGHLIGHT_STYLES.items():
            hs.addItem(label, key)
        hs.setCurrentIndex(list(HIGHLIGHT_STYLES).index(self.s.highlight_style))
        hs.currentIndexChanged.connect(lambda i: self._set("highlight_style", hs.itemData(i)))
        f.addRow("Highlight style", hs)
        self._w["glow"] = self._check("Glow around highlighted words", "glow")
        f.addRow(self._w["glow"])
        self._w["keep_sung_colored"] = self._check("Keep sung words coloured", "keep_sung_colored")
        f.addRow(self._w["keep_sung_colored"])

        self._w["show_background"] = self._check("Show background box (untick for lyrics only)", "show_background")
        f.addRow(self._w["show_background"])
        self._w["background_style"] = bs = QComboBox()
        for key, label in BG_STYLES:
            bs.addItem(label, key)
        bs.setCurrentIndex([k for k, _ in BG_STYLES].index(self.s.background_style))
        bs.currentIndexChanged.connect(lambda i: self._set("background_style", bs.itemData(i)))
        f.addRow("Background style", bs)
        from . import win32 as _w
        if _w.IS_WINDOWS and not _w.transparency_effects_on():
            note = ("⚠ Windows “Transparency effects” is OFF, so frosted glass can't be used right now. "
                    "Turn it on in Windows Settings → Personalization → Colors.")
        else:
            note = ("Frosted glass uses Windows “Transparency effects” (Settings → Personalization → Colors). "
                    "If that's off, the normal background is used.")
        f.addRow(_hint(note))
        self._w["corner_radius"] = self._spin(0, 40, "corner_radius", " px")
        f.addRow("Corner rounding", self._w["corner_radius"])
        self._w["background_opacity"] = self._slider("background_opacity", 0)
        f.addRow("Background opacity", self._w["background_opacity"])
        self._w["window_opacity"] = self._slider("window_opacity", 10)
        f.addRow("Overall opacity", self._w["window_opacity"])

        sep = QLabel("<b>Match the album art</b>")
        sep.setStyleSheet("color:#c9b8ff; padding-top:8px;")
        f.addRow(sep)
        self._w["match_album_highlight"] = self._check("Highlight colours from the album art", "match_album_highlight")
        f.addRow(self._w["match_album_highlight"])
        self._w["match_album_background"] = self._check("Background colour from the album art", "match_album_background")
        f.addRow(self._w["match_album_background"])
        self._w["smooth_colour_changes"] = self._check("Fade colours smoothly between songs", "smooth_colour_changes")
        f.addRow(self._w["smooth_colour_changes"])
        f.addRow(_hint("Works even when the album art itself is hidden (Layout → Show album art)."))
        return w

    def _layout_tab(self):
        w, f = self._page()
        self._w["lines_shown"] = lb = QComboBox()
        lb.addItems(LINE_CHOICES)
        lb.setCurrentIndex(self.s.lines_shown - 1)
        lb.currentIndexChanged.connect(lambda i: self._set("lines_shown", i + 1))
        f.addRow("Lines shown", lb)
        self._w["long_lines"] = ll = QComboBox()
        for key, label in LONG_CHOICES:
            ll.addItem(label, key)
        ll.setCurrentIndex([k for k, _ in LONG_CHOICES].index(self.s.long_lines))
        ll.currentIndexChanged.connect(lambda i: self._set("long_lines", ll.itemData(i)))
        f.addRow("Long lines", ll)
        self._w["show_title"] = self._check("Show song title and artist", "show_title")
        f.addRow(self._w["show_title"])
        self._w["show_album_art"] = self._check("Show album art", "show_album_art")
        f.addRow(self._w["show_album_art"])
        self._w["album_art_position"] = ab = QComboBox()
        for key, label in ART_CHOICES:
            ab.addItem(label, key)
        ab.setCurrentIndex([k for k, _ in ART_CHOICES].index(self.s.album_art_position))
        ab.currentIndexChanged.connect(lambda i: self._set("album_art_position", ab.itemData(i)))
        f.addRow("Album art position", ab)
        self._w["time_display"] = tb = QComboBox()
        for key, label in TIME_CHOICES:
            tb.addItem(label, key)
        tb.setCurrentIndex([k for k, _ in TIME_CHOICES].index(self.s.time_display))
        tb.currentIndexChanged.connect(lambda i: self._set("time_display", tb.itemData(i)))
        f.addRow("Song timer", tb)
        self._w["show_progress"] = self._check("Show song progress bar", "show_progress")
        f.addRow(self._w["show_progress"])

        self._w["position"] = pb = QComboBox()
        pb.addItems([p.replace("-", " ").title() for p in POSITIONS])
        pb.setCurrentIndex(POSITIONS.index(self.s.position))
        pb.currentIndexChanged.connect(lambda i: self._set("position", POSITIONS[i]))
        f.addRow("Screen position", pb)
        self._w["screen_index"] = sb = QComboBox()
        for i, sc in enumerate(QApplication.screens()):
            geo = sc.geometry()
            sb.addItem(f"Monitor {i + 1}  ({geo.width()}×{geo.height()})")
        sb.setCurrentIndex(max(0, min(self.s.screen_index, sb.count() - 1)))
        sb.currentIndexChanged.connect(lambda i: self._set("screen_index", i))
        f.addRow("Monitor", sb)
        self._w["width"] = self._spin(300, 4000, "width", " px", step=20)
        self._w["width"].setToolTip("With 'Make the overlay wider', this is the normal width; "
                                    "it stretches for long lines but never past the screen.")
        f.addRow("Width", self._w["width"])
        self._w["margin"] = self._spin(0, 1000, "margin", " px", step=5)
        f.addRow("Distance from edge", self._w["margin"])
        f.addRow(_hint("Tip: turn click-through off, then drag the overlay with the mouse to place it anywhere."))
        return w

    def _lyrics_tab(self):
        w, f = self._page()
        self._w["estimate_word_timing"] = self._check(
            "Estimate word timing when only line timing exists", "estimate_word_timing")
        f.addRow(self._w["estimate_word_timing"])
        self._w["word_timing_mode"] = wt = QComboBox()
        for key, label in WORD_TIMING_CHOICES:
            wt.addItem(label, key)
        wt.setCurrentIndex([k for k, _ in WORD_TIMING_CHOICES].index(self.s.word_timing_mode))
        wt.currentIndexChanged.connect(lambda i: self._set("word_timing_mode", wt.itemData(i)))
        f.addRow("Word timing guess", wt)
        f.addRow(_hint("Only used for songs without real word timing. Smart gives each word time "
                       "for the syllables you actually sing, and adds a short breath after commas."))
        self._w["sync_offset_ms"] = self._spin(-10000, 10000, "sync_offset_ms", " ms", step=50)
        self._w["sync_offset_ms"].setToolTip("Positive = lyrics appear earlier. Use if lyrics feel late.")
        f.addRow("Sync adjustment (all songs)", self._w["sync_offset_ms"])
        f.addRow(_hint("For just one song, use Ctrl+Alt+← / → while it plays – it's remembered for that song. "
                       "If a song's lyrics are badly off, try Ctrl+Alt+V for another lyrics version."))

        sep = QLabel("<b>Translation</b>")
        sep.setStyleSheet("color:#c9b8ff; padding-top:8px;")
        f.addRow(sep)
        self._w["translate_enabled"] = self._check(
            "Translate songs that aren't in my language", "translate_enabled")
        f.addRow(self._w["translate_enabled"])
        self._w["translate_target"] = lang = QComboBox()
        for code, (name, _, _) in LANGUAGES.items():
            lang.addItem(name, code)
        lang.setCurrentIndex(list(LANGUAGES).index(self.s.translate_target))
        lang.currentIndexChanged.connect(lambda i: self._set("translate_target", lang.itemData(i)))
        f.addRow("My language", lang)
        self._w["translation_layout"] = tl = QComboBox()
        for key, label in TRANSLATION_LAYOUTS:
            tl.addItem(label, key)
        tl.setCurrentIndex([k for k, _ in TRANSLATION_LAYOUTS].index(self.s.translation_layout))
        tl.currentIndexChanged.connect(lambda i: self._set("translation_layout", tl.itemData(i)))
        f.addRow("Show translation", tl)
        self._w["translate_provider"] = prov = QComboBox()
        for key, label in PROVIDERS:
            prov.addItem(label, key)
        prov.setCurrentIndex([k for k, _ in PROVIDERS].index(self.s.translate_provider))
        prov.currentIndexChanged.connect(lambda i: (self._set("translate_provider", prov.itemData(i)),
                                                    self._update_provider_rows()))
        f.addRow("Service", prov)

        self._w["translate_email"] = em = QLineEdit(self.s.translate_email)
        em.setPlaceholderText("optional – any email, raises the free limit 10×")
        em.editingFinished.connect(lambda: self._set("translate_email", em.text().strip()))
        self._email_label = QLabel("Email (MyMemory)")
        f.addRow(self._email_label, em)
        self._w["deepl_key"] = key = QLineEdit(self.s.deepl_key)
        key.setEchoMode(QLineEdit.EchoMode.Password)
        key.setPlaceholderText("paste your DeepL API key (ends in :fx)")
        key.editingFinished.connect(lambda: self._set("deepl_key", key.text().strip()))
        self._key_label = QLabel("DeepL API key")
        f.addRow(self._key_label, key)
        f.addRow(_hint("MyMemory: about 5,000 characters/day free (≈5–10 songs), 50,000 with an email.<br>"
                       "DeepL: 500,000 characters/month free – make a free account at "
                       "<a style='color:#c9b8ff' href='https://www.deepl.com/pro-api'>deepl.com/pro-api</a>. "
                       "Each song is only translated once and then remembered."))
        self._update_provider_rows()

        self._w["show_romaji"] = self._check("Show Japanese pronunciation (romaji)", "show_romaji")
        f.addRow(self._w["show_romaji"])
        if not romaji_available():
            self._w["show_romaji"].setEnabled(False)
            f.addRow(_hint("Romaji isn't available in this build."))
        else:
            f.addRow(_hint("Romaji is worked out on your PC – no internet or limits."))
        return w

    def _behaviour_tab(self):
        w, f = self._page()
        for key, text in (("click_through", "Click-through (mouse passes through to the game)"),
                          ("hide_when_paused", "Hide overlay while music is paused"),
                          ("spotify_only", "Only follow Spotify (ignore other media players)")):
            self._w[key] = self._check(text, key)
            f.addRow(self._w[key])
        return w

    def _shortcuts_tab(self):
        w, f = self._page()
        f.addRow(_hint("Click a button, then press the keys you want (for example Ctrl+Shift+L, or just F8). "
                       "Backspace turns a shortcut off. Esc cancels. Shortcuts work even while a game is focused."))
        self.hk_buttons = {}
        for field, label, _ in hotkeys.ACTIONS:
            b = HotkeyButton(getattr(self.s, field))
            b.captured.connect(lambda text, fld=field: self._set_hotkey(fld, text))
            b.capture_started.connect(self.capture_started)
            b.capture_finished.connect(self.capture_finished)
            self.hk_buttons[field] = b
            f.addRow(label, b)
        self.hk_problem = _hint("")
        self.hk_problem.setStyleSheet("color:#fca5a5; font-size:11px;")
        f.addRow(self.hk_problem)
        no_alt = QPushButton("Use shortcuts without Alt (Ctrl+Shift + …)")
        no_alt.clicked.connect(self._no_alt_preset)
        f.addRow(no_alt)
        defaults = QPushButton("Restore default shortcuts")
        defaults.clicked.connect(self._default_hotkeys)
        f.addRow(defaults)
        return w

    def _set_hotkey(self, field: str, text: str):
        # one key combination can only do one thing: clear it from any other action
        if text:
            for other, b in self.hk_buttons.items():
                if other != field and getattr(self.s, other) == text:
                    setattr(self.s, other, "")
                    b.set_value("")
        setattr(self.s, field, text)
        self.s.clamp()
        self.s.save()
        self.keys_label.setText(hotkey_help(self.s))

    def _apply_hotkeys(self, mapping: dict):
        for field, text in mapping.items():
            setattr(self.s, field, text)
            self.hk_buttons[field].set_value(text)
        self.s.clamp()
        self.s.save()
        self.keys_label.setText(hotkey_help(self.s))
        self.capture_started.emit()       # unregister...
        self.capture_finished.emit()      # ...and register the new set

    def _no_alt_preset(self):
        self._apply_hotkeys({f: d.replace("Ctrl+Alt+", "Ctrl+Shift+") for f, d in hotkeys.DEFAULTS.items()})

    def _default_hotkeys(self):
        self._apply_hotkeys(dict(hotkeys.DEFAULTS))

    def show_hotkey_problems(self, failed: list):
        if failed:
            self.hk_problem.setText("Already used by another program, so it won't work: " + ", ".join(failed)
                                    + ". Pick a different combination.")
        else:
            self.hk_problem.setText("")

    def _update_provider_rows(self):
        deepl = self.s.translate_provider == "deepl"
        self._w["translate_email"].setVisible(not deepl)
        self._email_label.setVisible(not deepl)
        self._w["deepl_key"].setVisible(deepl)
        self._key_label.setVisible(deepl)

    # ------------------------------------------------------------ helpers
    def _set(self, key, value):
        if self._building:
            return
        setattr(self.s, key, value)
        if key in THEME_KEYS and self.s.theme != CUSTOM:
            self.s.theme = CUSTOM
            self._building = True
            self.theme_box.setCurrentText(CUSTOM)
            self._building = False
        self.s.clamp()
        self.s.save()
        self.changed.emit()

    def _theme_chosen(self, name: str):
        if self._building or name == CUSTOM:
            return
        apply_theme(self.s, name)
        self.s.save()
        self.refresh()
        self.changed.emit()

    def _spin(self, lo, hi, key, suffix="", step=1):
        b = QSpinBox()
        b.setRange(lo, hi)
        b.setSingleStep(step)
        b.setValue(int(getattr(self.s, key)))
        b.setSuffix(suffix)
        b.valueChanged.connect(lambda v: self._set(key, int(v)))
        return b

    def _check(self, text, key):
        b = QCheckBox(text)
        b.setChecked(bool(getattr(self.s, key)))
        b.toggled.connect(lambda v: self._set(key, bool(v)))
        return b

    def _slider(self, key, minimum):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(minimum, 100)
        sl.setValue(int(round(getattr(self.s, key) * 100)))
        lab = QLabel(f"{sl.value()}%")
        lab.setFixedWidth(40)
        sl.valueChanged.connect(lambda v: (lab.setText(f"{v}%"), self._set(key, v / 100.0)))
        lay.addWidget(sl)
        lay.addWidget(lab)
        w.slider = sl
        return w

    def refresh(self):
        """Make every widget show the current settings (after theme / reset / hotkeys)."""
        self._building = True
        try:
            s = self.s
            self.theme_box.setCurrentText(s.theme if s.theme in THEMES else CUSTOM)
            for key, wdg in self._w.items():
                val = getattr(s, key)
                if isinstance(wdg, ColorButton):
                    wdg.set_color(val)
                elif isinstance(wdg, QFontComboBox):
                    wdg.setCurrentFont(QFont(val))
                elif isinstance(wdg, QCheckBox):
                    wdg.setChecked(bool(val))
                elif isinstance(wdg, QSpinBox):
                    wdg.setValue(int(val))
                elif isinstance(wdg, QLineEdit):
                    wdg.setText(val)
                elif hasattr(wdg, "slider"):
                    wdg.slider.setValue(int(round(val * 100)))
                elif isinstance(wdg, QComboBox):
                    if key == "lines_shown":
                        wdg.setCurrentIndex(val - 1)
                    elif key == "position":
                        wdg.setCurrentIndex(POSITIONS.index(val))
                    elif key == "screen_index":
                        wdg.setCurrentIndex(max(0, min(val, wdg.count() - 1)))
                    else:
                        i = wdg.findData(val)
                        if i >= 0:
                            wdg.setCurrentIndex(i)
            self._update_provider_rows()
            for field, b in getattr(self, "hk_buttons", {}).items():
                b.set_value(getattr(s, field))
            self.keys_label.setText(hotkey_help(s))
        finally:
            self._building = False

    def sync_from_settings(self):
        self.refresh()

    def _reset(self):
        fresh = Settings()
        fresh.first_run = False
        # keep the user's translation account details
        fresh.translate_email, fresh.deepl_key = self.s.translate_email, self.s.deepl_key
        for k, v in vars(fresh).items():
            setattr(self.s, k, v)
        self.s.save()
        self.refresh()
        self.changed.emit()
        self.capture_started.emit()       # re-register the default shortcuts
        self.capture_finished.emit()
