"""The floating, transparent lyrics window.

Plain English: this is a frameless window with a see-through background that
stays above other windows. We draw everything ourselves (panel, album art,
title, timer, lyrics, translation, karaoke highlight) about 60 times per
second so the highlight moves smoothly.

Layout, top to bottom (rows that are switched off take no space):
    header      ♪ Title — Artist                    word sync · -2:13
    previous    (dim)
    CURRENT     (big, highlighted)
    romaji      (Japanese pronunciation)
    translation (italic)
    next        (dim)
    progress bar
Album art, when on, sits on the left and the rest moves right.
"""

from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QWidget

from . import win32
from .config import Settings
from .lrc import Line, Lyrics, split_tokens, timed_copy, wrap_rows
from .palette import extract_palette, mix_hex

log = logging.getLogger(__name__)

LINE_ANIM_SECONDS = 0.28
COLOUR_FADE_SECONDS = 0.45     # how quickly colours glide to a new song's palette
COLOUR_KEYS = ("text_color", "highlight_color", "highlight_color2", "background_color")
SUNG_FADE = 0.35
MAX_MAIN_ROWS = 3      # a long current line may wrap onto up to 3 rows
MAX_SIDE_ROWS = 2      # previous / next / romaji / translation: up to 2 rows


def _qc(hex_str: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_str)
    if not c.isValid():
        c = QColor("#FFFFFF")
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t,
                           a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t,
                           a.alphaF() + (b.alphaF() - a.alphaF()) * t)


def _with_alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    return f"{seconds // 60}:{seconds % 60:02d}"


class OverlayWindow(QWidget):
    moved = Signal(int, int)                 # user dragged the overlay
    context_menu_requested = Signal(object)  # QPoint (global)
    settings_requested = Signal()

    def __init__(self, settings: Settings):
        super().__init__(None)
        self.s = settings
        self.setWindowTitle("Spotify Lyrics Overlay")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # What to show (set by the controller)
        self.title = ""
        self.artist = ""
        self.playing = False
        self.status = "Waiting for Spotify…"
        self.lyrics: Lyrics | None = None
        self.extras = None            # translate.Extras
        self.translation_pending = False   # show "translating…" while we wait
        self.position = 0.0           # lyric time (includes sync adjustment)
        self.play_pos = 0.0           # real song position, for the timer
        self.duration = 0.0
        self.word_mode_label = ""

        self._art_bytes: bytes | None = None
        self._art_pix: QPixmap | None = None
        self._palette: dict | None = None      # colours picked from the album art
        self._cur_cols: dict[str, str] = {}     # colours on screen right now (they fade to the targets)
        self._col_time = time.monotonic()
        self._frosted_active = False
        self._line_idx = -2
        self._anim_start = 0.0
        self._drag_offset = None
        self._hover = False
        self.apply_settings()

    # ------------------------------------------------------------ colours
    def target_colours(self) -> dict:
        """The colours we're heading for: from Settings, or from the album art if matching is on."""
        t = {k: getattr(self.s, k) for k in COLOUR_KEYS}
        pal = self._palette
        if pal:
            if self.s.match_album_highlight:
                t["highlight_color"], t["highlight_color2"] = pal["highlight"], pal["highlight2"]
            if self.s.match_album_background:
                t["background_color"] = pal["background"]
        return t

    def step_colours(self, now: float | None = None):
        """Move the on-screen colours a little closer to their targets (called every frame)."""
        now = time.monotonic() if now is None else now
        dt = max(0.0, now - self._col_time)
        self._col_time = now
        target = self.target_colours()
        f = 1.0 if not self.s.smooth_colour_changes else (1.0 - math.exp(-dt / COLOUR_FADE_SECONDS) if dt else 0.0)
        for k, goal in target.items():
            cur = self._cur_cols.get(k)
            self._cur_cols[k] = goal if (cur is None or f >= 0.999) else mix_hex(cur, goal, f)

    def _c(self, key: str) -> str:
        return self._cur_cols.get(key) or getattr(self.s, key)

    # ------------------------------------------------------------ settings / layout
    def fonts(self):
        main = QFont(self.s.font_family, -1)
        main.setPixelSize(self.s.font_size)
        main.setBold(self.s.bold)
        ctx = QFont(main)
        ctx.setPixelSize(max(8, int(self.s.font_size * 0.62)))
        ctx.setBold(False)
        head = QFont(self.s.font_family, -1)
        head.setPixelSize(max(9, int(self.s.font_size * 0.45)))
        head.setBold(True)
        extra = QFont(ctx)
        extra.setPixelSize(max(8, int(self.s.font_size * 0.55)))
        return main, ctx, head, extra

    def _show_romaji(self) -> bool:
        return bool(self.s.show_romaji and self.extras is not None and self.extras.romaji)

    def _show_translation(self) -> bool:
        if not self.s.translate_enabled:
            return False
        return bool(self.translation_pending or (self.extras is not None and self.extras.translations))

    def _swap(self) -> bool:
        """True when the translation should be the big karaoke line."""
        return bool(self.s.translate_enabled and self.s.translation_layout == "big"
                    and self.extras is not None and self.extras.translations)

    def _main_text(self, i: int) -> str:
        if self._swap():
            return self.extras.translations.get(i, self.lyrics.lines[i].text)
        return self.lyrics.lines[i].text

    def _under_text(self, i: int) -> str:
        """What goes in the small row under the big line: the translation, or (swapped) the original."""
        if self.extras is None:
            return ""
        if self._swap():
            return self.lyrics.lines[i].text if i in self.extras.translations else ""
        return self.extras.translations.get(i, "")

    def _main_line(self, i: int) -> Line:
        line = self.lyrics.lines[i]
        if not self._swap() or i not in self.extras.translations:
            return line
        key = (i, id(self.extras), self.s.estimate_word_timing, self.s.word_timing_mode)
        cache = self.__dict__.setdefault("_swap_cache", {})
        if key not in cache:
            copy = timed_copy(line, self.extras.translations[i], self.s.word_timing_mode)
            if not self.s.estimate_word_timing:
                copy.words = []
            if len(cache) > 400:
                cache.clear()
            cache[key] = copy
        return cache[key]

    def _header_needed(self) -> bool:
        return self.s.show_title or self.s.time_display != "off" or self._art_mode() == "header"

    def _art_mode(self):
        """Where the album art goes: None, "left" (big) or "header" (small, next to the title)."""
        if not self.s.show_album_art:
            return None
        pos = self.s.album_art_position
        if pos == "auto":
            pos = "left" if self.s.width >= 700 else "header"
        return pos

    def _left_art_side(self) -> int:
        return int(min(self.s.width * 0.22, self.s.font_size * 4.2))

    def lyric_area(self):
        """Horizontal space the lyrics may use (left, right)."""
        width = getattr(self, "_eff_width", self.s.width)
        left, right = float(self._pad), float(width - self._pad)
        if self._art_mode() == "left" and self._art_bytes:
            left += self._left_art_side() + self._pad * 0.8
        return left, right

    def _pieces(self, fm: QFontMetricsF, tokens):
        widths = [fm.horizontalAdvance(t) for t, _ in tokens]
        gaps = [g for _, g in tokens]
        return widths, gaps

    def _one_row(self) -> bool:
        """True when the user wants long lines kept on one row (wider overlay / smaller text)."""
        return self.s.long_lines in ("widen", "shrink")

    def _floor_ratio(self) -> float:
        return 0.5 if self._one_row() else 0.55

    def _fit_tokens(self, f: QFont, tokens, avail: float, max_rows: int):
        """Pick a font size so the text fits in max_rows rows; returns (font, rows, widths, gaps)."""
        size = f.pixelSize()
        floor = max(8, int(size * self._floor_ratio()))
        while True:
            fm = QFontMetricsF(f)
            widths, gaps = self._pieces(fm, tokens)
            rows = wrap_rows(widths, gaps, fm.horizontalAdvance(" "), avail)
            too_wide = widths and max(widths) > avail
            if (len(rows) <= max_rows and not too_wide) or f.pixelSize() <= floor:
                return f, rows, widths, gaps
            f = QFont(f)
            f.setPixelSize(max(floor, int(f.pixelSize() * 0.9)))

    def _rows_needed(self, f: QFont, texts, avail: float, cap: int) -> int:
        fm = QFontMetricsF(f)
        space = fm.horizontalAdvance(" ")
        best = 1
        for t in texts:
            tokens = split_tokens(t or "")
            if not tokens:
                continue
            widths, gaps = self._pieces(fm, tokens)
            best = max(best, min(cap, len(wrap_rows(widths, gaps, space, avail))))
            if best >= cap:
                break
        return best

    def apply_settings(self):
        main, ctx, head, extra = self.fonts()
        self._pad = max(10, int(self.s.font_size * 0.45))
        ly = self.lyrics
        if ly and ly.synced:
            lyric_texts = [self._main_text(i) for i in range(len(ly.lines))]
            under_texts = [self._under_text(i) for i in range(len(ly.lines))]
        else:
            lyric_texts, under_texts = [self.status], []

        # "Make the overlay wider": stretch to fit this song's longest line, but never past the screen
        self._eff_width = self.s.width
        if self.s.long_lines == "widen":
            left, right = self.lyric_area()
            overhead = self.s.width - (right - left - self._pad * 0.5)
            fm = QFontMetricsF(main)
            widest = max((fm.horizontalAdvance(t) for t in lyric_texts if t), default=0.0)
            extras_texts = []
            if self._show_translation() and self.extras is not None:
                extras_texts += under_texts
            if self._show_romaji():
                extras_texts += list(self.extras.romaji.values())
            if extras_texts:
                fx = QFontMetricsF(extra)
                widest = max(widest, max(fx.horizontalAdvance(t) for t in extras_texts))
            scr = self.target_screen()
            max_w = (scr.geometry().width() - 16) if scr is not None else self.s.width
            self._eff_width = int(max(self.s.width, min(widest + overhead + 2, max_w)))

        left, right = self.lyric_area()
        avail = right - left - self._pad * 0.5

        if self._one_row():
            # rows are only needed if a line won't fit even with the smallest allowed text
            def floor_font(f):
                g = QFont(f)
                g.setPixelSize(max(8, int(f.pixelSize() * self._floor_ratio())))
                return g
            main_rows = self._rows_needed(floor_font(main), lyric_texts, avail, MAX_MAIN_ROWS)
            ctx_rows = self._rows_needed(floor_font(ctx), lyric_texts, avail, MAX_SIDE_ROWS) if (ly and ly.synced) else 1
            extra_measure = floor_font(extra)
        else:
            main_rows = self._rows_needed(main, lyric_texts, avail, MAX_MAIN_ROWS)
            ctx_rows = self._rows_needed(ctx, lyric_texts, avail, MAX_SIDE_ROWS) if (ly and ly.synced) else 1
            extra_measure = extra

        def block(f, rows, first=1.25, more=1.05):
            return QFontMetricsF(f).height() * (first + more * (rows - 1))

        self._rows = []
        if self._header_needed():
            self._rows.append(("header", QFontMetricsF(head).height() * 1.6))
        if self.s.lines_shown >= 3:
            self._rows.append(("prev", block(ctx, ctx_rows)))
        self._rows.append(("main", block(main, main_rows)))
        if self._show_romaji():
            n = self._rows_needed(extra_measure, self.extras.romaji.values(), avail, MAX_SIDE_ROWS)
            self._rows.append(("romaji", block(extra, n, 1.2, 1.0)))
        if self._show_translation():
            n = self._rows_needed(extra_measure, under_texts, avail, MAX_SIDE_ROWS)
            self._rows.append(("trans", block(extra, n, 1.2, 1.0)))
        if self.s.lines_shown >= 2:
            self._rows.append(("next", block(ctx, ctx_rows)))
        self._row_caps = {"main": main_rows, "prev": ctx_rows, "next": ctx_rows,
                          "romaji": MAX_SIDE_ROWS, "trans": MAX_SIDE_ROWS}
        self._ctx_h = QFontMetricsF(ctx).height() * 1.25
        self._progress_h = 8 if self.s.show_progress else 0
        h = self._pad * 2 + sum(r[1] for r in self._rows) + self._progress_h
        if self._art_mode() == "left" and self._art_bytes:
            h = max(h, self._left_art_side() + self._pad * 2 + self._progress_h)
        self.setFixedSize(int(self._eff_width), int(h))
        self.setWindowOpacity(self.s.window_opacity)
        self._art_pix = None          # re-scale album art for the new size
        self.reposition()             # keeps the overlay anchored (and on-screen) when its size changes
        self.apply_click_through()
        self.apply_backdrop()
        self.update()

    def _radius(self) -> float:
        return float(max(0, min(self.s.corner_radius, self.height() / 2)))

    def apply_backdrop(self):
        """Frosted glass on/off (Windows only). Falls back to the normal background if refused."""
        want = self.s.background_style == "frosted" and self.s.show_background
        if not win32.IS_WINDOWS or not self.isVisible():
            self._frosted_active = False
            return
        hwnd = int(self.winId())
        was = self._frosted_active
        if want and not win32.transparency_effects_on():
            want = False             # Windows would draw a solid dark box instead of glass
        tint = self.target_colours()["background_color"]
        if want:
            self._frosted_active = win32.set_frosted(hwnd, True, tint, min(0.6, self.s.background_opacity * 0.5))
        else:
            self._frosted_active = False
        if not self._frosted_active and (was or want):
            win32.set_frosted(hwnd, False)       # make sure the blur is really off
        if self._frosted_active or was:
            win32.set_rounded_shape(hwnd, self.width(), self.height(),
                                    int(self._radius()) if self._frosted_active else 0)
        log.info("backdrop: wanted=%s frosted_active=%s", self.s.background_style == "frosted",
                 self._frosted_active)

    def target_screen(self):
        screens = QApplication.screens()
        if not screens:
            return None
        idx = self.s.screen_index if 0 <= self.s.screen_index < len(screens) else 0
        return screens[idx]

    def reposition(self):
        scr = self.target_screen()
        if scr is None:
            return
        g = scr.geometry()   # full screen, so it lines up over fullscreen games
        w, h, m = self.width(), self.height(), self.s.margin
        pos = self.s.position
        if pos == "custom":
            x, y = self.s.custom_x, self.s.custom_y
        else:
            if pos.endswith("left"):
                x = g.left() + m
            elif pos.endswith("right"):
                x = g.right() - w - m
            else:
                x = g.left() + (g.width() - w) // 2
            if pos.startswith("top"):
                y = g.top() + m
            elif pos.startswith("bottom"):
                y = g.bottom() - h - m
            else:
                y = g.top() + (g.height() - h) // 2
        # never let the overlay hang off the edge of the screen
        x = max(g.left(), min(int(x), g.left() + g.width() - w))
        y = max(g.top(), min(int(y), g.top() + g.height() - h))
        self.move(int(x), int(y))

    def apply_click_through(self):
        if win32.IS_WINDOWS:
            if self.isVisible():
                win32.set_overlay_styles(int(self.winId()), self.s.click_through)
        else:
            was_visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, self.s.click_through)
            if was_visible:
                self.show()

    def showEvent(self, e):
        super().showEvent(e)
        if win32.IS_WINDOWS:
            win32.set_overlay_styles(int(self.winId()), self.s.click_through)
            self.apply_backdrop()

    def keep_on_top(self):
        if self.isVisible():
            hwnd = int(self.winId())
            win32.ensure_styles(hwnd, self.s.click_through)   # games can't undo click-through
            win32.force_topmost(hwnd)

    # ------------------------------------------------------------ state
    def set_track(self, title: str, artist: str):
        self.title, self.artist = title, artist
        self._line_idx = -2

    def set_album_art(self, data: bytes | None):
        if data is self._art_bytes:
            return
        had = bool(self._art_bytes)
        self._art_bytes = data
        self._art_pix = None
        self._palette = self._palette_from(data) if data else None
        if self.s.match_album_background and self._frosted_active:
            self.apply_backdrop()          # new glass tint for the new song
        if had != bool(data) and self._art_mode() == "left":
            self.apply_settings()     # make / free room for the big album art
        self.update()

    @staticmethod
    def _palette_from(data: bytes):
        try:
            img = QImage.fromData(data)
            if img.isNull():
                return None
            small = img.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.FastTransformation)
            px = []
            for y in range(small.height()):
                for x in range(small.width()):
                    c = small.pixelColor(x, y)
                    px.append((c.red(), c.green(), c.blue()))
            return extract_palette(px)
        except Exception:
            return None

    def set_lyrics(self, lyrics: Lyrics | None, status: str = ""):
        self.lyrics = lyrics
        self.status = status
        self._line_idx = -2
        if lyrics and lyrics.synced:
            if lyrics.has_word_timing:
                self.word_mode_label = "word sync"
            elif self.s.estimate_word_timing:
                self.word_mode_label = "line sync · word estimate"
            else:
                self.word_mode_label = "line sync"
        else:
            self.word_mode_label = ""
        self.extras = None
        self.translation_pending = False
        self.apply_settings()         # the height depends on how long this song's lines are

    def set_translation_pending(self, pending: bool):
        if pending != self.translation_pending:
            self.translation_pending = pending
            self.apply_settings()

    def set_extras(self, extras):
        self.translation_pending = False
        self.extras = extras
        self.apply_settings()         # rows appear / disappear / grow

    def set_status(self, status: str):
        if status == self.status:
            return
        self.status = status
        if not (self.lyrics and self.lyrics.synced):
            self.apply_settings()     # a long message may need two rows
        self.update()

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.MouseButton.RightButton:
            self.context_menu_requested.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self.moved.emit(self.x(), self.y())

    def mouseDoubleClickEvent(self, e):
        self.settings_requested.emit()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    # ------------------------------------------------------------ painting helpers
    def _outline_color(self, alpha: float) -> QColor:
        light_text = QColor(self._c("text_color")).lightness() > 128
        c = QColor(0, 0, 0) if light_text else QColor(255, 255, 255)
        c.setAlphaF(_clamp01(alpha))
        return c

    def _outline_width(self) -> float:
        return 2.2 if self.s.show_background else 3.2   # thicker when there's no box behind

    def _text_path(self, f: QFont, x: float, base: float, text: str) -> QPainterPath:
        path = QPainterPath()
        path.addText(QPointF(x, base), f, text)
        return path

    def _draw_text(self, p: QPainter, path: QPainterPath, brush, outline_alpha=0.55,
                   glow: QColor | None = None, glow_strength: float = 1.0):
        if glow is not None and self.s.glow and glow_strength > 0.01:
            widths = ((16, 0.08), (10, 0.14), (5, 0.24)) if glow_strength > 1.0 else ((10, 0.10), (6, 0.18), (3, 0.30))
            g = min(glow_strength, 1.0)
            for width, a in widths:
                c = QColor(glow)
                c.setAlphaF(_clamp01(a * glow.alphaF() * g))
                p.strokePath(path, QPen(QBrush(c), width, Qt.PenStyle.SolidLine,
                                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.strokePath(path, QPen(QBrush(self._outline_color(outline_alpha)), self._outline_width(),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, brush)

    def _row_layout(self, rows, widths, gaps, space, left, right):
        """Yield (row_index, [(piece_index, x)]) with every row centred."""
        for r, row in enumerate(rows):
            row_w = sum(widths[i] for i in row) + sum(space for i in row[:-1] if gaps[i])
            x = (left + right - row_w) / 2
            placed = []
            for i in row:
                placed.append((i, x))
                x += widths[i] + (space if gaps[i] else 0.0)
            yield r, placed

    def _paint_simple_line(self, p, f: QFont, text: str, cy: float, color: QColor, area, max_rows: int = 1):
        """Draw plain (non-karaoke) text centred at cy, wrapping onto up to max_rows rows."""
        if not text or color.alphaF() <= 0.01:
            return
        left, right = area
        avail = right - left - self._pad * 0.5
        tokens = split_tokens(text)
        if not tokens:
            return
        f, rows, widths, gaps = self._fit_tokens(f, tokens, avail, 1 if self._one_row() else max_rows)
        fm = QFontMetricsF(f)
        if len(rows) > max_rows:              # still too long: cut the last row with "…"
            rows = rows[:max_rows - 1] + [[i for row in rows[max_rows - 1:] for i in row]]
        line_h = fm.height() * 1.05
        first_cy = cy - line_h * (len(rows) - 1) / 2
        for r, row in enumerate(rows):
            row_text = "".join(tokens[i][0] + (" " if gaps[i] and i != row[-1] else "") for i in row)
            row_text = fm.elidedText(row_text, Qt.TextElideMode.ElideRight, avail)
            x = (left + right - fm.horizontalAdvance(row_text)) / 2
            base = first_cy + r * line_h + (fm.ascent() - fm.descent()) / 2
            self._draw_text(p, self._text_path(f, x, base, row_text), color, 0.45 * color.alphaF())

    def time_text(self) -> str:
        if self.s.time_display == "off" or self.duration <= 0:
            return ""
        pos = max(0.0, min(self.play_pos, self.duration))
        if self.s.time_display == "left":
            return "-" + format_time(self.duration - pos)
        return f"{format_time(pos)} / {format_time(self.duration)}"

    # ------------------------------------------------------------ paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.step_colours()
        self._paint_background(p)

        if self._art_mode() == "left" and self._art_bytes:
            side = self._left_art_side()
            top = (self.height() - self._progress_h - side) / 2
            self._paint_art(p, float(self._pad), top, side, radius=min(10.0, self._radius() * 0.7))
        area = self.lyric_area()

        main_f, ctx_f, head_f, extra_f = self.fonts()
        y = float(self._pad)
        if self._art_mode() == "left" and self._art_bytes:
            # centre the text block vertically next to tall album art
            used = sum(h for _, h in self._rows)
            y = max(y, (self.height() - self._progress_h - used) / 2)
        centers = {}
        for name, h in self._rows:
            centers[name] = (y, h)
            y += h
        if "header" in centers:
            self._paint_header(p, head_f, centers["header"], area)
        self._paint_lyrics(p, main_f, ctx_f, extra_f, centers, area)
        if self._progress_h:
            self._paint_progress(p, area)
        p.end()

    def _paint_background(self, p: QPainter):
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(self._radius(), r.height() / 2)
        if self.s.show_background:
            p.setPen(Qt.PenStyle.NoPen)
            if self._frosted_active:
                # Windows blurs what's behind; we add a light tint and a glassy sheen on top
                p.setBrush(_qc(self._c("background_color"), self.s.background_opacity * 0.55))
                p.drawRoundedRect(r, radius, radius)
                sheen = QLinearGradient(r.topLeft(), r.bottomLeft())
                sheen.setColorAt(0.0, QColor(255, 255, 255, 34))
                sheen.setColorAt(0.45, QColor(255, 255, 255, 8))
                sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.setBrush(QBrush(sheen))
                p.drawRoundedRect(r, radius, radius)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QBrush(QColor(255, 255, 255, 40)), 1.0))
                p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            else:
                p.setBrush(_qc(self._c("background_color"), self.s.background_opacity))
                p.drawRoundedRect(r, radius, radius)
        draw_edge = (self.s.show_background and self.s.background_opacity > 0.02) or \
                    (self._hover and not self.s.click_through)
        if draw_edge:
            # thin neon edge; brighter while hovering (shows you can drag it)
            g = QLinearGradient(r.topLeft(), r.topRight())
            a = 0.55 if self._hover and not self.s.click_through else 0.28
            g.setColorAt(0, _qc(self._c("highlight_color"), a))
            g.setColorAt(1, _qc(self._c("highlight_color2"), a))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QBrush(g), 1.4))
            p.drawRoundedRect(r, radius, radius)

    def _paint_art(self, p: QPainter, x: float, y: float, side: float, radius: float = 10):
        size = int(side)
        if size < 8:
            return
        if self._art_pix is None or self._art_pix.width() < size:
            pix = QPixmap()
            if not pix.loadFromData(self._art_bytes) or pix.isNull():
                return
            big = max(size, self._left_art_side())
            self._art_pix = pix.scaled(big, big, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
        rect = QRectF(x, y, size, size)
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        p.save()
        p.setClipPath(clip)
        pw, ph = self._art_pix.width(), self._art_pix.height()
        side_src = min(pw, ph)
        src = QRectF((pw - side_src) / 2, (ph - side_src) / 2, side_src, side_src)
        p.drawPixmap(rect, self._art_pix, src)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(_qc(self._c("highlight_color"), 0.35)), 1.0))
        p.drawRoundedRect(rect, radius, radius)

    def _paint_header(self, p: QPainter, f: QFont, row, area):
        y, row_h = row
        left, right = area
        fm = QFontMetricsF(f)
        x = left + self._pad * 0.4
        right -= self._pad * 0.4
        if self._art_mode() == "header" and self._art_bytes:
            # small album art in front of the title
            side = min(row_h * 0.95, fm.height() * 1.45)
            self._paint_art(p, x, y + (row_h - side) / 2 - fm.height() * 0.1, side, radius=4)
            x += side + 8
        base = y + fm.ascent()
        p.setFont(f)
        t = self.time_text()
        right_edge = right
        if t:
            tw = fm.horizontalAdvance(t)
            p.setPen(_qc(self._c("text_color"), 0.85))
            p.drawText(QPointF(right_edge - tw, base), t)
            right_edge -= tw + 14
        label = self.word_mode_label
        if self.extras is not None and self.extras.label and self._show_translation():
            label = (label + " · " if label else "") + self.extras.label
        if self.s.show_title and label:
            lw = fm.horizontalAdvance(label)
            if right_edge - lw > x + 160:          # only if there's room left for the title
                p.setPen(_qc(self._c("highlight_color"), 0.75))
                p.drawText(QPointF(right_edge - lw, base), label)
                right_edge -= lw + 14
        if self.s.show_title:
            if self.title:
                icon = "♪ " if self.playing else "❚❚ "
                text = f"{icon}{self.title}  —  {self.artist}" if self.artist else f"{icon}{self.title}"
            else:
                text = "♪ Spotify Lyrics Overlay"
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(30.0, right_edge - x))
            p.setPen(_qc(self._c("text_color"), 0.72))
            p.drawText(QPointF(x, base), text)

    def _paint_progress(self, p: QPainter, area):
        if self.duration <= 0:
            return
        left, right = area
        left += self._pad * 0.4
        right -= self._pad * 0.4
        y = self.height() - self._pad * 0.6 - 3
        prog = _clamp01(self.play_pos / self.duration)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(self._c("text_color"), 0.15))
        p.drawRoundedRect(QRectF(left, y, right - left, 3), 1.5, 1.5)
        g = QLinearGradient(left, 0, right, 0)
        g.setColorAt(0, _qc(self._c("highlight_color")))
        g.setColorAt(1, _qc(self._c("highlight_color2")))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(left, y, (right - left) * prog, 3), 1.5, 1.5)

    def _pulse(self) -> float:
        return 0.45 + 0.25 * abs(((time.monotonic() * 1.2) % 2) - 1)

    def _paint_lyrics(self, p: QPainter, main_f, ctx_f, extra_f, rows, area):
        def cy(name):
            top, h = rows[name]
            return top + h / 2

        ly = self.lyrics
        text_col = self._c("text_color")
        if not ly or not ly.synced:
            msg = self.status or ("Instrumental ♪" if ly and ly.instrumental else "")
            self._paint_simple_line(p, main_f, msg, cy("main"), _qc(text_col, 0.7), area, MAX_MAIN_ROWS)
            return

        t = self.position
        idx = ly.line_index_at(t)
        if idx != self._line_idx:
            # forward by one line: the old line drifts up and fades (a jump/seek just cuts)
            self._fade_from = self._line_idx if idx == self._line_idx + 1 else None
            self._line_idx = idx
            self._anim_start = time.monotonic()
        a = _ease((time.monotonic() - self._anim_start) / LINE_ANIM_SECONDS)
        lines = ly.lines
        slide = (1 - a) * self._ctx_h * 0.8

        if "prev" in rows and idx - 1 >= 0:
            self._paint_simple_line(p, ctx_f, self._main_text(idx - 1), cy("prev") + slide,
                                    _qc(text_col, 0.38 * a), area, self._row_caps["prev"])
        if "next" in rows and idx + 1 < len(lines):
            self._paint_simple_line(p, ctx_f, self._main_text(idx + 1) or "♪", cy("next") + slide * 0.5,
                                    _qc(text_col, 0.5), area, self._row_caps["next"])

        if idx >= 0 and not lines[idx].is_break and "trans" in rows and self.extras is None \
                and self.translation_pending:
            tf = QFont(extra_f)
            tf.setItalic(True)
            self._paint_simple_line(p, tf, "translating…", cy("trans") + slide,
                                    _qc(text_col, 0.45 * a * self._pulse() / 0.7), area, 1)
        if idx >= 0 and not lines[idx].is_break and self.extras is not None:
            if "romaji" in rows:
                self._paint_simple_line(p, extra_f, self.extras.romaji.get(idx, ""), cy("romaji") + slide,
                                        _qc(text_col, 0.72 * a), area, MAX_SIDE_ROWS)
            if "trans" in rows:
                tf = QFont(extra_f)
                if self._swap():
                    col = _qc(text_col, 0.7)              # the original lyrics, small and calm
                else:
                    tf.setItalic(True)
                    col = _with_alpha(_mix(_qc(text_col), _qc(self._c("highlight_color2")), 0.35), 0.9)
                self._paint_simple_line(p, tf, self._under_text(idx), cy("trans") + slide,
                                        _with_alpha(col, col.alphaF() * a), area, MAX_SIDE_ROWS)

        old = getattr(self, "_fade_from", None)
        if a < 1.0 and old is not None and 0 <= old < len(lines) and not lines[old].is_break:
            sung = _mix(_qc(text_col), _qc(self._c("highlight_color")), 0.45 if self.s.keep_sung_colored else 0.0)
            self._paint_simple_line(p, main_f, self._main_text(old), cy("main") - a * self._ctx_h * 0.9,
                                    _with_alpha(sung, 0.85 * (1 - a)), area, self._row_caps["main"])

        if idx < 0:
            remaining = max(0.0, (lines[0].start if lines else 0) - t)
            dots = "♪  ♪  ♪" if remaining > 1.0 else "♪"
            self._paint_simple_line(p, main_f, dots, cy("main"), _qc(text_col, self._pulse()), area)
            return
        line = lines[idx]
        if line.is_break:
            self._paint_simple_line(p, main_f, "♪  ♪  ♪", cy("main") + slide,
                                    _qc(text_col, self._pulse() * a), area)
            return
        self._paint_karaoke_line(p, main_f, self._main_line(idx), t, cy("main") + slide, a, area)

    # ------------------------------------------------------------ karaoke line
    def _paint_karaoke_line(self, p: QPainter, f: QFont, line: Line, t: float, cy: float,
                            alpha: float, area):
        left, right = area
        avail = right - left - self._pad * 0.5
        text_c = _qc(self._c("text_color"), alpha)
        hi1 = _qc(self._c("highlight_color"), alpha)
        hi2 = _qc(self._c("highlight_color2"), alpha)
        oa = 0.55 * alpha
        style = self.s.highlight_style

        if line.words:
            tokens = [(w.text, w.gap) for w in line.words]
        else:
            tokens = split_tokens(line.text)
        if not tokens:
            return
        f, rows, widths, gaps = self._fit_tokens(f, tokens, avail, 1 if self._one_row() else MAX_MAIN_ROWS)
        fm = QFontMetricsF(f)
        space = fm.horizontalAdvance(" ")
        line_h = fm.height() * 1.05
        first_cy = cy - line_h * (len(rows) - 1) / 2
        layout = list(self._row_layout(rows, widths, gaps, space, left, right))

        if not line.words:
            # ---- line-by-line mode: whole line lit, thin progress bar under the last row
            for r, placed in layout:
                base = first_cy + r * line_h + (fm.ascent() - fm.descent()) / 2
                for i, x in placed:
                    self._draw_text(p, self._text_path(f, x, base, tokens[i][0]), text_c, oa, glow=hi1)
            last = layout[-1][1]
            x0 = min(x for _, x in layout[0][1] + last)
            x1 = max(x + widths[i] for r, placed in layout for i, x in placed)
            prog = _clamp01((t - line.start) / max(line.end - line.start, 0.1))
            bar_y = first_cy + (len(rows) - 1) * line_h + (fm.ascent() - fm.descent()) / 2 + fm.descent() + 4
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(self._c("text_color"), 0.15 * alpha))
            p.drawRoundedRect(QRectF(x0, bar_y, x1 - x0, 3), 1.5, 1.5)
            g = QLinearGradient(x0, 0, x1, 0)
            g.setColorAt(0, hi1)
            g.setColorAt(1, hi2)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(x0, bar_y, (x1 - x0) * prog, 3), 1.5, 1.5)
            return

        # ---- word-by-word karaoke, row by row
        sung_target = _mix(text_c, hi1, 0.45) if self.s.keep_sung_colored else text_c
        height = fm.height()
        for r, placed in layout:
            base = first_cy + r * line_h + (fm.ascent() - fm.descent()) / 2
            top = base - fm.ascent()
            for i, x in placed:
                word = line.words[i]
                w = widths[i]
                path = self._text_path(f, x, base, word.text)
                grad = QLinearGradient(x, 0, x + max(w, 1), 0)
                grad.setColorAt(0, hi1)
                grad.setColorAt(1, hi2)
                if t >= word.end:
                    fade = _clamp01((t - word.end) / SUNG_FADE)
                    c = _mix(hi1, sung_target, fade)
                    self._draw_text(p, path, c, oa, glow=hi1 if fade < 1 else None, glow_strength=1 - fade)
                elif t >= word.start:
                    dur = max(word.end - word.start, 0.01)
                    self._paint_current_word(p, path, grad, style, x, w, base, top, height, fm,
                                             _clamp01((t - word.start) / dur), t - word.start,
                                             text_c, hi1, oa)
                else:
                    self._draw_text(p, path, _with_alpha(text_c, 0.9 * alpha), oa)

    def _paint_current_word(self, p, path, grad, style, x, w, base, top, height, fm,
                            prog, since, text_c, hi1, oa):
        if style == "wipe":
            self._draw_text(p, path, text_c, oa, glow=hi1, glow_strength=0.4 + 0.6 * prog)
            # soft-edged sweep: solid colour up to the edge, then a short fade
            feather = max(6.0, min(22.0, w * 0.35))
            edge = x - feather + (w + feather * 2) * prog
            p.save()
            p.setClipRect(QRectF(x - 4, top - 4, max(0.0, edge - x + 4), height + 8))
            p.fillPath(path, QBrush(grad))
            p.restore()
            steps = 6
            for k in range(steps):
                x0 = edge + feather * k / steps
                p.save()
                p.setOpacity(1.0 - (k + 1) / (steps + 1))
                p.setClipRect(QRectF(x0, top - 4, feather / steps + 0.5, height + 8))
                p.fillPath(path, QBrush(grad))
                p.restore()
        elif style == "word":
            pop = _clamp01(since / 0.12)
            self._draw_text(p, path, _mix(text_c, hi1, pop), oa, glow=hi1, glow_strength=pop)
            p.save()
            p.setOpacity(pop)
            p.fillPath(path, QBrush(grad))
            p.restore()
        elif style == "glow":
            rise = _ease(since / 0.18)
            self._draw_text(p, path, QBrush(grad), oa, glow=hi1, glow_strength=1.0 + rise)
        elif style in ("grow", "bounce"):
            cx = x + w / 2
            p.save()
            if style == "grow":
                sc = 1.0 + 0.09 * _ease(since / 0.15)
                p.translate(cx, base)
                p.scale(sc, sc)
                p.translate(-cx, -base)
            else:
                hop = math.sin(math.pi * _clamp01(since / 0.32))
                p.translate(0, -height * 0.14 * hop)
            self._draw_text(p, path, QBrush(grad), oa, glow=hi1)
            p.restore()
        elif style == "underline":
            self._draw_text(p, path, QBrush(grad), oa, glow=hi1, glow_strength=0.6)
            bar_y = base + fm.descent() + 3
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_with_alpha(hi1, 0.25 * hi1.alphaF()))
            p.drawRoundedRect(QRectF(x, bar_y, w, 4), 2, 2)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x, bar_y, w * prog, 4), 2, 2)
        else:
            self._draw_text(p, path, QBrush(grad), oa, glow=hi1)
