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

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QWidget

from . import win32
from .config import Settings
from .lrc import Line, Lyrics

LINE_ANIM_SECONDS = 0.28
SUNG_FADE = 0.35


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
        self.position = 0.0           # lyric time (includes sync adjustment)
        self.play_pos = 0.0           # real song position, for the timer
        self.duration = 0.0
        self.word_mode_label = ""

        self._art_bytes: bytes | None = None
        self._art_pix: QPixmap | None = None
        self._line_idx = -2
        self._anim_start = 0.0
        self._drag_offset = None
        self._hover = False
        self.apply_settings()

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
        return bool(self.s.translate_enabled and self.extras is not None and self.extras.translations)

    def _header_needed(self) -> bool:
        return self.s.show_title or self.s.time_display != "off"

    def apply_settings(self):
        main, ctx, head, extra = self.fonts()
        self._pad = max(10, int(self.s.font_size * 0.45))
        ctx_h = QFontMetricsF(ctx).height() * 1.25
        extra_h = QFontMetricsF(extra).height() * 1.2
        self._rows = []
        if self._header_needed():
            self._rows.append(("header", QFontMetricsF(head).height() * 1.6))
        if self.s.lines_shown >= 3:
            self._rows.append(("prev", ctx_h))
        self._rows.append(("main", QFontMetricsF(main).height() * 1.25))
        if self._show_romaji():
            self._rows.append(("romaji", extra_h))
        if self._show_translation():
            self._rows.append(("trans", extra_h))
        if self.s.lines_shown >= 2:
            self._rows.append(("next", ctx_h))
        self._ctx_h = ctx_h
        self._progress_h = 8 if self.s.show_progress else 0
        h = int(self._pad * 2 + sum(r[1] for r in self._rows) + self._progress_h)
        self.setFixedSize(self.s.width, h)
        self.setWindowOpacity(self.s.window_opacity)
        self._art_pix = None          # re-scale album art for the new size
        self.reposition()             # keeps the overlay anchored when its height changes
        self.apply_click_through()
        self.update()

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
        self._art_bytes = data
        self._art_pix = None
        self.update()

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
        self.set_extras(None)

    def set_extras(self, extras):
        had = (self._show_romaji(), self._show_translation())
        self.extras = extras
        if had != (self._show_romaji(), self._show_translation()):
            self.apply_settings()    # rows appear / disappear
        self.update()

    def set_status(self, status: str):
        self.status = status
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
        light_text = QColor(self.s.text_color).lightness() > 128
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

    def _fit_font(self, f: QFont, text: str, avail: float) -> QFont:
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text)
        if w <= avail or w == 0:
            return f
        nf = QFont(f)
        nf.setPixelSize(max(8, int(f.pixelSize() * max(0.5, avail / w))))
        return nf

    def _paint_simple_line(self, p, f: QFont, text: str, cy: float, color: QColor, area):
        if not text or color.alphaF() <= 0.01:
            return
        left, right = area
        avail = right - left - self._pad
        f = self._fit_font(f, text, avail)
        fm = QFontMetricsF(f)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail)
        x = (left + right - fm.horizontalAdvance(text)) / 2
        base = cy + (fm.ascent() - fm.descent()) / 2
        self._draw_text(p, self._text_path(f, x, base, text), color, 0.45 * color.alphaF())

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
        self._paint_background(p)

        left, right = float(self._pad), float(self.width() - self._pad)
        art_side = self.height() - self._pad * 2 - self._progress_h
        if self.s.show_album_art and self._art_bytes and art_side > 16:
            self._paint_art(p, left, self._pad, art_side)
            left += art_side + self._pad * 0.8
        area = (left, right)

        main_f, ctx_f, head_f, extra_f = self.fonts()
        y = float(self._pad)
        centers = {}
        for name, h in self._rows:
            centers[name] = (y, h)
            y += h
        if "header" in centers:
            self._paint_header(p, head_f, centers["header"][0], area)
        self._paint_lyrics(p, main_f, ctx_f, extra_f, centers, area)
        if self._progress_h:
            self._paint_progress(p, area)
        p.end()

    def _paint_background(self, p: QPainter):
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(18.0, r.height() / 4)
        if self.s.show_background:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(self.s.background_color, self.s.background_opacity))
            p.drawRoundedRect(r, radius, radius)
        draw_edge = (self.s.show_background and self.s.background_opacity > 0.02) or \
                    (self._hover and not self.s.click_through)
        if draw_edge:
            # thin neon edge; brighter while hovering (shows you can drag it)
            g = QLinearGradient(r.topLeft(), r.topRight())
            a = 0.55 if self._hover and not self.s.click_through else 0.28
            g.setColorAt(0, _qc(self.s.highlight_color, a))
            g.setColorAt(1, _qc(self.s.highlight_color2, a))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QBrush(g), 1.4))
            p.drawRoundedRect(r, radius, radius)

    def _paint_art(self, p: QPainter, x: float, y: float, side: float):
        size = int(side)
        if self._art_pix is None or self._art_pix.width() != size:
            pix = QPixmap()
            if not pix.loadFromData(self._art_bytes) or pix.isNull():
                return
            self._art_pix = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
        rect = QRectF(x, y, size, size)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 10, 10)
        p.save()
        p.setClipPath(clip)
        src = QRectF((self._art_pix.width() - size) / 2, (self._art_pix.height() - size) / 2, size, size)
        p.drawPixmap(rect, self._art_pix, src)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(_qc(self.s.highlight_color, 0.35)), 1.0))
        p.drawRoundedRect(rect, 10, 10)

    def _paint_header(self, p: QPainter, f: QFont, y: float, area):
        left, right = area
        fm = QFontMetricsF(f)
        x = left + self._pad * 0.4
        right -= self._pad * 0.4
        base = y + fm.ascent()
        p.setFont(f)
        # right side: sync mode + timer
        t = self.time_text()
        right_edge = right
        if t:
            tw = fm.horizontalAdvance(t)
            p.setPen(_qc(self.s.text_color, 0.85))
            p.drawText(QPointF(right_edge - tw, base), t)
            right_edge -= tw + 14
        label = self.word_mode_label
        if self.extras is not None and self.extras.label and self._show_translation():
            label = (label + " · " if label else "") + self.extras.label
        if self.s.show_title and label:
            lw = fm.horizontalAdvance(label)
            if right_edge - lw > x + 120:
                p.setPen(_qc(self.s.highlight_color, 0.75))
                p.drawText(QPointF(right_edge - lw, base), label)
                right_edge -= lw + 14
        if self.s.show_title:
            if self.title:
                icon = "♪ " if self.playing else "❚❚ "
                text = f"{icon}{self.title}  —  {self.artist}" if self.artist else f"{icon}{self.title}"
            else:
                text = "♪ Spotify Lyrics Overlay"
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(40.0, right_edge - x))
            p.setPen(_qc(self.s.text_color, 0.72))
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
        p.setBrush(_qc(self.s.text_color, 0.15))
        p.drawRoundedRect(QRectF(left, y, right - left, 3), 1.5, 1.5)
        g = QLinearGradient(left, 0, right, 0)
        g.setColorAt(0, _qc(self.s.highlight_color))
        g.setColorAt(1, _qc(self.s.highlight_color2))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(left, y, (right - left) * prog, 3), 1.5, 1.5)

    def _pulse(self) -> float:
        return 0.45 + 0.25 * abs(((time.monotonic() * 1.2) % 2) - 1)

    def _paint_lyrics(self, p: QPainter, main_f, ctx_f, extra_f, rows, area):
        def cy(name):
            top, h = rows[name]
            return top + h / 2

        ly = self.lyrics
        text_col = self.s.text_color
        if not ly or not ly.synced:
            msg = self.status or ("Instrumental ♪" if ly and ly.instrumental else "")
            self._paint_simple_line(p, ctx_f if len(msg) > 40 else main_f, msg, cy("main"),
                                    _qc(text_col, 0.7), area)
            return

        t = self.position
        idx = ly.line_index_at(t)
        if idx != self._line_idx:
            self._line_idx = idx
            self._anim_start = time.monotonic()
        a = _ease((time.monotonic() - self._anim_start) / LINE_ANIM_SECONDS)
        lines = ly.lines
        slide = (1 - a) * self._ctx_h * 0.8

        if "prev" in rows and idx - 1 >= 0:
            self._paint_simple_line(p, ctx_f, lines[idx - 1].text, cy("prev") + slide,
                                    _qc(text_col, 0.38 * a), area)
        if "next" in rows and idx + 1 < len(lines):
            self._paint_simple_line(p, ctx_f, lines[idx + 1].text or "♪", cy("next") + slide * 0.5,
                                    _qc(text_col, 0.5), area)

        if idx >= 0 and not lines[idx].is_break and self.extras is not None:
            if "romaji" in rows:
                self._paint_simple_line(p, extra_f, self.extras.romaji.get(idx, ""), cy("romaji") + slide,
                                        _qc(text_col, 0.72 * a), area)
            if "trans" in rows:
                tf = QFont(extra_f)
                tf.setItalic(True)
                col = _mix(_qc(text_col), _qc(self.s.highlight_color2), 0.35)
                self._paint_simple_line(p, tf, self.extras.translations.get(idx, ""), cy("trans") + slide,
                                        _with_alpha(col, 0.9 * a), area)

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
        self._paint_karaoke_line(p, main_f, line, t, cy("main") + slide, a, area)

    # ------------------------------------------------------------ karaoke line
    def _paint_karaoke_line(self, p: QPainter, f: QFont, line: Line, t: float, cy: float,
                            alpha: float, area):
        left, right = area
        avail = right - left - self._pad
        f = self._fit_font(f, line.text, avail)
        fm = QFontMetricsF(f)
        base = cy + (fm.ascent() - fm.descent()) / 2
        text_c = _qc(self.s.text_color, alpha)
        hi1 = _qc(self.s.highlight_color, alpha)
        hi2 = _qc(self.s.highlight_color2, alpha)
        oa = 0.55 * alpha
        style = self.s.highlight_style

        if not line.words:
            # ---- line-by-line mode: whole line lit, thin progress bar underneath
            text = fm.elidedText(line.text, Qt.TextElideMode.ElideRight, avail)
            w = fm.horizontalAdvance(text)
            x = (left + right - w) / 2
            self._draw_text(p, self._text_path(f, x, base, text), text_c, oa, glow=hi1)
            prog = _clamp01((t - line.start) / max(line.end - line.start, 0.1))
            bar_y = base + fm.descent() + 4
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(self.s.text_color, 0.15 * alpha))
            p.drawRoundedRect(QRectF(x, bar_y, w, 3), 1.5, 1.5)
            g = QLinearGradient(x, 0, x + w, 0)
            g.setColorAt(0, hi1)
            g.setColorAt(1, hi2)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(x, bar_y, w * prog, 3), 1.5, 1.5)
            return

        # ---- word-by-word karaoke
        space = fm.horizontalAdvance(" ")
        widths = [fm.horizontalAdvance(w.text) for w in line.words]
        total = sum(widths) + space * (len(widths) - 1)
        x = (left + right - total) / 2
        top = base - fm.ascent()
        height = fm.height()
        sung_target = _mix(text_c, hi1, 0.45) if self.s.keep_sung_colored else text_c

        for word, w in zip(line.words, widths):
            path = self._text_path(f, x, base, word.text)
            grad = QLinearGradient(x, 0, x + max(w, 1), 0)
            grad.setColorAt(0, hi1)
            grad.setColorAt(1, hi2)

            if t >= word.end:
                # already sung: fade from the highlight to the "sung" colour
                fade = _clamp01((t - word.end) / SUNG_FADE)
                c = _mix(hi1, sung_target, fade)
                self._draw_text(p, path, c, oa, glow=hi1 if fade < 1 else None, glow_strength=1 - fade)
            elif t >= word.start:
                dur = max(word.end - word.start, 0.01)
                prog = _clamp01((t - word.start) / dur)
                since = t - word.start
                self._paint_current_word(p, path, grad, style, x, w, base, top, height, fm,
                                         prog, since, text_c, hi1, oa)
            else:
                self._draw_text(p, path, _with_alpha(text_c, 0.9 * alpha), oa)
            x += w + space

    def _paint_current_word(self, p, path, grad, style, x, w, base, top, height, fm,
                            prog, since, text_c, hi1, oa):
        if style == "wipe":
            self._draw_text(p, path, text_c, oa, glow=hi1)
            p.save()
            p.setClipRect(QRectF(x - 2, top - 4, (w + 4) * prog, height + 8))
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
