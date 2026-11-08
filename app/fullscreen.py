"""Full-screen lyrics: the song playing now, big, over its blurred album art (like Spotify's
full-screen lyrics). Open it from the tray menu or Settings → Layout; Esc, F11 or a
double-click closes it. It's a normal window, so it never covers a game on its own."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from .lrc import split_tokens, word_state


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


class FullscreenLyrics(QWidget):
    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.ov = ctl.overlay
        self.s = ctl.s
        self.setWindowTitle("Lyrics – full screen")
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._scroll = None              # smoothly follows the current line number
        self._blur_key = None
        self._blur_pix = None
        self._art_key = None
        self._art_pix = None
        self._hint_until = 0.0
        self._clock = 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)      # even frame rhythm (see controller)
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ open / close
    def open_on(self, screen=None):
        if screen is not None:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())
        self._scroll = None
        self._hint_until = 3.5
        self._clock = 0.0
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.timer.start(16)

    def close_view(self):
        self.timer.stop()
        self.hide()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F11, Qt.Key.Key_Q):
            self.close_view()
        else:
            super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.close_view()

    def mouseMoveEvent(self, e):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hint_until = self._clock + 2.5
        QTimer.singleShot(2600, lambda: self.isVisible() and self.setCursor(Qt.CursorShape.BlankCursor))

    def closeEvent(self, e):
        self.timer.stop()
        super().closeEvent(e)

    def _tick(self):
        self._clock += 0.016
        self.update()

    # ------------------------------------------------------------------ pictures
    def _art(self):
        data = self.ov._art_bytes if self.s.show_album_art else None
        if data is None:
            return None, None
        side = int(min(self.width(), self.height()) * 0.14)
        key = (id(data), side, self.width(), self.height())
        if key != self._art_key:
            pix = QPixmap()
            if not pix.loadFromData(data) or pix.isNull():
                self._art_key, self._art_pix, self._blur_pix = key, None, None
                return None, None
            self._art_key = key
            self._art_pix = pix.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
            tiny = pix.scaled(12, 12, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation)
            mid = tiny.scaled(48, 48, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._blur_pix = mid.scaled(self.width(), self.height(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
        return self._art_pix, self._blur_pix

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        W, H = float(self.width()), float(self.height())
        art, blur = self._art()
        accent = QColor(self.ov._c("highlight_color"))
        if blur is not None:
            p.drawPixmap(int((W - blur.width()) / 2), int((H - blur.height()) / 2), blur)
        else:
            g = QLinearGradient(0, 0, W, H)
            g.setColorAt(0, accent.darker(320))
            g.setColorAt(1, QColor("#07070b"))
            p.fillRect(self.rect(), g)
        shade = QLinearGradient(0, 0, 0, H)
        shade.setColorAt(0, QColor(0, 0, 0, 150))
        shade.setColorAt(0.5, QColor(0, 0, 0, 120))
        shade.setColorAt(1, QColor(0, 0, 0, 190))
        p.fillRect(self.rect(), shade)

        margin = W * 0.07
        family = self.s.font_family or "Segoe UI"

        # ---- header: cover, title, artist
        top = H * 0.06
        x = margin
        if art is not None:
            side = float(art.width())
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, top, side, side), side * 0.08, side * 0.08)
            p.save()
            p.setClipPath(path)
            p.drawPixmap(int(x), int(top), art)
            p.restore()
            x += side + W * 0.02
            head_h = side
        else:
            head_h = H * 0.1
        tf = QFont(family)
        tf.setPixelSize(max(18, int(H * 0.042)))
        tf.setBold(True)
        af = QFont(family)
        af.setPixelSize(max(13, int(H * 0.026)))
        tfm, afm = QFontMetricsF(tf), QFontMetricsF(af)
        block = tfm.height() + afm.height() + 6
        ty = top + (head_h - block) / 2
        p.setFont(tf)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QPointF(x, ty + tfm.ascent()),
                   tfm.elidedText(self.ov.title or "Nothing playing", Qt.TextElideMode.ElideRight, W - x - margin))
        p.setFont(af)
        p.setPen(QColor(255, 255, 255, 170))
        p.drawText(QPointF(x, ty + tfm.height() + 6 + afm.ascent()),
                   afm.elidedText(self.ov.artist or "", Qt.TextElideMode.ElideRight, W - x - margin))

        # ---- lyrics
        lyr_top = top + head_h + H * 0.06
        lyr_bottom = H - H * 0.12
        self._paint_lyrics(p, margin, lyr_top, W - 2 * margin, lyr_bottom - lyr_top, family, accent)

        # ---- progress
        dur = self.ov.duration or 0.0
        if dur > 0:
            y = H - H * 0.065
            bar_w = W - 2 * margin
            prog = _clamp01(self.ov.play_pos / dur)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 50))
            p.drawRoundedRect(QRectF(margin, y, bar_w, 4), 2, 2)
            p.setBrush(QColor(255, 255, 255, 230))
            p.drawRoundedRect(QRectF(margin, y, bar_w * prog, 4), 2, 2)
            sf = QFont(family)
            sf.setPixelSize(max(11, int(H * 0.017)))
            p.setFont(sf)
            p.setPen(QColor(255, 255, 255, 160))
            fm = QFontMetricsF(sf)
            p.drawText(QPointF(margin, y + 8 + fm.ascent()), _fmt(self.ov.play_pos))
            rt = _fmt(dur)
            p.drawText(QPointF(margin + bar_w - fm.horizontalAdvance(rt), y + 8 + fm.ascent()), rt)

        # ---- hint
        if self._clock < self._hint_until:
            hf = QFont(family)
            hf.setPixelSize(max(11, int(H * 0.016)))
            p.setFont(hf)
            a = int(200 * _clamp01((self._hint_until - self._clock) / 0.6))
            p.setPen(QColor(255, 255, 255, a))
            text = "Esc, F11 or double-click to leave full screen"
            fm = QFontMetricsF(hf)
            p.drawText(QPointF(W - margin - fm.horizontalAdvance(text), top + fm.ascent() * 0.2), text)
        p.end()

    def _paint_lyrics(self, p: QPainter, x: float, top: float, width: float, height: float, family: str,
                      accent: QColor):
        ly = self.ov.lyrics
        if ly is None or not ly.lines:
            f = QFont(family)
            f.setPixelSize(max(18, int(self.height() * 0.04)))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(QRectF(x, top, width, height), Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                       self.ov.status or "Waiting for lyrics…")
            return
        t = self.ov.position
        idx = ly.line_index_at(t)
        target = float(max(idx, 0))
        if self._scroll is None or abs(self._scroll - target) > 6:
            self._scroll = target
        else:
            self._scroll += (target - self._scroll) * 0.12
        big = QFont(family)
        big.setPixelSize(max(24, int(self.height() * 0.062)))
        big.setBold(True)
        small = QFont(big)
        small.setPixelSize(max(14, int(self.height() * 0.026)))
        small.setBold(False)
        fm = QFontMetricsF(big)
        row_h = fm.height() * 1.18
        center_y = top + height * 0.38
        # translations (if on) under the current line
        trans = None
        if self.ov.extras is not None and getattr(self.ov.extras, "translations", None) and idx >= 0:
            trans = self.ov.extras.translations.get(idx)

        # lay out lines around the current one (each may wrap onto several rows)
        p.save()
        p.setClipRect(QRectF(x - 20, top, width + 40, height))
        first = max(0, int(self._scroll) - 4)
        last = min(len(ly.lines), int(self._scroll) + 8)
        # y of line `first`, measured from the smoothly scrolling position
        heights = {}
        def row_count(line):
            if line.words:
                lay = self._word_layout(line, fm, width)
                return (lay[-1][0] + 1) if lay else 1
            return len(self._wrap(line.text or "♪", fm, width))
        for i in range(first, last):
            heights[i] = row_count(ly.lines[i]) * row_h + row_h * 0.35 + (QFontMetricsF(small).height() + 6 if
                                                               (i == idx and trans) else 0)
        base_i = int(self._scroll)
        frac = self._scroll - base_i
        y = center_y - (heights.get(base_i, row_h) * frac)
        for i in range(base_i - 1, first - 1, -1):
            y -= heights[i]
        for i in range(first, last):
            line = ly.lines[i]
            dist = abs(i - self._scroll)
            alpha = 1.0 if i == idx else max(0.16, 0.5 - 0.1 * dist)
            rows = self._wrap(line.text or "♪", fm, width)
            p.setFont(big)
            if i == idx and not line.is_break:
                self._paint_current(p, line, t, rows, x, y, fm, row_h, accent)
            else:
                col = QColor(255, 255, 255, int(255 * alpha))
                p.setPen(col)
                for r, text in enumerate(rows):
                    p.drawText(QPointF(x, y + r * row_h + fm.ascent()), text)
            if i == idx and trans:
                p.setFont(small)
                p.setPen(QColor(255, 255, 255, 170))
                p.drawText(QPointF(x, y + row_count(line) * row_h + QFontMetricsF(small).ascent()), trans)
            y += heights[i]
        p.restore()

    @staticmethod
    def _wrap(text: str, fm: QFontMetricsF, width: float) -> list[str]:
        rows, cur = [], ""
        for piece, gap in split_tokens(text):
            cand = cur + piece
            if cur and fm.horizontalAdvance(cand) > width:
                rows.append(cur.rstrip())
                cur = piece + (" " if gap else "")
            else:
                cur = cand + (" " if gap else "")
        if cur.strip() or not rows:
            rows.append(cur.rstrip())
        return rows

    @staticmethod
    def _word_layout(line, fm: QFontMetricsF, width: float):
        """(row, x offset) for every word of a line, wrapping at `width`."""
        out, row, cx = [], 0, 0.0
        for w in line.words:
            wid = fm.horizontalAdvance(w.text)
            if cx > 0 and cx + wid > width:
                row, cx = row + 1, 0.0
            out.append((row, cx))
            cx += wid + (fm.horizontalAdvance(" ") if w.gap else 0.0)
        return out

    def _paint_current(self, p, line, t, rows, x, y, fm, row_h, accent):
        """The line being sung: sung words bright white, the rest dimmed, with a soft wipe."""
        dim = QColor(255, 255, 255, 95)
        bright = QColor(255, 255, 255)
        if not line.words:
            started = t >= line.start
            for r, text in enumerate(rows):
                p.setPen(bright if started else dim)
                p.drawText(QPointF(x, y + r * row_h + fm.ascent()), text)
            return
        ws = word_state(line, t)
        width = self.width() - 2 * x
        for k, (w, (r, ox)) in enumerate(zip(line.words, self._word_layout(line, fm, width))):
            cx, yy = x + ox, y + r * row_h + fm.ascent()
            if k < ws.index:
                p.setPen(bright)
                p.drawText(QPointF(cx, yy), w.text)
                continue
            p.setPen(dim)
            p.drawText(QPointF(cx, yy), w.text)
            if k == ws.index:
                wid = fm.horizontalAdvance(w.text)
                p.save()
                p.setClipRect(QRectF(cx, yy - fm.ascent(), wid * _clamp01(ws.progress), fm.height()))
                p.setPen(bright)
                p.drawText(QPointF(cx, yy), w.text)
                p.restore()


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"
