import colorsys
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.palette import extract_palette, mix_hex  # noqa: E402


def hue(hex_col):
    r, g, b = (int(hex_col[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)


def test_orange_and_blue_cover():
    # 70% orange, 25% blue, 5% grey - like an orange cover with a blue logo
    px = [(240, 120, 20)] * 70 + [(30, 90, 230)] * 25 + [(128, 128, 128)] * 5
    pal = extract_palette(px)
    h1, s1, v1 = hue(pal["highlight"])
    h2, _, _ = hue(pal["highlight2"])
    assert 0.04 < h1 < 0.11                        # orange
    assert 0.55 < h2 < 0.68                        # blue
    assert s1 >= 0.55 and v1 >= 0.9                # bright & saturated for readability
    hb, sb, vb = hue(pal["background"])
    assert vb < 0.2 and abs(hb - h1) < 0.03        # dark tint of the main colour


def test_one_colour_cover_still_gives_a_gradient():
    pal = extract_palette([(200, 30, 60)] * 100)
    assert pal["highlight"] != pal["highlight2"]


def test_black_and_white_cover():
    px = [(0, 0, 0)] * 50 + [(255, 255, 255)] * 50 + [(120, 120, 120)] * 20
    pal = extract_palette(px)
    assert pal == {"highlight": "#E5E7EB", "highlight2": "#9CA3AF", "background": "#0B0B0F"}


def test_dull_cover_is_brightened():
    pal = extract_palette([(90, 70, 40)] * 100)    # muddy brown
    _, s, v = hue(pal["highlight"])
    assert s >= 0.55 and v >= 0.9


def test_empty():
    assert extract_palette([]) is None


def test_mix_hex():
    assert mix_hex("#000000", "#FFFFFF", 0.5) == "#808080"
    assert mix_hex("#A855F7", "#F97316", 0) == "#A855F7"
    assert mix_hex("#A855F7", "#F97316", 1) == "#F97316"
