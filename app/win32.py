"""Small Windows helpers using the standard Win32 API via ctypes.

Plain English:
* "Click-through" means mouse clicks pass straight through the overlay to the
  game underneath. Windows does this for windows marked WS_EX_TRANSPARENT.
* "Always on top" is kept by re-asserting HWND_TOPMOST every couple of seconds,
  because some games push themselves to the front.
* "Global hotkeys" (like Ctrl+Alt+L) work even while a game has focus. They are
  registered with RegisterHotKey, the normal Windows way.

None of this touches the game or Spotify. It only changes our own window.
On non-Windows systems every function here does nothing, so the rest of
the app can still be tested.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_APPWINDOW = 0x00040000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

VK = {
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
}

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # 64-bit safe signatures
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _GetWindowLong = user32.GetWindowLongPtrW
        _SetWindowLong = user32.SetWindowLongPtrW
        _LONG = ctypes.c_longlong
    else:
        _GetWindowLong = user32.GetWindowLongW
        _SetWindowLong = user32.SetWindowLongW
        _LONG = ctypes.c_long
    _GetWindowLong.restype = _LONG
    _GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
    _SetWindowLong.restype = _LONG
    _SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, _LONG]

    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL


def desired_exstyle(current: int, click_through: bool) -> int:
    """Work out the extended style we want, starting from what Qt already set.

    WS_EX_LAYERED is only *added* for click-through (Windows needs LAYERED +
    TRANSPARENT for clicks to pass through). Qt already makes translucent
    windows layered, so we never strip it either.
    """
    style = current | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    style &= ~WS_EX_APPWINDOW
    if click_through:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    return style


def ensure_styles(hwnd: int, click_through: bool) -> bool:
    """Re-apply our styles only if something changed them. Returns True if changed."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        cur = _GetWindowLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        want = desired_exstyle(cur, click_through)
        if want == cur:
            return False
        _SetWindowLong(hwnd, GWL_EXSTYLE, want)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        log.exception("ensure_styles failed")
        return False


def set_overlay_styles(hwnd: int, click_through: bool) -> bool:
    """Apply our extended window styles and push the window to the top."""
    if not IS_WINDOWS or not hwnd:
        return False
    ensure_styles(hwnd, click_through)
    force_topmost(hwnd)
    return True


def force_topmost(hwnd: int) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
    except Exception:
        log.exception("force_topmost failed")


def register_hotkey(hotkey_id: int, mods: int, vk: int) -> bool:
    """Register a system-wide hotkey. Messages arrive as WM_HOTKEY on this thread."""
    if not IS_WINDOWS:
        return False
    ok = bool(user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk))
    if not ok:
        log.warning("RegisterHotKey(%s) failed (already used by another app?)", hotkey_id)
    return ok


def unregister_hotkey(hotkey_id: int) -> None:
    if IS_WINDOWS:
        user32.UnregisterHotKey(None, hotkey_id)


def parse_msg(message_ptr: int):
    """Turn the pointer Qt gives us into (message, wParam)."""
    if not IS_WINDOWS:
        return None, None
    msg = wintypes.MSG.from_address(int(message_ptr))
    return msg.message, msg.wParam


# --------------------------------------------------------------------------
# Frosted glass (blur what's behind the overlay) and rounded corners.
#
# Windows 10 (1803+) and 11 can blur whatever is behind a window - it's the same
# "acrylic" effect the Start menu uses. We switch it on with the long-standing
# SetWindowCompositionAttribute call. The blur is clipped to a rounded shape so
# it matches the overlay's corners. If Windows refuses (older version, or
# "Transparency effects" turned off in Settings), the overlay simply keeps its
# normal see-through background.

ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
WCA_ACCENT_POLICY = 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2


def acrylic_tint(hex_color: str, alpha: float) -> int:
    """'#RRGGBB' + alpha -> the 0xAABBGGRR number Windows expects for the glass tint."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    a = max(0, min(255, int(round(alpha * 255))))
    return (a << 24) | (b << 16) | (g << 8) | r


if IS_WINDOWS:
    class _ACCENTPOLICY(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class _WINCOMPATTRDATA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]

    try:
        _SetWCA = user32.SetWindowCompositionAttribute
        _SetWCA.argtypes = [wintypes.HWND, ctypes.POINTER(_WINCOMPATTRDATA)]
        _SetWCA.restype = wintypes.BOOL
    except AttributeError:          # very old Windows
        _SetWCA = None
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None


def transparency_effects_on() -> bool:
    """Windows Settings → Personalization → Colors → "Transparency effects".
    When it's off, Windows draws blur-behind as a solid dark panel, so we don't use it."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            value, _ = winreg.QueryValueEx(k, "EnableTransparency")
            return bool(value)
    except OSError:
        return True          # setting not present: Windows' default is on


def set_frosted(hwnd: int, enabled: bool, tint_hex: str = "#000000", tint_alpha: float = 0.3) -> bool:
    """Turn the blur-behind effect on/off. Returns True if Windows accepted it."""
    if not IS_WINDOWS or not hwnd or _SetWCA is None:
        return False
    try:
        for state in ((ACCENT_ENABLE_ACRYLICBLURBEHIND, ACCENT_ENABLE_BLURBEHIND) if enabled else (ACCENT_DISABLED,)):
            accent = _ACCENTPOLICY(state, 2 if enabled else 0, acrylic_tint(tint_hex, tint_alpha), 0)
            data = _WINCOMPATTRDATA(WCA_ACCENT_POLICY, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                                    ctypes.sizeof(accent))
            if _SetWCA(hwnd, ctypes.byref(data)):
                return True
        return False
    except Exception:
        log.exception("set_frosted failed")
        return False


def set_rounded_shape(hwnd: int, width: int, height: int, radius: int) -> None:
    """Clip the window (and its blur) to a rounded rectangle; radius 0 = square/no clipping."""
    if not IS_WINDOWS or not hwnd:
        return
    try:
        if radius > 0:
            rgn = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
            ok_rgn = user32.SetWindowRgn(hwnd, rgn, True)       # Windows owns the region afterwards
        else:
            ok_rgn = user32.SetWindowRgn(hwnd, None, True)
        hr = None
        if dwmapi is not None:                          # Windows 11: smooth system rounding too
            pref = ctypes.c_int(DWMWCP_ROUND if radius > 0 else DWMWCP_DONOTROUND)
            hr = dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                              ctypes.byref(pref), ctypes.sizeof(pref))
        log.info("rounded shape r=%s: SetWindowRgn=%s DwmCorner=%s", radius, ok_rgn, hr)
    except Exception:
        log.exception("set_rounded_shape failed")


DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def set_dark_titlebar(hwnd: int) -> None:
    """Give one of our normal windows (Settings, sync window) a dark Windows title bar."""
    if not IS_WINDOWS or not hwnd or dwmapi is None:
        return
    try:
        on = ctypes.c_int(1)
        for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, 19):      # 19 = older Windows 10 builds
            if dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        log.exception("set_dark_titlebar failed")
