"""The settings window. Every change is applied instantly and saved automatically."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QFontComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from . import APP_NAME, APP_VERSION
from .config import POSITIONS, Settings

STYLE = """
QDialog { background: #12121c; color: #e8e8f0; }
QGroupBox { border: 1px solid #2c2c40; border-radius: 8px; margin-top: 14px; padding: 10px 8px 6px 8px;
            font-weight: bold; color: #c9b8ff; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QLabel, QCheckBox { color: #e8e8f0; }
QComboBox, QSpinBox, QFontComboBox { background: #1c1c2b; color: #fff; border: 1px solid #34344c;
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

HOTKEY_HELP = (
    "<b>Ctrl+Alt+L</b> show / hide overlay &nbsp;·&nbsp; <b>Ctrl+Alt+K</b> click-through on/off<br>"
    "<b>Ctrl+Alt+O</b> open settings &nbsp;·&nbsp; <b>Ctrl+Alt+← / →</b> lyrics earlier / later"
)


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

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle(f"{APP_NAME} – Settings")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(480)
        self._building = True

        root = QVBoxLayout(self)

        # --- Text
        g = QGroupBox("Text")
        f = QFormLayout(g)
        self.font_box = QFontComboBox()
        self.font_box.setCurrentFont(QFont(settings.font_family))
        self.font_box.currentFontChanged.connect(lambda fnt: self._set("font_family", fnt.family()))
        f.addRow("Font", self.font_box)
        self.size_box = self._spin(10, 120, settings.font_size, "font_size", " px")
        f.addRow("Font size", self.size_box)
        self.bold_box = self._check("Bold", "bold")
        self.glow_box = self._check("Glow around highlighted words", "glow")
        f.addRow(self.bold_box)
        f.addRow(self.glow_box)
        root.addWidget(g)

        # --- Colours
        g = QGroupBox("Colours")
        f = QFormLayout(g)
        self.color_btns = {}
        for key, label in (("text_color", "Lyrics"), ("highlight_color", "Highlight"),
                           ("highlight_color2", "Highlight (gradient end)"),
                           ("background_color", "Background")):
            b = ColorButton(getattr(settings, key))
            b.changed.connect(lambda c, k=key: self._set(k, c))
            self.color_btns[key] = b
            f.addRow(label, b)
        self.bg_slider = self._slider(settings.background_opacity, "background_opacity", 0)
        f.addRow("Background opacity", self.bg_slider)
        self.win_slider = self._slider(settings.window_opacity, "window_opacity", 10)
        f.addRow("Overall opacity", self.win_slider)
        root.addWidget(g)

        # --- Position
        g = QGroupBox("Position && size")
        f = QFormLayout(g)
        self.pos_box = QComboBox()
        self.pos_box.addItems([p.replace("-", " ").title() for p in POSITIONS])
        self.pos_box.setCurrentIndex(POSITIONS.index(settings.position))
        self.pos_box.currentIndexChanged.connect(lambda i: self._set("position", POSITIONS[i]))
        f.addRow("Screen position", self.pos_box)
        self.screen_box = QComboBox()
        for i, sc in enumerate(QApplication.screens()):
            geo = sc.geometry()
            self.screen_box.addItem(f"Monitor {i + 1}  ({geo.width()}×{geo.height()})")
        self.screen_box.setCurrentIndex(max(0, min(settings.screen_index, self.screen_box.count() - 1)))
        self.screen_box.currentIndexChanged.connect(lambda i: self._set("screen_index", i))
        f.addRow("Monitor", self.screen_box)
        self.width_box = self._spin(300, 4000, settings.width, "width", " px", step=20)
        f.addRow("Width", self.width_box)
        self.margin_box = self._spin(0, 1000, settings.margin, "margin", " px", step=5)
        f.addRow("Distance from edge", self.margin_box)
        hint = QLabel("Tip: turn click-through off, then drag the overlay with the mouse "
                      "to place it anywhere.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9a9ab0; font-size:11px;")
        f.addRow(hint)
        root.addWidget(g)

        # --- Behaviour
        g = QGroupBox("Behaviour")
        f = QFormLayout(g)
        self.ct_box = self._check("Click-through (mouse passes through to the game)", "click_through")
        self.title_box = self._check("Show song title and artist", "show_title")
        self.lines_box = QComboBox()
        self.lines_box.addItems(LINE_CHOICES)
        self.lines_box.setCurrentIndex(settings.lines_shown - 1)
        self.lines_box.currentIndexChanged.connect(lambda i: self._set("lines_shown", i + 1))
        self.est_box = self._check("Estimate word timing when only line timing exists", "estimate_word_timing")
        self.pause_box = self._check("Hide overlay while music is paused", "hide_when_paused")
        self.spot_box = self._check("Only follow Spotify (ignore other media players)", "spotify_only")
        for w in (self.ct_box, self.title_box, self.est_box, self.pause_box, self.spot_box):
            f.addRow(w)
        f.addRow("Lines shown", self.lines_box)
        self.offset_box = self._spin(-10000, 10000, settings.sync_offset_ms, "sync_offset_ms", " ms", step=50)
        self.offset_box.setToolTip("Positive = lyrics appear earlier. Use if lyrics feel late.")
        f.addRow("Sync adjustment", self.offset_box)
        root.addWidget(g)

        keys = QLabel(HOTKEY_HELP)
        keys.setWordWrap(True)
        keys.setStyleSheet("color:#b8b8cc; font-size:11px; padding:4px;")
        root.addWidget(keys)

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

    # ------------------------------------------------------------ helpers
    def _set(self, key, value):
        if self._building:
            return
        setattr(self.s, key, value)
        self.s.clamp()
        self.s.save()
        self.changed.emit()

    def _spin(self, lo, hi, val, key, suffix="", step=1):
        b = QSpinBox()
        b.setRange(lo, hi)
        b.setSingleStep(step)
        b.setValue(int(val))
        b.setSuffix(suffix)
        b.valueChanged.connect(lambda v: self._set(key, int(v)))
        return b

    def _check(self, text, key):
        b = QCheckBox(text)
        b.setChecked(bool(getattr(self.s, key)))
        b.toggled.connect(lambda v: self._set(key, bool(v)))
        return b

    def _slider(self, val, key, minimum):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(minimum, 100)
        sl.setValue(int(round(val * 100)))
        lab = QLabel(f"{sl.value()}%")
        lab.setFixedWidth(40)
        sl.valueChanged.connect(lambda v: (lab.setText(f"{v}%"), self._set(key, v / 100.0)))
        lay.addWidget(sl)
        lay.addWidget(lab)
        w.slider = sl
        return w

    def sync_from_settings(self):
        """Refresh widgets after something changed elsewhere (hotkey, dragging)."""
        self._building = True
        try:
            self.ct_box.setChecked(self.s.click_through)
            self.offset_box.setValue(self.s.sync_offset_ms)
            self.pos_box.setCurrentIndex(POSITIONS.index(self.s.position))
        finally:
            self._building = False

    def _reset(self):
        fresh = Settings()
        fresh.first_run = False
        for k, v in vars(fresh).items():
            setattr(self.s, k, v)
        self.s.save()
        self._building = True
        try:
            self.font_box.setCurrentFont(QFont(self.s.font_family))
            self.size_box.setValue(self.s.font_size)
            self.bold_box.setChecked(self.s.bold)
            self.glow_box.setChecked(self.s.glow)
            for k, b in self.color_btns.items():
                b.set_color(getattr(self.s, k))
            self.bg_slider.slider.setValue(int(self.s.background_opacity * 100))
            self.win_slider.slider.setValue(int(self.s.window_opacity * 100))
            self.pos_box.setCurrentIndex(POSITIONS.index(self.s.position))
            self.screen_box.setCurrentIndex(0)
            self.width_box.setValue(self.s.width)
            self.margin_box.setValue(self.s.margin)
            for b, k in ((self.ct_box, "click_through"), (self.title_box, "show_title"),
                         (self.est_box, "estimate_word_timing"),
                         (self.pause_box, "hide_when_paused"), (self.spot_box, "spotify_only")):
                b.setChecked(getattr(self.s, k))
            self.offset_box.setValue(self.s.sync_offset_ms)
            self.lines_box.setCurrentIndex(self.s.lines_shown - 1)
        finally:
            self._building = False
        self.changed.emit()
