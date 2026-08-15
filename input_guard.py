from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes
from typing import Callable

import wininput
from wininput import MACROFLOW_INPUT_TAG


user32 = ctypes.windll.user32

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_MACROFLOW_INPUT = 0x8001
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_ESCAPE = 0x1B
VK_F8 = 0x77
VK_F9 = 0x78
VK_F12 = 0x7B
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x00000001

# Software hotkeys that keyboard capture must never claim.
RESERVED_HOTKEY_VKS = {VK_F8, VK_F9, VK_F12}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.BlockInput.argtypes = [wintypes.BOOL]
user32.BlockInput.restype = wintypes.BOOL


def should_block_keyboard(vk_code: int, flags: int, extra_info: int = 0) -> bool:
    """Allow physical F12 and MacroFlow packets; block every other source."""
    if int(vk_code) == VK_F12 and not (int(flags) & LLKHF_INJECTED):
        return False
    return not (
        int(flags) & LLKHF_INJECTED
        and int(extra_info) == MACROFLOW_INPUT_TAG
    )


def should_block_mouse(flags: int, extra_info: int = 0) -> bool:
    """Only allow mouse packets explicitly tagged by MacroFlow playback."""
    return not (
        int(flags) & LLMHF_INJECTED
        and int(extra_info) == MACROFLOW_INPUT_TAG
    )


class FocusInputGuard:
    """Block physical input globally while keeping an F12 emergency callback."""

    def __init__(self, on_f12: Callable[[], None] | None = None):
        self._on_f12 = on_f12
        self.active = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._keyboard_hook = None
        self._mouse_hook = None
        self._keyboard_proc = None
        self._mouse_proc = None
        self._error: Exception | None = None
        self._f12_down = False
        self._system_blocked = False
        self._input_requests: queue.Queue = queue.Queue()

    def start(self, timeout: float = 2.0) -> bool:
        if self.active:
            return True
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="MacroFlowFocusGuard", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            return False
        return self.active and self._error is None

    def block(self) -> bool:
        return self.active and self._system_blocked

    def unblock(self) -> bool:
        self._stop_hooks()
        return not self._system_blocked

    def stop(self) -> None:
        self._stop_hooks()

    def release(self) -> None:
        """Stop the hooks from any thread."""
        self._stop_hooks()

    def _stop_hooks(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(1.0)
        self.active = False
        self._thread = None
        self._thread_id = 0

    def _dispatch_input(self, input_obj) -> None:
        """Execute an input packet on the thread that owns BlockInput."""
        if threading.current_thread() is self._thread:
            wininput._send_input_direct(input_obj)
            return
        if not self.active or not self._thread_id:
            raise RuntimeError("强制专注输入线程未运行。")
        done = threading.Event()
        request = {"input": input_obj, "done": done, "error": None}
        self._input_requests.put(request)
        if not user32.PostThreadMessageW(self._thread_id, WM_MACROFLOW_INPUT, 0, 0):
            request["error"] = RuntimeError("无法向专注模式输入线程发送回放动作。")
            done.set()
        while not done.wait(0.1):
            if not self.active:
                raise RuntimeError("强制专注模式已停止，回放动作未发送。")
        if request["error"] is not None:
            raise request["error"]

    def _drain_input_requests(self) -> None:
        while True:
            try:
                request = self._input_requests.get_nowait()
            except queue.Empty:
                return
            try:
                wininput._send_input_direct(request["input"])
            except Exception as exc:
                request["error"] = exc
            finally:
                request["done"].set()

    def _fail_input_requests(self) -> None:
        while True:
            try:
                request = self._input_requests.get_nowait()
            except queue.Empty:
                return
            request["error"] = RuntimeError("强制专注模式已停止，回放动作未发送。")
            request["done"].set()

    def _run(self) -> None:
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())

        @HOOKPROC
        def keyboard_proc(code, wparam, lparam):
            if code == HC_ACTION:
                data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if int(data.vkCode) == VK_F12 and not (int(data.flags) & LLKHF_INJECTED):
                    message = int(wparam)
                    if message in (WM_KEYDOWN, WM_SYSKEYDOWN) and not self._f12_down:
                        self._f12_down = True
                        if self._on_f12:
                            try:
                                self._on_f12()
                            except Exception:
                                pass
                    elif message in (WM_KEYUP, WM_SYSKEYUP):
                        self._f12_down = False
                if should_block_keyboard(data.vkCode, data.flags, data.dwExtraInfo):
                    return 1
            return user32.CallNextHookEx(self._keyboard_hook, code, wparam, lparam)

        @HOOKPROC
        def mouse_proc(code, wparam, lparam):
            if code == HC_ACTION:
                data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if should_block_mouse(data.flags, data.dwExtraInfo):
                    return 1
            return user32.CallNextHookEx(self._mouse_hook, code, wparam, lparam)

        self._keyboard_proc = keyboard_proc
        self._mouse_proc = mouse_proc
        try:
            self._keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, None, 0)
            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, None, 0)
            if not self._keyboard_hook or not self._mouse_hook:
                raise ctypes.WinError()
            # Low-level hooks can lock ordinary desktop input, but games may
            # read Raw Input/DirectInput directly. BlockInput is required to
            # suppress those physical device events as well. It only permits
            # SendInput from its owner thread, so all MacroFlow packets are
            # dispatched back to this same message-pump thread.
            if not user32.BlockInput(True):
                raise ctypes.WinError()
            self._system_blocked = True
            wininput.set_input_dispatcher(self._dispatch_input)
            self.active = True
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if int(message.message) == WM_MACROFLOW_INPUT:
                    self._drain_input_requests()
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            wininput.set_input_dispatcher(None)
            self._fail_input_requests()
            if self._system_blocked:
                user32.BlockInput(False)
                self._system_blocked = False
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            self._keyboard_hook = self._mouse_hook = None
            self.active = False


class KeyCapturer:
    """Capture the next physical key press via a low-level keyboard hook.

    The hook consumes the key-down event so it never reaches other windows.
    Esc cancels, F8 / F9 / F12 (software hotkeys) and injected keys pass
    through untouched. Callbacks fire on the hook thread; callers marshal
    them back to the UI thread (e.g. widget.after).
    """

    def __init__(self, on_key: Callable[[int], None], on_cancel: Callable[[], None] | None = None):
        self._on_key = on_key
        self._on_cancel = on_cancel
        self.active = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._keyboard_hook = None
        self._keyboard_proc = None
        self._error: Exception | None = None

    def start(self, timeout: float = 2.0) -> bool:
        if self.active:
            return True
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="MacroFlowKeyCapturer", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            return False
        return self.active and self._error is None

    def stop(self) -> None:
        """Stop the hook from any thread."""
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(1.0)
        self.active = False
        self._thread = None
        self._thread_id = 0

    def _run(self) -> None:
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())

        @HOOKPROC
        def keyboard_proc(code, wparam, lparam):
            if code == HC_ACTION:
                data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(data.vkCode)
                if int(wparam) in (WM_KEYDOWN, WM_SYSKEYDOWN) and not (int(data.flags) & LLKHF_INJECTED):
                    if vk == VK_ESCAPE:
                        if self._on_cancel:
                            try:
                                self._on_cancel()
                            except Exception:
                                pass
                        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                        return 1
                    if vk not in RESERVED_HOTKEY_VKS:
                        if self._on_key:
                            try:
                                self._on_key(vk)
                            except Exception:
                                pass
                        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                        return 1
                # Esc / reserved hotkeys / injected keys: let them through.
            return user32.CallNextHookEx(self._keyboard_hook, code, wparam, lparam)

        self._keyboard_proc = keyboard_proc
        try:
            self._keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, None, 0)
            if not self._keyboard_hook:
                raise ctypes.WinError()
            self.active = True
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
            self._keyboard_hook = None
            self.active = False
