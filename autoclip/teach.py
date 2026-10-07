"""Instruction box plus an F8 recorder for the GUI profile.

The user moves the mouse onto a CLIP STUDIO control and presses F8; F8 is swallowed by a low-level
keyboard hook so CLIP STUDIO never sees it. The instruction box (scripts/teach-window.ps1) never takes
the keyboard focus. No GUI toolkit is needed beyond Windows PowerShell.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import time
from pathlib import Path

from .common import CODE_ROOT

VK_F8 = 0x77
WM_KEYDOWN, WM_SYSKEYDOWN = 0x100, 0x104
HOOKPROC = C.WINFUNCTYPE(C.c_ssize_t, C.c_int, W.WPARAM, W.LPARAM)


class KeyboardInfo(C.Structure):
    _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", C.c_size_t)]


class Teacher:
    def __init__(self, text_file, anchor=(100, 900)):
        self.u = C.WinDLL("user32", use_last_error=True)
        self.u.SetProcessDPIAware()
        self.u.SetWindowsHookExW.argtypes = [C.c_int, HOOKPROC, W.HINSTANCE, W.DWORD]
        self.u.SetWindowsHookExW.restype = W.HHOOK
        self.u.CallNextHookEx.argtypes = [W.HHOOK, C.c_int, W.WPARAM, W.LPARAM]
        self.u.CallNextHookEx.restype = C.c_ssize_t
        self.u.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        self.pressed = False
        self.total, self.index = 0, 0
        self.text_file = Path(text_file)
        self.text_file.parent.mkdir(parents=True, exist_ok=True)
        self.text_file.write_text("準備中…", encoding="utf-8")
        ready = self.text_file.with_suffix('.ready')
        ready.unlink(missing_ok=True)
        self.window_log = self.text_file.with_suffix('.window.log')
        self.log_stream = self.window_log.open('wb')
        self.window = subprocess.Popen(
            ["powershell.exe", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CODE_ROOT / "scripts/teach-window.ps1"),
             "-TextFile", str(self.text_file), "-X", str(anchor[0]), "-Y", str(anchor[1]), "-ParentPid", str(os.getpid()),
             "-ReadyFile", str(ready)],
            creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=self.log_stream)
        deadline = time.monotonic() + 15
        while not ready.is_file():
            if self.window.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise ValueError(f'Instruction window failed to appear; see {self.window_log}')
            time.sleep(.05)
        self.callback = HOOKPROC(self._hook)  # keep a reference or the hook is garbage collected
        self.hook = self.u.SetWindowsHookExW(13, self.callback, None, 0)
        if not self.hook:
            self.close()
            raise OSError("Could not install the F8 keyboard hook")

    def _hook(self, code, wparam, lparam):
        if code == 0 and C.cast(lparam, C.POINTER(KeyboardInfo)).contents.vkCode == VK_F8:
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self.pressed = True
            return 1  # swallow F8 down and up
        return self.u.CallNextHookEx(None, code, wparam, lparam)

    def pump(self):
        """Low-level hooks are delivered through this thread's message queue."""
        message = W.MSG()
        while self.u.PeekMessageW(C.byref(message), None, 0, 0, 1):
            self.u.TranslateMessage(C.byref(message))
            self.u.DispatchMessageW(C.byref(message))

    def show(self, text):
        self.text_file.write_text(text, encoding="utf-8")

    def cursor(self):
        point = W.POINT()
        self.u.GetCursorPos(C.byref(point))
        return [point.x, point.y]

    def ask(self, text, timeout=900):
        """Show an instruction and wait for F8. Returns the mouse position at that moment."""
        self.index += 1
        self.pressed = False
        self.show(f"校正 {self.index}/{self.total}\n{text}\nマウスを目的の位置に置いたまま F8 を押してください。")
        deadline = time.monotonic() + timeout
        while not self.pressed:
            if self.window.poll() is not None:
                raise ValueError(f'Instruction window exited; see {self.window_log}')
            self.pump()
            time.sleep(0.02)
            if time.monotonic() > deadline:
                raise TimeoutError("No F8 within the time limit")
        return self.cursor()

    def close(self):
        if getattr(self, "hook", None):
            self.u.UnhookWindowsHookEx(self.hook)
            self.hook = None
        try:
            self.show("__CLOSE__")
            self.window.wait(timeout=5)
        except Exception:
            self.window.kill()
            self.window.wait(timeout=5)
        finally:
            self.log_stream.close()
