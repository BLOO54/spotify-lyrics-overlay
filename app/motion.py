"""Small maths for smooth movement (kept free of Qt so it can be tested anywhere)."""
from __future__ import annotations

import math


def sweep_target(knots, t: float) -> float:
    """Where the wipe should be along the line (pixels, in reading order) at time t.
    knots: [(time, position)] - each word's (start, left edge) and (end, right edge), in order.
    Between knots it moves at an even speed, so it flows through the space between words
    instead of stopping at the end of one word and jumping to the next."""
    if not knots:
        return 0.0
    if t <= knots[0][0]:
        return knots[0][1]
    for (t0, u0), (t1, u1) in zip(knots, knots[1:]):
        if t <= t1:
            return u0 if t1 <= t0 else u0 + (u1 - u0) * (t - t0) / (t1 - t0)
    return knots[-1][1]


def follow(shown: float, target: float, dt: float, tau: float = 0.06) -> float:
    """Move the shown position toward the target smoothly (no sudden speed changes)."""
    if dt <= 0:
        return shown
    return shown + (target - shown) * (1.0 - math.exp(-dt / tau))
