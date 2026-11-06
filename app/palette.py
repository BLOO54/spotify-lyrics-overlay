"""Picking colours from album art.

Plain English: we shrink the album cover to a tiny image, sort its pixels into
colour "families" (reds, oranges, blues...) and pick:
  * the most striking family  -> highlight colour
  * a second, different family -> highlight gradient end
  * a very dark tint of the main colour -> background
Colours are then nudged to stay readable: highlights bright and saturated,
background dark, so white lyrics always stand out.
"""

from __future__ import annotations

import colorsys

HUE_BUCKETS = 18          # 20° each


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(c * 255))) for c in (r, g, b)))


def _from_hsv(h: float, s: float, v: float) -> str:
    return _hex(*colorsys.hsv_to_rgb(h % 1.0, s, v))


def _hue_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1 - d)


def extract_palette(pixels) -> dict | None:
    """pixels: iterable of (r, g, b) 0-255. Returns {"highlight", "highlight2", "background"} hex colours."""
    buckets = [[0.0, 0.0, 0.0, 0.0, 0] for _ in range(HUE_BUCKETS)]   # weight, sum_hx, sum_hy, sum_s, count
    all_v = []
    n = 0
    import math
    for r, g, b in pixels:
        n += 1
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        all_v.append(v)
        if s < 0.18 or v < 0.15:
            continue                      # greys, whites and near-black don't say much about colour
        weight = s * s * (0.35 + v)      # vivid, bright pixels count most
        bk = buckets[int(h * HUE_BUCKETS) % HUE_BUCKETS]
        bk[0] += weight
        bk[1] += math.cos(h * 2 * math.pi) * weight
        bk[2] += math.sin(h * 2 * math.pi) * weight
        bk[3] += s * weight
        bk[4] += 1
    if n == 0:
        return None

    def bucket_hue(bk):
        ang = math.atan2(bk[2], bk[1]) / (2 * math.pi)
        return ang % 1.0

    ranked = sorted((bk for bk in buckets if bk[4] >= max(2, n * 0.01)), key=lambda bk: -bk[0])
    if not ranked:
        # black & white cover: use a calm neutral palette
        return {"highlight": "#E5E7EB", "highlight2": "#9CA3AF", "background": "#0B0B0F"}

    h1 = bucket_hue(ranked[0])
    s1 = ranked[0][3] / ranked[0][0] if ranked[0][0] else 0.6
    second = next((bk for bk in ranked[1:] if _hue_dist(bucket_hue(bk), h1) >= 0.08), None)
    if second is not None and second[0] >= ranked[0][0] * 0.12:
        h2 = bucket_hue(second)
    else:
        h2 = h1 + 0.09                    # one-colour cover: a neighbouring hue keeps a gentle gradient

    hl1 = _from_hsv(h1, max(0.55, min(0.95, s1 + 0.15)), 0.97)
    hl2 = _from_hsv(h2, max(0.55, min(0.95, s1 + 0.1)), 0.95)
    bg = _from_hsv(h1, 0.45, 0.13)        # a deep tint of the main colour
    return {"highlight": hl1, "highlight2": hl2, "background": bg}


def mix_hex(a: str, b: str, t: float) -> str:
    """Blend two '#RRGGBB' colours; t=0 -> a, t=1 -> b."""
    t = max(0.0, min(1.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#{:02X}{:02X}{:02X}".format(*(round(x + (y - x) * t) for x, y in zip(ca, cb)))
