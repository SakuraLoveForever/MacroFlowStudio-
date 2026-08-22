"""Match-area highlight overlay.

Draws a thin, visible border around a matched region on the desktop:
a borderless, transparent, click-through, always-on-top layered window
that shows itself for a short while and then hides.

Pure Win32 via ctypes. Thread-safe: ``show_overlay`` / ``hide_overlay``
may be called from any thread; the window lives in its own message-loop
thread so no Tk main-loop dependency is needed (the player may be running
in a worker thread).

Coordinates are physical pixels, matching the capture/send conventions
used by image_match and wininput. On DPI-unaware processes the window
coordinates are converted to virtualized pixels before creation.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

DEFAULT_COLOR = 0x000000FF  # 亮红 (GDI COLORREF, BGR)
DEFAULT_DURATION_MS = 900
BORDER_PX = 2

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
LWA_COLORKEY = 0x00000001
KEY_COLOR = 0x00FF00FF  # 洋红：作为窗口背景键色，被变为全透明
PS_SOLID = 0
TRANSPARENT_BKMODE = 1
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
WM_USER = 0x0400
WM_OVERLAY_SHOW = WM_USER + 1
WM_OVERLAY_HIDE = WM_USER + 2
MDT_EFFECTIVE_DPI = 0

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_shcore = ctypes.windll.shcore

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
    wintypes.WPARAM, wintypes.LPARAM,
)


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_class_name = "MacroFlowDetectOverlay"

_lock = threading.Lock()
_pending: tuple[int, int, int, int, int, int] | None = None  # (l,t,w,h,color,duration)
_window_hwnd: int | None = None
_window_thread: threading.Thread | None = None
_ready = threading.Event()


def _monitor_dpi(x: int, y: int) -> int:
    """Physical DPI of the monitor containing (x, y)."""
    try:
        point = wintypes.POINT(int(x), int(y))
        monitor = _user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
        if monitor:
            dpi_x = wintypes.UINT()
            dpi_y = wintypes.UINT()
            if _shcore.GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI,
                                         ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                return dpi_x.value or 96
    except (AttributeError, OSError):
        pass
    return 96


def _to_virtualized(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Convert physical pixels to virtualized pixels on a DPI-unaware process."""
    left, top, width, height = rect
    dpi = _monitor_dpi(left, top)
    scale = dpi / 96.0
    if abs(scale - 1.0) < 0.01:
        return rect
    return (
        round(left / scale), round(top / scale),
        round(width / scale), round(height / scale),
    )


def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    global _pending
    if msg == WM_PAINT:
        paint = PAINTSTRUCT()
        hdc = _user32.BeginPaint(hwnd, ctypes.byref(paint))
        rect = wintypes.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rect))
        with _lock:
            border_color = _pending[4] if _pending else DEFAULT_COLOR
        # 背景填充为键色（透明），再画一圈细边框。
        brush = _gdi32.CreateSolidBrush(KEY_COLOR)
        _gdi32.FillRect(hdc, ctypes.byref(rect), brush)
        _gdi32.DeleteObject(brush)
        pen = _gdi32.CreatePen(PS_SOLID, BORDER_PX, border_color)
        old_pen = _gdi32.SelectObject(hdc, pen)
        _gdi32.SetBkMode(hdc, TRANSPARENT_BKMODE)
        _gdi32.Rectangle(hdc, 1, 1, max(2, rect.right - 1), max(2, rect.bottom - 1))
        _gdi32.SelectObject(hdc, old_pen)
        _gdi32.DeleteObject(pen)
        _user32.EndPaint(hwnd, ctypes.byref(paint))
        return 0
    if msg == WM_TIMER:
        _user32.KillTimer(hwnd, 1)
        _user32.ShowWindow(hwnd, SW_HIDE)
        return 0
    if msg == WM_OVERLAY_SHOW:
        with _lock:
            data = _pending
        if data is None:
            return 0
        left, top, width, height, _color, duration_ms = data
        left, top, width, height = _to_virtualized((left, top, width, height))
        _user32.MoveWindow(
            hwnd, left - BORDER_PX, top - BORDER_PX,
            max(1, width + BORDER_PX * 2), max(1, height + BORDER_PX * 2),
            True,
        )
        _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        _user32.SetTimer(hwnd, 1, max(50, duration_ms), None)
        return 0
    if msg == WM_OVERLAY_HIDE:
        _user32.ShowWindow(hwnd, SW_HIDE)
        return 0
    if msg == WM_CLOSE:
        _user32.DestroyWindow(hwnd)
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _window_loop() -> None:
    global _window_hwnd
    wnd_proc = WNDPROC(_wnd_proc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = wnd_proc
    wc.hInstance = wintypes.HINSTANCE(ctypes.windll.kernel32.GetModuleHandleW(None))
    wc.lpszClassName = _class_name
    _user32.RegisterClassW(ctypes.byref(wc))
    hwnd = _user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
        _class_name, "", WS_POPUP,
        0, 0, 1, 1, None, None, wc.hInstance, None,
    )
    if not hwnd:
        return
    _user32.SetLayeredWindowAttributes(hwnd, KEY_COLOR, 0, LWA_COLORKEY)
    with _lock:
        _window_hwnd = int(hwnd)
    _ready.set()
    message = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(message))
        _user32.DispatchMessageW(ctypes.byref(message))
    _user32.DestroyWindow(hwnd)


def _ensure_window() -> int | None:
    global _window_thread
    with _lock:
        hwnd = _window_hwnd
        if hwnd is None and _window_thread is None:
            _window_thread = threading.Thread(target=_window_loop, daemon=True)
            _window_thread.start()
    if hwnd is None:
        _ready.wait(2.0)
        with _lock:
            hwnd = _window_hwnd
    return hwnd


def show_overlay(x: int, y: int, width: int, height: int,
                 color: int = DEFAULT_COLOR, duration_ms: int = DEFAULT_DURATION_MS) -> None:
    """Show a thin border around (x, y, width, height) for a short while.

    Coordinates are physical desktop pixels. Repeated calls refresh the
    border position and restart the auto-hide timer.
    """
    if width <= 0 or height <= 0:
        return
    hwnd = _ensure_window()
    if not hwnd:
        return
    with _lock:
        global _pending
        _pending = (int(x), int(y), int(width), int(height), int(color), int(duration_ms))
    _user32.PostMessageW(hwnd, WM_OVERLAY_SHOW, 0, 0)


def hide_overlay() -> None:
    """Hide the border immediately."""
    hwnd = _ensure_window()
    if hwnd:
        _user32.PostMessageW(hwnd, WM_OVERLAY_HIDE, 0, 0)
