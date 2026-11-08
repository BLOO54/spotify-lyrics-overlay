"""The settings window. Every change is applied instantly and saved automatically."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFontComboBox,
                               QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
                               QMessageBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import APP_NAME, APP_VERSION, hotkeys
from .config import POSITIONS, Settings
from .themes import CUSTOM, HIGHLIGHT_STYLES, THEME_KEYS, THEMES, apply_theme
from .translate import LANGUAGES, romaji_available

# One design language for every window: deep charcoal, soft cards, violet accent.
from .ai_sync import MODELS as AI_MODELS

STYLE = """
QDialog, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget { background: #0f0f15; color: #ececf3; }
QWidget { font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 13px; }
QFrame#card { background: #17171f; border: 1px solid #24242f; border-radius: 14px; }
QFrame#sidebar { background: #13131a; border-right: 1px solid #1f1f29; }
QFrame#preview { background: #0b0b10; border: 1px solid #24242f; border-radius: 14px; }
QLabel { color: #ececf3; background: transparent; }
QLabel#pageTitle { font-size: 22px; font-weight: 600; }
QLabel#pageSub { color: #8d8da3; font-size: 12px; }
QLabel#section { color: #b9a6ff; font-weight: 600; font-size: 12px; padding-top: 6px; letter-spacing: 0.5px; }
QCheckBox { color: #ececf3; spacing: 10px; background: transparent; }
QCheckBox::indicator { width: 34px; height: 18px; border-radius: 9px; background: #2a2a36; border: 1px solid #383848; }
QCheckBox::indicator:checked { background: #8b5cf6; border-color: #a78bfa; }
QComboBox, QSpinBox, QFontComboBox, QLineEdit { background: #1d1d27; color: #fff; border: 1px solid #2e2e3c;
            border-radius: 8px; padding: 5px 10px; min-height: 24px; }
QComboBox:hover, QSpinBox:hover, QLineEdit:hover { border-color: #4a4a62; }
QComboBox:focus, QSpinBox:focus, QLineEdit:focus { border-color: #8b5cf6; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #1d1d27; color: #fff; border: 1px solid #2e2e3c; border-radius: 8px;
            selection-background-color: #6d28d9; outline: 0; padding: 4px; }
QPushButton { background: #22222d; color: #fff; border: 1px solid #32323f; border-radius: 9px; padding: 7px 14px; }
QPushButton:hover { background: #2c2440; border-color: #8b5cf6; }
QPushButton:pressed { background: #3b2a66; }
QPushButton#primary { background: #7c3aed; border-color: #8b5cf6; font-weight: 600; }
QPushButton#primary:hover { background: #8b5cf6; }
QSlider::groove:horizontal { height: 4px; background: #2a2a36; border-radius: 2px; }
QSlider::handle:horizontal { background: #fff; border: 3px solid #8b5cf6; width: 12px; height: 12px;
            margin: -7px 0; border-radius: 9px; }
QSlider::sub-page:horizontal { background: #8b5cf6; border-radius: 2px; }
QListWidget#nav { background: transparent; border: none; outline: 0; color: #b8b8cc; font-size: 14px; }
QListWidget#nav::item { padding: 10px 14px; margin: 2px 8px; border-radius: 10px; }
QListWidget#nav::item:hover { background: #1c1c26; color: #fff; }
QListWidget#nav::item:selected { background: #241b3d; color: #fff; }
QProgressBar { background: #1b1b24; border: 1px solid #2a2a36; border-radius: 6px; height: 10px;
               text-align: center; color: transparent; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:1 #a78bfa);
                      border-radius: 5px; }
QListWidget#synced { background: #14141b; border: 1px solid #24242f; border-radius: 8px; padding: 6px; }
QListWidget#synced::item { border-bottom: 1px solid #22222c; }
QLabel#badgeMine { background: #1e3a2f; color: #6ee7b7; border-radius: 9px; padding: 2px 9px; font-size: 11px; }
QLabel#badgeAi { background: #2e2350; color: #c4b5fd; border-radius: 9px; padding: 2px 9px; font-size: 11px; }
QLabel#rowTitle { font-size: 14px; font-weight: 600; color: #ececf3; background: transparent; }
QLabel#rowSub { font-size: 12px; color: #8a8aa3; background: transparent; }
QPushButton#danger { background: #2a1a1f; color: #fca5a5; border: 1px solid #4a2a33; }
QPushButton#danger:hover { background: #3a1f27; }
QListWidget#history { background: #14141b; border: 1px solid #24242f; border-radius: 8px; padding: 6px;
                      color: #e6e6f0; }
QListWidget#history::item { padding: 10px 8px; border-bottom: 1px solid #22222c; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; border: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar::handle:vertical { background: #2e2e3c; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #1d1d27; color: #fff; border: 1px solid #2e2e3c; padding: 6px; border-radius: 6px; }
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
             ("frosted", "Frosted glass – experimental (blurs what's behind)")]
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
    check_updates_requested = Signal()
    tap_sync_requested = Signal()
    lyrics_folder_requested = Signal()
    update_requested = Signal()
    ai_download_requested = Signal(str)
    ai_remove_requested = Signal(str)
    ai_cancel_requested = Signal()
    ai_status_wanted = Signal()
    history_clear_requested = Signal()
    meanings_requested = Signal()
    ai_window_requested = Signal()
    fullscreen_requested = Signal()
    synced_wanted = Signal()
    synced_remove_requested = Signal(str, str)      # kind ("mine" / "ai"), file path
    synced_remove_all_requested = Signal(object)    # list of (kind, path)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle(f"{APP_NAME} – Settings")
        self.setStyleSheet(STYLE)
        self.resize(940, 680)
        self.setMinimumSize(760, 520)
        self._building = True
        self._w = {}      # setting name -> widget, for refreshing
        self._preview_fn = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- sidebar
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 18, 0, 14)
        brand = QLabel("♪  Lyrics Overlay")
        brand.setStyleSheet("font-size:16px; font-weight:600; padding: 0 22px 14px 22px;")
        sl.addWidget(brand)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        sl.addWidget(self.nav, 1)
        ver = QLabel(f"Version {APP_VERSION}\nLyrics from LRCLIB.net")
        ver.setStyleSheet("color:#5f5f78; font-size:11px; padding: 0 22px;")
        sl.addWidget(ver)
        root.addWidget(side)

        # ---- content
        content = QVBoxLayout()
        content.setContentsMargins(26, 20, 26, 18)
        content.setSpacing(12)
        self.preview_frame = QFrame()
        self.preview_frame.setObjectName("preview")
        pl = QVBoxLayout(self.preview_frame)
        pl.setContentsMargins(10, 10, 10, 10)
        self.preview = QLabel("Live preview appears here")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(120)
        self.preview.setStyleSheet("color:#5f5f78;")
        pl.addWidget(self.preview)
        content.addWidget(self.preview_frame)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel()
        self.page_sub.setObjectName("pageSub")
        content.addWidget(self.page_title)
        content.addWidget(self.page_sub)
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)

        pages = [
            ("Look", "Design, theme, colours and text sizes", self._look_tab()),
            ("Layout", "What's shown, where, and how big", self._layout_tab()),
            ("Lyrics & translation", "Timing, your own lyrics and translations", self._lyrics_tab()),
            ("Behaviour", "Click-through, notifications and updates", self._behaviour_tab()),
            ("Shortcuts", "Keys that work even in games", self._shortcuts_tab()),
            ("AI sync", "What the AI is doing, what it needs, and which lines it heard", self._ai_tab()),
            ("Song meanings", "What the lyrics mean – from Genius, with an optional AI backup",
             self._meanings_tab()),
            ("Synced songs", "Songs you or AI sync have timed – remove any you don't like", self._synced_tab()),
            ("Notifications", "Past pop-ups: songs synced, model downloads and saved timing", self._history_tab()),
        ]
        icons = ["◐", "▦", "♫", "⚙", "⌨", "✦", "💬", "⏱", "🔔"]
        self._pages = pages
        for (name, sub, widget), icon in zip(pages, icons):
            self.nav.addItem(QListWidgetItem(f"{icon}   {name}"))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(widget)
            self.stack.addWidget(scroll)
        self.nav.currentRowChanged.connect(self._show_page)

        self.keys_label = QLabel(hotkey_help(self.s))
        self.keys_label.setWordWrap(True)
        self.keys_label.setStyleSheet("color:#7f7f98; font-size:11px;")
        content.addWidget(self.keys_label)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.reset_note = QLabel("")
        self.reset_note.setWordWrap(True)
        self.reset_note.setStyleSheet("color:#fca5a5; font-size:12px;")
        self.reset_note.setVisible(False)
        content.addWidget(self.reset_note)
        reset = QPushButton("Reset to defaults")
        self.reset_btn = reset
        self._reset_state = "idle"          # idle -> armed (click again) -> undo (just reset)
        self._reset_backup = None
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._reset_idle)
        reset.clicked.connect(self._reset_clicked)
        close = QPushButton("Done")
        close.setObjectName("primary")
        close.clicked.connect(self.close)
        btns.addWidget(reset)
        btns.addWidget(close)
        content.addLayout(btns)
        root.addLayout(content, 1)

        self.nav.setCurrentRow(0)
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._update_preview)
        self.preview_timer.start(120)
        self._building = False

    def _show_page(self, row: int):
        if 0 <= row < len(self._pages):
            self.stack.setCurrentIndex(row)
            self.page_title.setText(self._pages[row][0])
            self.page_sub.setText(self._pages[row][1])

    def set_preview_source(self, fn):
        """fn() returns a QPixmap of the real overlay - shown live at the top of the window."""
        self._preview_fn = fn

    def _update_preview(self):
        if self._preview_fn is None or not self.isVisible():
            return
        try:
            pix = self._preview_fn()
        except Exception:
            return
        if pix is None or pix.isNull():
            return
        w = max(50, self.preview.width())
        h = max(50, self.preview.height())
        self.preview.setPixmap(pix.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))

    def showEvent(self, e):
        super().showEvent(e)
        self.ai_status_wanted.emit()
        self.synced_wanted.emit()
        try:
            from . import win32
            win32.set_dark_titlebar(int(self.winId()))
        except Exception:
            pass

    # ------------------------------------------------------------ tabs
    def _page(self):
        """A page: one rounded card holding a form."""
        outer = QWidget()
        outer.setObjectName("page")
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 4, 8, 4)
        card = QFrame()
        card.setObjectName("card")
        f = QFormLayout(card)
        f.setContentsMargins(22, 18, 22, 18)
        f.setHorizontalSpacing(18)
        f.setVerticalSpacing(12)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        ol.addWidget(card)
        ol.addStretch(1)
        return outer, f

    def _look_tab(self):
        w, f = self._page()
        from .overlay import OverlayWindow
        self._w["design"] = dz = QComboBox()
        for key, label in OverlayWindow.DESIGNS.items():
            dz.addItem(label, key)
        dz.setCurrentIndex(max(0, dz.findData(self.s.design)))
        dz.currentIndexChanged.connect(lambda i: self._set("design", dz.itemData(i)))
        f.addRow("Design", dz)
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
        f.addRow("Lyrics size", self._w["font_size"])
        self._w["side_font_size"] = self._spin(8, 80, "side_font_size", " px")
        f.addRow("Other lines size", self._w["side_font_size"])
        self._w["title_font_size"] = self._spin(8, 60, "title_font_size", " px")
        f.addRow("Title && timer size", self._w["title_font_size"])
        f.addRow(_hint("Lyrics size = the big current line. Other lines = previous/next lines, translation and "
                       "romaji. To stop the box growing when you make text bigger, use Layout → "
                       "Keep overlay size fixed."))
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
        sep.setObjectName("section")
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
        fs = QPushButton("Open full-screen lyrics")
        fs.clicked.connect(self.fullscreen_requested)
        fs_row = QHBoxLayout()
        fs_row.addWidget(fs)
        fs_row.addStretch(1)
        fs_holder = QWidget()
        fs_holder.setLayout(fs_row)
        f.addRow(fs_holder)
        f.addRow(_hint("Big lyrics over the blurred album art, filling the screen. Esc, F11 or a double-click "
                       "closes it. Also in the tray menu."))
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
        self._w["header_art_size"] = hs2 = QComboBox()
        for key, label in (("small", "Small"), ("medium", "Medium"), ("large", "Large")):
            hs2.addItem(label, key)
        hs2.setCurrentIndex(["small", "medium", "large"].index(self.s.header_art_size))
        hs2.currentIndexChanged.connect(lambda i: self._set("header_art_size", hs2.itemData(i)))
        f.addRow("Size next to the title", hs2)
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
        self._w["fixed_size"] = self._check("Keep overlay size fixed (text fits inside the box)", "fixed_size")
        f.addRow(self._w["fixed_size"])
        self._w["fixed_height"] = self._spin(0, 2000, "fixed_height", " px", step=10)
        self._w["fixed_height"].setToolTip("Height of the box while 'Keep overlay size fixed' is on. "
                                           "It's set to the current height when you switch it on.")
        f.addRow("Fixed height", self._w["fixed_height"])
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
        f.addRow(_hint("For just one song, use the earlier / later shortcuts while it plays – it's remembered "
                       "for that song. If a song's lyrics are badly off, try another lyrics version, or time it "
                       "yourself:"))
        row = QHBoxLayout()
        tap = QPushButton("Sync the current song myself…")
        tap.clicked.connect(self.tap_sync_requested)
        folder = QPushButton("Open my lyrics folder")
        folder.clicked.connect(self.lyrics_folder_requested)
        row.addWidget(tap)
        row.addWidget(folder)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        f.addRow(holder)
        self._w["use_my_files"] = self._check("Use my own synced files", "use_my_files")
        f.addRow(self._w["use_my_files"])
        f.addRow(_hint("Off = songs you synced yourself use the normal (or AI) timing instead. Your files are "
                       "kept – turn this back on any time."))

        sep = QLabel("<b>AI sync (optional)</b>")
        sep.setObjectName("section")
        f.addRow(sep)
        f.addRow(_hint("Listens to each song once, hears when every line is sung and remembers it – "
                       "from the next play on, the lyrics follow the singer closely. Works for songs with "
                       "only unsynced lyrics too. The sound is kept in memory only and never saved or "
                       "uploaded; each song's timing is a tiny file. Your own synced files always come first."))
        self._w["ai_sync_enabled"] = self._check("Use AI sync (turning it off keeps the model installed)",
                                                 "ai_sync_enabled")
        f.addRow(self._w["ai_sync_enabled"])
        self._w["ai_model"] = am = QComboBox()
        for key, what in (("light", "faster"), ("best", "more accurate, slower"),
                          ("max", "most accurate, 2–3× slower than Best")):
            _, label, size = AI_MODELS[key]
            am.addItem(f"{label} – {size} ({what})", key)
        am.setCurrentIndex(max(0, am.findData(self.s.ai_model)))
        am.currentIndexChanged.connect(lambda i: (self._set("ai_model", am.itemData(i)),
                                                  self.ai_status_wanted.emit()))
        f.addRow("Model", am)
        self._w["ai_threads"] = at = QComboBox()
        for n, label in ((1, "Very gentle – 1 core"), (2, "Gentle – 2 cores (recommended while gaming)"),
                         (4, "Faster – 4 cores"), (8, "Fastest – 8 cores")):
            at.addItem(label, n)
        at.setCurrentIndex(max(0, at.findData(self.s.ai_threads)))
        at.currentIndexChanged.connect(lambda i: self._set("ai_threads", at.itemData(i)))
        f.addRow("CPU use", at)
        self._w["ai_listen"] = al = QComboBox()
        al.addItem("Only Spotify (ignores game sound, Discord…)", "app")
        al.addItem("Everything the PC plays", "pc")
        al.setCurrentIndex(max(0, al.findData(self.s.ai_listen)))
        al.currentIndexChanged.connect(lambda i: self._set("ai_listen", al.itemData(i)))
        f.addRow("Listen to", al)
        sep2 = QLabel("Extra accuracy (slower)")
        sep2.setStyleSheet("color:#c9b8ff; font-weight:600; padding-top:4px;")
        f.addRow(sep2)
        self._w["ai_second_pass"] = self._check("Second listen: go back over the parts it missed, knowing which "
                                                "lyrics to expect there", "ai_second_pass")
        f.addRow(self._w["ai_second_pass"])
        self._w["ai_vocals"] = self._check("Voice track: separate the voice from the music to catch lines it "
                                           "missed (about 40 MB download)", "ai_vocals")
        f.addRow(self._w["ai_vocals"])
        f.addRow(_hint("Each song is still only done once, and these only ever add lines – never make the timing "
                       "worse. Roughly, per song: second listen adds 30 s–2 min, the voice track 1–3 min, and the "
                       "Max model takes 2–3× as long as Best. Recommended: second listen on. The voice track helps "
                       "most with loud, busy songs (it uses Spleeter by Deezer)."))
        self._w["ai_progress_style"] = ap = QComboBox()
        ap.addItem("Bar and %", "bar")
        ap.addItem("Only %", "percent")
        ap.addItem("Off (just \"AI listening\")", "off")
        ap.setCurrentIndex(max(0, ap.findData(self.s.ai_progress_style)))
        ap.currentIndexChanged.connect(lambda i: self._set("ai_progress_style", ap.itemData(i)))
        f.addRow("Progress in overlay", ap)
        self.ai_status = QLabel("")
        self.ai_status.setWordWrap(True)
        self.ai_status.setStyleSheet("color:#c9c9d6;")
        f.addRow(self.ai_status)
        self.ai_bar = QProgressBar()
        self.ai_bar.setRange(0, 100)
        self.ai_bar.setVisible(False)
        f.addRow(self.ai_bar)
        arow = QHBoxLayout()
        self.ai_get = QPushButton("Download model")
        self.ai_get.setObjectName("primary")
        self.ai_get.clicked.connect(lambda: self.ai_download_requested.emit(self.s.ai_model))
        self.ai_del = QPushButton("Uninstall model")
        self.ai_del.clicked.connect(self._ai_remove_or_cancel)
        arow.addWidget(self.ai_get)
        arow.addWidget(self.ai_del)
        arow.addStretch(1)
        aholder = QWidget()
        aholder.setLayout(arow)
        f.addRow(aholder)
        f.addRow(_hint("Tip: the AI only starts once a song has played (nearly) to the end, and runs at low "
                       "priority so games stay smooth. Tray menu → \"AI sync: listen to this song again\" "
                       "redoes a song. \"Only Spotify\" needs Windows 10 (version 2004 or newer) or Windows 11 – "
                       "otherwise it listens to the whole PC, where loud game sound can make it less accurate."))
        self._ai_busy = False

        sep = QLabel("<b>Translation</b>")
        sep.setObjectName("section")
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
        self._w["popup_style"] = ps = QComboBox()
        for key, label in (("app", "App style – sliding cards and a dark menu"),
                           ("windows", "Windows style – standard notifications and menu")):
            ps.addItem(label, key)
        ps.setCurrentIndex(max(0, ps.findData(self.s.popup_style)))
        ps.currentIndexChanged.connect(lambda i: self._set("popup_style", ps.itemData(i)))
        f.addRow("Pop-ups && menu", ps)

        sep = QLabel("<b>Updates</b>")
        sep.setObjectName("section")
        f.addRow(sep)
        self._w["auto_update_check"] = self._check("Check for new versions automatically", "auto_update_check")
        f.addRow(self._w["auto_update_check"])
        self.update_label = QLabel(f"You have version {APP_VERSION}.")
        self.update_label.setWordWrap(True)
        f.addRow(self.update_label)
        row = QHBoxLayout()
        check = QPushButton("Check for updates now")
        check.clicked.connect(self.check_updates_requested)
        self.update_btn = QPushButton("Update now")
        self.update_btn.setStyleSheet("background:#7c3aed; border-color:#a855f7;")
        self.update_btn.clicked.connect(self.update_requested)
        self.update_btn.setVisible(False)
        row.addWidget(check)
        row.addWidget(self.update_btn)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        f.addRow(holder)
        f.addRow(_hint("Updating downloads the new version from your GitHub releases page, checks the file, "
                       "installs it quietly and restarts the overlay. Your settings are kept."))

        sep = QLabel("<b>Settings backups</b>")
        sep.setObjectName("section")
        f.addRow(sep)
        self.backup_box = QComboBox()
        f.addRow("Backup", self.backup_box)
        brow = QHBoxLayout()
        make = QPushButton("Back up now")
        make.clicked.connect(self._backup_now)
        restore = QPushButton("Restore this backup")
        restore.clicked.connect(self._restore_backup)
        brow.addWidget(make)
        brow.addWidget(restore)
        brow.addStretch(1)
        bholder = QWidget()
        bholder.setLayout(brow)
        f.addRow(bholder)
        f.addRow(_hint("Your settings are backed up automatically once a day and every time you reset or restore "
                       "(the newest 20 are kept). Restoring brings back everything in Settings – look, layout, "
                       "shortcuts and so on. Synced songs, the AI model and your keys aren't touched."))
        self._fill_backups()
        return w

    def _fill_backups(self):
        from .history import when_text
        names = {"auto": "daily backup", "manual": "made by you", "before-reset": "before a reset",
                 "before-restore": "before a restore"}
        self.backup_box.clear()
        for b in Settings.list_backups():
            self.backup_box.addItem(f"{when_text(b['time'].isoformat())} – {names.get(b['reason'], b['reason'])}",
                                    str(b["path"]))
        if self.backup_box.count() == 0:
            self.backup_box.addItem("No backups yet", "")

    def _backup_now(self):
        if self.s.backup("manual"):
            self._fill_backups()
            self.backup_box.setCurrentIndex(0)

    def _restore_backup(self):
        path = self.backup_box.currentData()
        if not path:
            return
        ans = QMessageBox.question(
            self, "Restore settings",
            f"Restore your settings from “{self.backup_box.currentText()}”?\n\n"
            "Your current settings are backed up first, so you can switch back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._apply_backup(path, before="before-restore")

    def _apply_backup(self, path, before: str):
        from pathlib import Path
        self.s.backup(before)
        self.s.restore_from(Path(path))
        self.refresh()
        self.changed.emit()
        self.capture_started.emit()       # register the restored shortcuts
        self.capture_finished.emit()
        self._fill_backups()

    def _ai_tab(self):
        w, f = self._page()
        btn = QPushButton("Open the AI sync window")
        btn.setObjectName("primary")
        btn.clicked.connect(self.ai_window_requested)
        row = QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        f.addRow(holder)
        f.addRow(_hint("A window all about AI sync: what it's doing right now (listening / working, with "
                       "progress), what it still needs (model, Spotify, being able to hear Spotify), and for every "
                       "song it synced, which lyric lines it heard and which it missed."))
        f.addRow(_hint("The AI sync settings themselves (model, CPU use, what to listen to) are in "
                       "<b>Lyrics & translation</b>."))
        return w

    def _meanings_tab(self):
        w, f = self._page()
        open_btn = QPushButton("Open song meanings")
        open_btn.setObjectName("primary")
        open_btn.clicked.connect(self.meanings_requested)
        row = QHBoxLayout()
        row.addWidget(open_btn)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        f.addRow(holder)
        f.addRow(_hint("Opens a window where you can search any song (or use the one playing), click a lyric "
                       "line and read what it means. It only appears when you open it."))
        self._w["meanings_text_size"] = ts = QComboBox()
        ts.addItem("Normal", "normal")
        ts.addItem("Large", "large")
        ts.addItem("Extra large", "huge")
        ts.setCurrentIndex(max(0, ts.findData(self.s.meanings_text_size)))
        ts.currentIndexChanged.connect(lambda i: self._set("meanings_text_size", ts.itemData(i)))
        f.addRow("Meaning text size", ts)

        sep = QLabel("<b>Genius</b>")
        sep.setObjectName("section")
        f.addRow(sep)
        self._w["genius_token"] = gt = QLineEdit(self.s.genius_token)
        gt.setEchoMode(QLineEdit.EchoMode.Password)
        gt.setPlaceholderText("paste your Genius access token")
        gt.editingFinished.connect(lambda: self._set("genius_token", gt.text().strip()))
        f.addRow("Genius access token", gt)
        f.addRow(_hint("Free, takes about a minute: sign in at "
                       "<a style='color:#c9b8ff' href='https://genius.com/api-clients'>genius.com/api-clients</a>"
                       " → <b>New API Client</b> (any name, and any website such as https://example.com) → "
                       "<b>Save</b> → <b>Generate Access Token</b> → copy it here. It only lets the app read "
                       "public Genius explanations."))

        sep = QLabel("<b>AI backup (optional)</b>")
        sep.setObjectName("section")
        f.addRow(sep)
        self._w["meanings_ai"] = self._check("Explain lines Genius hasn't explained with AI", "meanings_ai")
        f.addRow(self._w["meanings_ai"])
        self._w["anthropic_key"] = ak = QLineEdit(self.s.anthropic_key)
        ak.setEchoMode(QLineEdit.EchoMode.Password)
        ak.setPlaceholderText("paste your Anthropic API key (starts with sk-ant-)")
        ak.editingFinished.connect(lambda: self._set("anthropic_key", ak.text().strip()))
        f.addRow("Anthropic API key", ak)
        f.addRow(_hint("Uses Claude through your own Anthropic account – get a key at "
                       "<a style='color:#c9b8ff' href='https://console.anthropic.com'>console.anthropic.com</a> "
                       "(it needs a little prepaid credit). Each explanation costs a fraction of a cent, only runs "
                       "when you click a line Genius hasn't explained, and is remembered so the same line is never "
                       "paid for twice. Answers are in the language picked under Translation."))
        return w

    def _synced_tab(self):
        w, f = self._page()
        top = QHBoxLayout()
        self.synced_filter = QComboBox()
        self.synced_filter.addItem("All synced songs", "all")
        self.synced_filter.addItem("Synced by me", "mine")
        self.synced_filter.addItem("Synced by AI", "ai")
        self.synced_filter.currentIndexChanged.connect(lambda _: self._fill_synced())
        self.synced_search = QLineEdit()
        self.synced_search.setPlaceholderText("Filter by song or artist…")
        self.synced_search.textChanged.connect(lambda _: self._fill_synced())
        top.addWidget(self.synced_filter)
        top.addWidget(self.synced_search, 1)
        self.synced_remove_all = QPushButton("Remove all")
        self.synced_remove_all.setObjectName("danger")
        self.synced_remove_all.clicked.connect(self._confirm_remove_all)
        top.addWidget(self.synced_remove_all)
        holder = QWidget()
        holder.setLayout(top)
        f.addRow(holder)
        self.synced_list = QListWidget()
        self.synced_list.setObjectName("synced")
        self.synced_list.setMinimumHeight(340)
        self.synced_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.synced_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        f.addRow(self.synced_list)
        self.synced_count = QLabel("")
        self.synced_count.setStyleSheet("color:#7f7f98;")
        f.addRow(self.synced_count)
        f.addRow(_hint("Removing a song deletes only its timing file – the song goes back to the normal lyrics "
                       "timing. AI sync will learn it again the next time it plays to the end (if AI sync is on); "
                       "songs you synced yourself can be synced again from the tray menu."))
        self._synced_entries = []
        self._synced_shown = []
        return w

    def set_synced_songs(self, entries: list):
        self._synced_entries = entries
        self._fill_synced()

    def _fill_synced(self):
        from .history import when_text
        want = self.synced_filter.currentData() or "all"
        q = self.synced_search.text().strip().lower()
        self.synced_list.clear()
        shown = 0
        self._synced_shown = []
        for e in sorted(self._synced_entries, key=lambda e: e.get("modified", ""), reverse=True):
            if want != "all" and e["kind"] != want:
                continue
            if q and q not in f"{e['title']} {e['artist']}".lower():
                continue
            shown += 1
            self._synced_shown.append(e)
            row = QWidget()
            row.setObjectName("syncRow")
            row.setStyleSheet("QWidget#syncRow { background: transparent; }")
            h = QHBoxLayout(row)
            h.setContentsMargins(8, 8, 8, 8)
            h.setSpacing(12)
            badge = QLabel("Synced by me" if e["kind"] == "mine" else "AI synced")
            badge.setObjectName("badgeMine" if e["kind"] == "mine" else "badgeAi")
            badge.setFixedWidth(96)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            texts = QVBoxLayout()
            texts.setSpacing(2)
            t = QLabel(e["title"] or "(untitled)")
            t.setObjectName("rowTitle")
            sub_bits = [b for b in (e.get("artist"), when_text(e["modified"]) if e.get("modified") else "",
                                    e.get("note")) if b]
            s_ = QLabel("  ·  ".join(sub_bits))
            s_.setObjectName("rowSub")
            texts.addWidget(t)
            texts.addWidget(s_)
            rm = QPushButton("Remove")
            rm.setObjectName("danger")
            rm.clicked.connect(lambda _=False, e=e: self._confirm_remove(e))
            h.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
            h.addLayout(texts, 1)
            h.addWidget(rm, 0, Qt.AlignmentFlag.AlignVCenter)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.synced_list.addItem(item)
            self.synced_list.setItemWidget(item, row)
        if not shown:
            self.synced_list.addItem(QListWidgetItem("No synced songs yet." if not self._synced_entries
                                                     else "No songs match."))
        mine = sum(1 for e in self._synced_entries if e["kind"] == "mine")
        ai = len(self._synced_entries) - mine
        self.synced_count.setText(f"{mine} synced by you · {ai} synced by AI")

    def _confirm_remove_all(self):
        items = list(self._synced_shown)
        if not items:
            return
        which = {"all": "synced songs", "mine": "songs you synced", "ai": "songs AI synced"}[
            self.synced_filter.currentData() or "all"]
        filtered = " that match your filter" if self.synced_search.text().strip() else ""
        ans = QMessageBox.question(
            self, "Remove all synced songs",
            f"Remove the timing for all {len(items)} {which}{filtered}?\n\n"
            "They go back to the normal lyrics timing. This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.synced_remove_all_requested.emit([(e["kind"], e["path"]) for e in items])

    def _confirm_remove(self, e: dict):
        who = "your own timing" if e["kind"] == "mine" else "the AI timing"
        ans = QMessageBox.question(
            self, "Remove synced song",
            f"Remove {who} for “{e['title']}”?\n\nThe song goes back to the normal lyrics timing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.synced_remove_requested.emit(e["kind"], e["path"])

    def _history_tab(self):
        w, f = self._page()
        self.history_list = QListWidget()
        self.history_list.setObjectName("history")
        self.history_list.setWordWrap(True)
        self.history_list.setMinimumHeight(320)
        self.history_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.history_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        f.addRow(self.history_list)
        row = QHBoxLayout()
        self.history_count = QLabel("")
        self.history_count.setStyleSheet("color:#7f7f98;")
        clear = QPushButton("Clear")
        clear.clicked.connect(self.history_clear_requested)
        row.addWidget(self.history_count)
        row.addStretch(1)
        row.addWidget(clear)
        holder = QWidget()
        holder.setLayout(row)
        f.addRow(holder)
        f.addRow(_hint("Only the important ones are kept here (the last 100) – things like a song finishing "
                       "AI sync, a model download, or timing you saved yourself."))
        return w

    def show_history(self, entries: list):
        from .history import when_text
        self.history_list.clear()
        marks = {"success": "✔", "warning": "⚠", "info": "•"}
        for e in entries:
            kind = e.get("kind", "info")
            item = QListWidgetItem(f"{marks.get(kind, '•')}  {when_text(e.get('time', ''))}\n{e.get('text', '')}")
            item.setForeground(QColor("#e6e6f0"))
            item.setData(Qt.ItemDataRole.UserRole, kind)
            item.setToolTip(e.get("text", ""))
            self.history_list.addItem(item)
        if not entries:
            self.history_list.addItem(QListWidgetItem("No notifications yet."))
        self.history_count.setText(f"{len(entries)} saved" if entries else "")

    def set_ai_status(self, text: str, busy: bool = False, installed: bool | None = None,
                      progress: int | None = None):
        self._ai_busy = busy
        self.ai_status.setText(text)
        self.ai_bar.setVisible(busy)
        if progress is not None:
            self.ai_bar.setValue(progress)
        if installed is not None:
            self.ai_get.setVisible(not installed and not busy)
            self.ai_del.setVisible(installed or busy)
        if busy:
            self.ai_get.setVisible(False)
            self.ai_del.setVisible(True)
        self.ai_del.setText("Cancel download" if busy else "Uninstall model")

    def _ai_remove_or_cancel(self):
        if self._ai_busy:
            self.ai_cancel_requested.emit()
            return
        from .ai_sync import MODELS
        _, label, size = MODELS[self.s.ai_model]
        ans = QMessageBox.question(
            self, "Uninstall AI model",
            f"Uninstall the {label} AI model? This frees {size}.\n\n"
            "To just pause AI sync, untick \"Use AI sync\" instead – then you won't need to download it again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.ai_remove_requested.emit(self.s.ai_model)

    def set_update_status(self, text: str, can_update: bool):
        self.update_label.setText(text)
        self.update_btn.setVisible(can_update)

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

    def _reset_clicked(self):
        if self._reset_state == "idle":
            self._reset_state = "armed"
            self.reset_btn.setText("Click again to reset")
            self.reset_btn.setStyleSheet("background:#7f1d1d; border:1px solid #ef4444; color:#ffffff;")
            self.reset_note.setText("⚠ This puts EVERY setting back to the default – design, colours, sizes, "
                                    "position, shortcuts, translation and AI sync on/off. Your synced songs, the AI "
                                    "model and your keys are kept, and a backup is saved first. Click the button "
                                    "again within 6 seconds to reset.")
            self.reset_note.setVisible(True)
            self._reset_timer.start(6000)
        elif self._reset_state == "armed":
            self._reset_timer.stop()
            self._reset_backup = self.s.backup("before-reset")
            self._reset()
            self._fill_backups()
            self._reset_state = "undo"
            self.reset_btn.setText("Undo reset")
            self.reset_btn.setStyleSheet("background:#7c3aed; border-color:#a855f7; color:#ffffff;")
            self.reset_note.setStyleSheet("color:#c4b5fd; font-size:12px;")
            self.reset_note.setText("Settings reset to the defaults. Changed your mind? Press Undo reset "
                                    "(or restore the “before a reset” backup in Behaviour → Settings backups).")
            self._reset_timer.start(30000)
        else:
            self._reset_timer.stop()
            if self._reset_backup:
                self._apply_backup(self._reset_backup, before="before-restore")
            self._reset_idle()

    def _reset_idle(self):
        self._reset_state = "idle"
        self.reset_btn.setText("Reset to defaults")
        self.reset_btn.setStyleSheet("")
        self.reset_note.setStyleSheet("color:#fca5a5; font-size:12px;")
        self.reset_note.setVisible(False)

    def _reset(self):
        fresh = Settings()
        fresh.first_run = False
        # keep the user's translation account details
        fresh.translate_email, fresh.deepl_key = self.s.translate_email, self.s.deepl_key
        fresh.genius_token, fresh.anthropic_key = self.s.genius_token, self.s.anthropic_key
        for k, v in vars(fresh).items():
            setattr(self.s, k, v)
        self.s.save()
        self.refresh()
        self.changed.emit()
        self.capture_started.emit()       # re-register the default shortcuts
        self.capture_finished.emit()
