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

LINE_ANIM_SECONDS = 0.45     # how long lines take to glide to their new place
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


def _glide(t: float) -> float:
    """Gentle start and gentle stop (for lines moving): no sudden jump at the start like a plain ease-out."""
    t = max(0.0, min(1.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


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
        self.ai_note = ""          # "AI listening" / "AI syncing" while AI sync learns a song
        self.ai_progress = None    # 0..1 for the progress bar / %, None = no progress to show

        self._art_bytes: bytes | None = None
        self._art_pix: QPixmap | None = None
        self._palette: dict | None = None      # colours picked from the album art
        self._cur_cols: dict[str, str] = {}     # colours on screen right now (they fade to the targets)
        self._col_time = time.monotonic()
        self._frosted_active = False
        self._fit_main = 1.0
        self._fit_side = 1.0
        self._fit_all = 1.0
        self._dropped: set = set()
        self._line_idx = -2
        self._anim_start = 0.0
        self._fade_from = None
        self._drag_offset = None
        self._hover = False
        self.apply_settings()

    # ------------------------------------------------------------ designs
    DESIGNS = {
        "classic": "Classic – your colours, glowing highlight",
        "aurora": "Aurora – blurred album art, bright glowing lyrics",
        "hud": "HUD – sharp gaming interface with accent lines",
        "spotify": "Spotify-style – album-coloured card, left-aligned",
    }

    @property
    def design(self) -> str:
        return self.s.design if self.s.design in self.DESIGNS else "classic"

    def _align_left(self) -> bool:
        return self.design == "spotify"

    def _line_colours(self, alpha: float):
        """Colours for the karaoke line: (upcoming, highlight1, highlight2, sung, glow or None, outline factor)."""
        text = _qc(self._c("text_color"), alpha)
        hi1 = _qc(self._c("highlight_color"), alpha)
        hi2 = _qc(self._c("highlight_color2"), alpha)
        d = self.design
        if d == "aurora":       # brightness, not colour: dim words light up white, with a soft coloured glow
            white = QColor(255, 255, 255)
            white.setAlphaF(alpha)
            return _with_alpha(white, 0.42 * alpha), white, white, white, _with_alpha(hi1, 0.8 * alpha), 0.25
        if d == "spotify":      # dim upcoming words on the coloured card, sung words turn bright white
            white = QColor(255, 255, 255)
            white.setAlphaF(alpha)
            return _with_alpha(white, 0.48 * alpha), white, white, white, None, 0.35
        sung = _mix(text, hi1, 0.45) if self.s.keep_sung_colored else text
        return _with_alpha(text, 0.9 * alpha), hi1, hi2, sung, hi1, 1.0

    def _side_colour(self, which: str, alpha: float) -> QColor:
        """Colour for the previous / next lines in each design."""
        text = self._c("text_color")
        d = self.design
        if d == "spotify":
            return _qc("#FFFFFF", (0.42 if which == "prev" else 0.55) * alpha)
        if d == "aurora":
            return _qc("#FFFFFF", (0.30 if which == "prev" else 0.40) * alpha)
        return _qc(text, (0.38 if which == "prev" else 0.5) * alpha)

    def _design_fill(self) -> QColor:
        """Card colour for the Spotify-style design: a deep version of the album / highlight colour."""
        base = QColor(self._c("highlight_color"))
        h, s, v, _ = base.getHsvF()
        return QColor.fromHsvF(max(0.0, h), min(1.0, max(0.35, s * 0.8)), 0.42, self.s.background_opacity * 0.4 + 0.6)

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
        """Fonts for: big lyric line, other lines, title row, translation/romaji.

        _fit_side / _fit_main / _fit_all are below 1 only when "Keep overlay size fixed" is on
        and the text wouldn't otherwise fit in the box."""
        fm, fa = self._fit_main * self._fit_all, self._fit_side * self._fit_all
        main = QFont(self.s.font_family, -1)
        if self.design != "classic" and self.s.font_family == "Segoe UI":
            # Windows 11's newer display font where available - crisper at large sizes
            main.setFamilies(["Segoe UI Variable Display", "Segoe UI"])
        main.setPixelSize(max(8, int(self.s.font_size * fm)))
        main.setBold(self.s.bold)
        ctx = QFont(main)
        ctx.setPixelSize(max(8, int(self.s.side_font_size * fa)))
        ctx.setBold(False)
        head = QFont(main)
        head.setPixelSize(max(8, int(self.s.title_font_size * fa)))
        head.setBold(True)
        if self.design == "hud":
            head.setCapitalization(QFont.Capitalization.AllUppercase)
            head.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
        extra = QFont(ctx)
        extra.setPixelSize(max(8, int(self.s.side_font_size * 0.89 * fa)))
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
        if self.design == "spotify":
            return "header"
        pos = self.s.album_art_position
        if pos == "auto":
            pos = "left" if self.s.width >= 700 else "header"
        return pos

    HEADER_ART_SCALE = {"small": 1.93, "medium": 3.22, "large": 4.44}   # x title size

    def _header_art_side(self) -> float:
        side = self.s.title_font_size * self._fit_side * self._fit_all * self.HEADER_ART_SCALE.get(self.s.header_art_size, 3.22)
        return max(16.0, min(side, self.s.width * 0.2))

    def _left_art_side(self) -> int:
        side = min(self.s.width * 0.22, self.s.side_font_size * self._fit_side * self._fit_all * 6.8)
        if self.s.fixed_size and self.s.fixed_height:
            side = min(side, self.s.fixed_height - 2 * self._pad - (8 if self.s.show_progress else 0))
        return int(max(16, side))

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
        self._fit_main = self._fit_side = self._fit_all = 1.0
        self._dropped = set()
        h = self._layout()
        if self.s.fixed_size:
            if self.s.fixed_height <= 0:          # just switched on: freeze the current height
                self.s.fixed_height = int(h)
                self.s.save()
            target = self.s.fixed_height
            if h > target:
                # The lyrics come first. To make room in the fixed box:
                # 1) shrink the other lines and the title (down to 60%) ...
                self._fit_side = self._best_scale(lambda v: setattr(self, "_fit_side", v), 0.6, target)
                # 2) ... then, only if needed, the big lyric line (down to half) ...
                if self._layout() > target:
                    self._fit_main = self._best_scale(lambda v: setattr(self, "_fit_main", v), 0.5, target)
                # 3) ... and as a last resort everything together
                if self._layout() > target:
                    self._fit_all = self._best_scale(lambda v: setattr(self, "_fit_all", v), 0.3, target)
                # 4) a really small box: hide the least important rows so the lyric still fits
                for row in ("prev", "header", "next", "romaji", "trans"):
                    if self._layout() <= target:
                        break
                    self._dropped.add(row)
                self._layout()
            h = target
        self.setFixedSize(int(self._eff_width), int(h))
        self.setWindowOpacity(self.s.window_opacity)
        self._art_pix = None          # re-scale album art for the new size
        self.reposition()             # keeps the overlay anchored (and on-screen) when its size changes
        self.apply_click_through()
        self.apply_backdrop()
        self.update()

    def _best_scale(self, set_scale, lowest: float, target: float) -> float:
        """Largest scale in [lowest, 1] whose layout fits in `target` pixels (binary search)."""
        lo, hi = lowest, 1.0
        set_scale(lo)
        if self._layout() > target:
            return lo
        for _ in range(8):
            mid = (lo + hi) / 2
            set_scale(mid)
            if self._layout() <= target:
                lo = mid
            else:
                hi = mid
        set_scale(lo)
        return lo

    def _layout(self) -> float:
        """Work out the rows; returns the height the overlay would naturally need."""
        main, ctx, head, extra = self.fonts()
        self._pad = max(8, int(self.s.title_font_size * self._fit_side * self._fit_all))
        ly = self.lyrics
        if ly and ly.synced:
            lyric_texts = [self._main_text(i) for i in range(len(ly.lines))]
            under_texts = [self._under_text(i) for i in range(len(ly.lines))]
        else:
            lyric_texts, under_texts = [self.status], []

        # "Make the overlay wider": stretch to fit this song's longest line, but never past the screen
        self._eff_width = self.s.width
        if self.s.long_lines == "widen" and not self.s.fixed_size:
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
            head_h = QFontMetricsF(head).height() * (2.7 if self.design == "spotify" else 1.6)
            if self._art_mode() == "header" and self._art_bytes:
                head_h = max(head_h, self._header_art_side() + 8)    # room for the cover
            self._rows.append(("header", head_h))
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
        self._rows = [r for r in self._rows if r[0] not in self._dropped]
        self._row_caps = {"main": main_rows, "prev": ctx_rows, "next": ctx_rows,
                          "romaji": MAX_SIDE_ROWS, "trans": MAX_SIDE_ROWS}
        self._ctx_h = QFontMetricsF(ctx).height() * 1.25
        self._progress_h = 8 if self.s.show_progress else 0
        h = self._pad * 2 + sum(r[1] for r in self._rows) + self._progress_h
        if self._art_mode() == "left" and self._art_bytes:
            h = max(h, self._left_art_side() + self._pad * 2 + self._progress_h)
        return h

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
        if lyrics and lyrics.source == "my file" and self.word_mode_label:
            self.word_mode_label = "your timing · " + self.word_mode_label
        if lyrics and lyrics.source == "AI sync" and self.word_mode_label:
            self.word_mode_label = "AI timing · " + self.word_mode_label.replace("line sync · ", "")
        self.extras = None
        self.translation_pending = False
        self.apply_settings()         # the height depends on how long this song's lines are

    def set_ai_note(self, text: str, progress: float | None = None):
        if text != self.ai_note or progress != self.ai_progress:
            self.ai_note = text
            self.ai_progress = progress if text else None
            self.update()

    def ai_note_text(self) -> str:
        """'AI listening 42%' - the note with its percentage (if that's switched on)."""
        if not (self.ai_note and self.s.ai_sync_enabled):
            return ""
        if self.ai_progress is not None and self.s.ai_progress_style in ("bar", "percent"):
            return f"{self.ai_note} {int(round(_clamp01(self.ai_progress) * 100))}%"
        return self.ai_note

    def _paint_ai_bar(self, p: QPainter, x: float, width: float, y: float):
        """A thin progress bar above the AI note."""
        if self.ai_progress is None or self.s.ai_progress_style != "bar" or width < 10:
            return
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(self._c("text_color"), 0.18))
        p.drawRoundedRect(QRectF(x, y, width, 3), 1.5, 1.5)
        fill = _clamp01(self.ai_progress) * width
        if fill > 0.5:
            p.setBrush(_qc(self._c("highlight_color"), 0.95))
            p.drawRoundedRect(QRectF(x, y, fill, 3), 1.5, 1.5)
        p.restore()

    def header_label(self) -> str:
        """The small coloured text in the header: how the lyrics are timed (+ translation, AI)."""
        label = self.word_mode_label
        if not self.s.ai_sync_enabled:          # the AI indicator only shows while AI sync is on
            label = label.replace("AI timing · ", "")
        if self.extras is not None and self.extras.label and self._show_translation():
            label = (label + " · " if label else "") + self.extras.label
        note = self.ai_note_text()
        if note:
            label = note + (" · " + label if label else "")
        return label

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
        if glow is not None and self.s.glow and glow_strength > 0.01 and self.design != "spotify":
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

    def _row_x(self, left: float, right: float, width: float) -> float:
        if self._align_left():
            return left + self._pad * 0.4
        return (left + right - width) / 2

    def _row_layout(self, rows, widths, gaps, space, left, right):
        """Yield (row_index, [(piece_index, x)]) with every row centred (or left-aligned)."""
        for r, row in enumerate(rows):
            row_w = sum(widths[i] for i in row) + sum(space for i in row[:-1] if gaps[i])
            x = self._row_x(left, right, row_w)
            placed = []
            for i in row:
                placed.append((i, x))
                x += widths[i] + (space if gaps[i] else 0.0)
            yield r, placed

    def _outline_factor(self) -> float:
        return {"aurora": 0.25, "spotify": 0.35}.get(self.design, 1.0)

    def _paint_simple_line(self, p, f: QFont, text: str, cy: float, color: QColor, area, max_rows: int = 1,
                           soft: bool = False):
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
            x = self._row_x(left, right, fm.horizontalAdvance(row_text))
            base = first_cy + r * line_h + (fm.ascent() - fm.descent()) / 2
            path = self._text_path(f, x, base, row_text)
            if soft:
                # a gentle out-of-focus look for the lines around the current one (Aurora)
                for dx, dy in ((-0.9, 0), (0.9, 0), (0, -0.9), (0, 0.9)):
                    p.save()
                    p.translate(dx, dy)
                    p.fillPath(path, _with_alpha(color, color.alphaF() * 0.35))
                    p.restore()
                p.fillPath(path, _with_alpha(color, color.alphaF() * 0.6))
            else:
                self._draw_text(p, path, color, 0.45 * color.alphaF() * self._outline_factor())

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
        # centre the text block vertically (next to tall album art, or in a fixed-size box)
        used = sum(h for _, h in self._rows)
        y = max(float(self._pad), (self.height() - self._progress_h - used) / 2)
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
        d = self.design
        if d != "classic" and self.s.show_background:
            {"aurora": self._bg_aurora, "hud": self._bg_hud, "spotify": self._bg_spotify}[d](p)
            return
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

    # -------- design backgrounds
    def _blurred_art(self, w: int, h: int):
        """The album cover, heavily blurred to fill the card (cached; cheap 'shrink then enlarge' blur)."""
        key = (id(self._art_bytes), w, h)
        if getattr(self, "_blur_key", None) == key:
            return self._blur_pix
        pix = QPixmap()
        if not self._art_bytes or not pix.loadFromData(self._art_bytes) or pix.isNull():
            self._blur_key, self._blur_pix = key, None
            return None
        tiny = pix.scaled(10, 10, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
        mid = tiny.scaled(40, 40, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        big = mid.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        self._blur_key, self._blur_pix = key, big
        return big

    def _card_path(self, r: QRectF, radius: float) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        return path

    def _bg_aurora(self, p: QPainter):
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(max(self._radius(), 14.0), r.height() / 2)
        clip = self._card_path(r, radius)
        p.save()
        p.setClipPath(clip)
        art = self._blurred_art(int(r.width()), int(r.height()))
        if art is not None:
            p.setOpacity(min(1.0, 0.45 + self.s.background_opacity))
            p.drawPixmap(int(r.x() + (r.width() - art.width()) / 2), int(r.y() + (r.height() - art.height()) / 2), art)
            p.setOpacity(1.0)
        else:                       # no cover: a soft gradient of the highlight colours
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, _qc(self._c("highlight_color"), 0.55))
            g.setColorAt(1, _qc(self._c("highlight_color2"), 0.55))
            p.fillRect(r, QBrush(g))
        # darken for readability, lighter at the top like light falling on glass
        shade = QLinearGradient(r.topLeft(), r.bottomLeft())
        shade.setColorAt(0.0, QColor.fromRgbF(0, 0, 0, 0.30))
        shade.setColorAt(1.0, QColor.fromRgbF(0, 0, 0, 0.58))
        p.fillRect(r, QBrush(shade))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(QColor(255, 255, 255, 38)), 1.0))
        p.drawPath(self._card_path(r.adjusted(0.5, 0.5, -0.5, -0.5), radius))

    def _bg_hud(self, p: QPainter):
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        cut = min(14.0, r.height() / 5)
        # angled corners (top-left and bottom-right) like a game HUD panel
        shape = QPainterPath()
        shape.moveTo(r.left() + cut, r.top())
        shape.lineTo(r.right(), r.top())
        shape.lineTo(r.right(), r.bottom() - cut)
        shape.lineTo(r.right() - cut, r.bottom())
        shape.lineTo(r.left(), r.bottom())
        shape.lineTo(r.left(), r.top() + cut)
        shape.closeSubpath()
        p.fillPath(shape, _qc(self._c("background_color"), self.s.background_opacity))
        hi1, hi2 = _qc(self._c("highlight_color")), _qc(self._c("highlight_color2"))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(_with_alpha(hi1, 0.22)), 1.0))
        p.drawPath(shape)
        # bright accent strip along the top, fading out
        g = QLinearGradient(r.left(), 0, r.right(), 0)
        g.setColorAt(0.0, _with_alpha(hi1, 0.95))
        g.setColorAt(0.6, _with_alpha(hi2, 0.55))
        g.setColorAt(1.0, _with_alpha(hi2, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        top = QPainterPath()
        top.moveTo(r.left() + cut, r.top())
        top.lineTo(r.right() * 0.75, r.top())
        top.lineTo(r.right() * 0.75 - 2, r.top() + 2)
        top.lineTo(r.left() + cut - 2, r.top() + 2)
        top.closeSubpath()
        p.drawPath(top)
        # corner brackets
        pen = QPen(QBrush(_with_alpha(hi1, 0.85)), 2.0)
        p.setPen(pen)
        b = 10.0
        p.drawLine(QPointF(r.right() - b, r.top() + 1), QPointF(r.right() - 1, r.top() + 1))
        p.drawLine(QPointF(r.right() - 1, r.top() + 1), QPointF(r.right() - 1, r.top() + b))
        p.drawLine(QPointF(r.left() + 1, r.bottom() - b), QPointF(r.left() + 1, r.bottom() - 1))
        p.drawLine(QPointF(r.left() + 1, r.bottom() - 1), QPointF(r.left() + b, r.bottom() - 1))

    def _bg_spotify(self, p: QPainter):
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(12.0, r.height() / 2)
        fill = self._design_fill()
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, fill.lighter(112))
        g.setColorAt(1, fill.darker(115))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
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
        if self.design == "spotify":
            return self._header_spotify(p, f, row, area)
        y, row_h = row
        left, right = area
        fm = QFontMetricsF(f)
        x = left + self._pad * 0.4
        right -= self._pad * 0.4
        if self._art_mode() == "header" and self._art_bytes:
            # small album art in front of the title
            side = min(row_h - 4, self._header_art_side())
            self._paint_art(p, x, y + (row_h - side) / 2, side, radius=max(4.0, side * 0.12))
            x += side + 10
        base = y + (row_h - fm.height()) / 2 + fm.ascent()     # title centred beside the cover
        p.setFont(f)
        t = self.time_text()
        right_edge = right
        if t:
            tw = fm.horizontalAdvance(t)
            p.setPen(_qc(self._c("text_color"), 0.85))
            p.drawText(QPointF(right_edge - tw, base), t)
            right_edge -= tw + 14
        label = self.header_label()
        if self.s.show_title and label:
            lw = fm.horizontalAdvance(label)
            if right_edge - lw > x + 160:          # only if there's room left for the title
                p.setPen(_qc(self._c("highlight_color"), 0.75))
                p.drawText(QPointF(right_edge - lw, base), label)
                note = self.ai_note_text()
                if note and label.startswith(note):
                    self._paint_ai_bar(p, right_edge - lw, fm.horizontalAdvance(note), base - fm.ascent() - 4)
                right_edge -= lw + 14
        if self.s.show_title:
            d = self.design
            if d == "hud":
                tag = "▸ " if self.playing else "❚❚ "
                text = f"{tag}{self.title}  //  {self.artist}" if self.title else "▸ SPOTIFY LYRICS OVERLAY"
                text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(30.0, right_edge - x))
                p.setPen(_qc(self._c("highlight_color"), 0.9))
                p.drawText(QPointF(x, base), text)
                return
            if d == "aurora" and self.title:
                # bold title, softer artist
                title_t = fm.elidedText(self.title, Qt.TextElideMode.ElideRight, max(30.0, (right_edge - x) * 0.65))
                p.setPen(QColor(255, 255, 255, 235))
                p.drawText(QPointF(x, base), title_t)
                if self.artist:
                    af = QFont(f)
                    af.setBold(False)
                    p.setFont(af)
                    tw = fm.horizontalAdvance(title_t)
                    rest = QFontMetricsF(af).elidedText("  ·  " + self.artist, Qt.TextElideMode.ElideRight,
                                                         max(10.0, right_edge - x - tw))
                    p.setPen(QColor(255, 255, 255, 150))
                    p.drawText(QPointF(x + tw, base), rest)
                return
            if self.title:
                icon = "♪ " if self.playing else "❚❚ "
                text = f"{icon}{self.title}  —  {self.artist}" if self.artist else f"{icon}{self.title}"
            else:
                text = "♪ Spotify Lyrics Overlay"
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(30.0, right_edge - x))
            p.setPen(_qc(self._c("text_color"), 0.72))
            p.drawText(QPointF(x, base), text)

    def _header_spotify(self, p: QPainter, f: QFont, row, area):
        """Album art + title on top, artist underneath, time on the right - like Spotify's now-playing."""
        y, row_h = row
        left, right = area
        x = left + self._pad * 0.4
        right -= self._pad * 0.4
        if self._art_bytes and self.s.show_album_art:
            side = min(row_h - 6, self._header_art_side() * 1.15)
            self._paint_art(p, x, y + (row_h - side) / 2, side, radius=6)
            x += side + 12
        title_f = QFont(f)
        title_f.setPixelSize(int(f.pixelSize() * 1.15))
        sub_f = QFont(f)
        sub_f.setBold(False)
        tfm, sfm = QFontMetricsF(title_f), QFontMetricsF(sub_f)
        t = self.time_text()
        right_edge = right
        if t:
            p.setFont(sub_f)
            tw = sfm.horizontalAdvance(t)
            p.setPen(QColor(255, 255, 255, 190))
            p.drawText(QPointF(right_edge - tw, y + row_h / 2 + sfm.ascent() / 2 - 1), t)
            right_edge -= tw + 14
        if not self.s.show_title:
            return
        block = tfm.height() + sfm.height()
        top = y + (row_h - block) / 2
        p.setFont(title_f)
        p.setPen(QColor(255, 255, 255, 245))
        p.drawText(QPointF(x, top + tfm.ascent()),
                   tfm.elidedText(self.title or "Spotify Lyrics Overlay", Qt.TextElideMode.ElideRight, max(30.0, right_edge - x)))
        p.setFont(sub_f)
        p.setPen(QColor(255, 255, 255, 170))
        sub = self.artist or ""
        label = self.header_label()
        if label:
            sub = f"{sub}  ·  {label}" if sub else label
        sub_base = top + tfm.height() + sfm.ascent()
        p.drawText(QPointF(x, sub_base), sfm.elidedText(sub, Qt.TextElideMode.ElideRight, max(30.0, right_edge - x)))
        note = self.ai_note_text()
        if note and sfm.horizontalAdvance(sub) <= right_edge - x:
            start = x + sfm.horizontalAdvance(sub[:len(sub) - len(label)]) if label else x
            self._paint_ai_bar(p, start, sfm.horizontalAdvance(note), sub_base + sfm.descent() + 2)   # under it here

    def _paint_progress(self, p: QPainter, area):
        if self.duration <= 0:
            return
        left, right = area
        left += self._pad * 0.4
        right -= self._pad * 0.4
        y = self.height() - self._pad * 0.6 - 3
        prog = _clamp01(self.play_pos / self.duration)
        d = self.design
        p.setPen(Qt.PenStyle.NoPen)
        if d in ("aurora", "spotify"):
            track, fill = QColor(255, 255, 255, 50), QBrush(QColor(255, 255, 255, 230))
        else:
            track = _qc(self._c("text_color"), 0.15)
            g = QLinearGradient(left, 0, right, 0)
            g.setColorAt(0, _qc(self._c("highlight_color")))
            g.setColorAt(1, _qc(self._c("highlight_color2")))
            fill = QBrush(g)
        h = 2.0 if d == "hud" else 3.0
        rad = 0.0 if d == "hud" else 1.5
        p.setBrush(track)
        p.drawRoundedRect(QRectF(left, y, right - left, h), rad, rad)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(left, y, (right - left) * prog, h), rad, rad)
        if d == "spotify" and prog > 0:          # Spotify's little position dot
            p.drawEllipse(QPointF(left + (right - left) * prog, y + h / 2), 4.5, 4.5)

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
        a = _glide((time.monotonic() - self._anim_start) / LINE_ANIM_SECONDS)
        lines = ly.lines
        slide = (1 - a) * self._ctx_h * 0.8

        if "prev" in rows and idx - 1 >= 0:
            self._paint_simple_line(p, ctx_f, self._main_text(idx - 1), cy("prev") + slide,
                                    self._side_colour("prev", a), area, self._row_caps["prev"],
                                    soft=self.design == "aurora")
        if "next" in rows and idx + 1 < len(lines):
            # the new "next" line fades in as it slides up, instead of popping in
            self._paint_simple_line(p, ctx_f, self._main_text(idx + 1) or "♪", cy("next") + slide * 0.5,
                                    self._side_colour("next", a if self._fade_from is not None else 1.0),
                                    area, self._row_caps["next"],
                                    soft=self.design == "aurora")

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
            sung = self._line_colours(1.0)[3]
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
        upcoming, hi1, hi2, sung_target, glow_c, ofac = self._line_colours(alpha)
        text_c = upcoming
        oa = 0.55 * alpha * ofac
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
                    self._draw_text(p, self._text_path(f, x, base, tokens[i][0]), sung_target, oa, glow=glow_c)
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
                    self._draw_text(p, path, c, oa, glow=glow_c if fade < 1 else None, glow_strength=1 - fade)
                elif t >= word.start:
                    dur = max(word.end - word.start, 0.01)
                    self._paint_current_word(p, path, grad, style, x, w, base, top, height, fm,
                                             _clamp01((t - word.start) / dur), t - word.start,
                                             text_c, glow_c if glow_c is not None else hi1, oa)
                else:
                    self._draw_text(p, path, text_c, oa)

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
