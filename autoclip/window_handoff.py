"""Return to the identified Codex/VS Code window without touching documents."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import re
import time


def choose_codex_window(windows, foreground, workspace):
    native = [w for w in windows if w['exe'].lower() == 'codex.exe']
    editors = [w for w in windows if w['exe'].lower() in ('code.exe','code - insiders.exe')
               and workspace.casefold() in [s.strip().casefold() for s in re.split(r' [-–—] ',w['title'])]]
    candidates = native + editors
    current = next((w for w in candidates if w['hwnd'] == foreground),None)
    if current:
        return current
    if len(editors) == 1:
        return editors[0]
    if len(native) == 1:
        return native[0]
    return None # Never pick an unrelated editor/browser by Z order alone.


class Windows:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Windows desktop required')
        self.u = C.WinDLL('user32',use_last_error=True)
        self.k = C.WinDLL('kernel32',use_last_error=True)
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.GetWindow.argtypes = [W.HWND,W.UINT]; self.u.GetWindow.restype = W.HWND
        self.u.GetWindowTextW.argtypes = [W.HWND,W.LPWSTR,C.c_int]
        self.u.GetWindowThreadProcessId.argtypes = [W.HWND,C.POINTER(W.DWORD)]
        for name in ('IsWindowVisible','IsWindowEnabled','IsIconic','IsWindow'):
            getattr(self.u,name).argtypes = [W.HWND]
        self.u.ShowWindow.argtypes = [W.HWND,C.c_int]
        self.u.SetForegroundWindow.argtypes = [W.HWND]
        self.u.BringWindowToTop.argtypes = [W.HWND]
        self.u.AttachThreadInput.argtypes = [W.DWORD,W.DWORD,W.BOOL]
        self.k.GetCurrentThreadId.restype = W.DWORD
        self.u.FlashWindow.argtypes = [W.HWND,W.BOOL]
        self.k.OpenProcess.argtypes = [W.DWORD,W.BOOL,W.DWORD]; self.k.OpenProcess.restype = W.HANDLE
        self.k.QueryFullProcessImageNameW.argtypes = [W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
        self.k.CloseHandle.argtypes = [W.HANDLE]

    def identity(self, hwnd):
        if not self.u.IsWindow(hwnd):
            return None
        pid = W.DWORD();self.u.GetWindowThreadProcessId(hwnd,C.byref(pid))
        title = C.create_unicode_buffer(2048);self.u.GetWindowTextW(hwnd,title,len(title))
        handle = self.k.OpenProcess(0x1000,False,pid.value)
        if not handle:
            return None
        try:
            path = C.create_unicode_buffer(32768);size = W.DWORD(len(path))
            if not self.k.QueryFullProcessImageNameW(handle,0,path,C.byref(size)):
                return None
            return {'hwnd':int(hwnd),'pid':pid.value,'title':title.value,'exe':Path(path.value).name}
        finally:
            self.k.CloseHandle(handle)

    def windows(self):
        found = []
        callback = C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        def collect(hwnd, _):
            if self.u.IsWindowVisible(hwnd) and not self.u.GetWindow(hwnd,4): # GW_OWNER
                info = self.identity(hwnd)
                if info and info['exe'].lower() in ('codex.exe','code.exe','code - insiders.exe','clipstudiopaint.exe'):
                    found.append(info)
            return True
        self.u.EnumWindows(callback(collect),0)
        return found

    def foreground(self):
        return self.u.GetForegroundWindow()

    def minimize(self, hwnd):
        if not self.u.IsWindowEnabled(hwnd):
            return False # Leave unexpected dialogs visible for diagnosis.
        self.u.ShowWindow(hwnd,6) # SW_MINIMIZE; never send close/save shortcuts.
        time.sleep(.2)
        return bool(self.u.IsIconic(hwnd))

    def activate(self, hwnd):
        if self.u.IsIconic(hwnd):
            self.u.ShowWindow(hwnd,9) # SW_RESTORE
        self.u.SetForegroundWindow(hwnd)
        time.sleep(.2)
        active = self.foreground() == hwnd
        if not active:
            # Connect input queues only for this explicit handoff, and always
            # disconnect them. No ALT/TAB shortcuts are injected into documents.
            current = self.k.GetCurrentThreadId()
            foreground = self.foreground()
            attached = []
            try:
                for window in (foreground,hwnd):
                    if not window:
                        continue
                    thread = self.u.GetWindowThreadProcessId(window,None)
                    if thread and thread != current and thread not in attached:
                        if self.u.AttachThreadInput(current,thread,True):
                            attached.append(thread)
                self.u.BringWindowToTop(hwnd)
                self.u.SetForegroundWindow(hwnd)
            finally:
                for thread in reversed(attached):
                    self.u.AttachThreadInput(current,thread,False)
            time.sleep(.2)
            active = self.foreground() == hwnd
        if not active:
            self.u.FlashWindow(hwnd,True)
        return active


class Handoff:
    def __init__(self, workspace, api=None):
        self.api = api or Windows()
        self.workspace = workspace
        self.target = choose_codex_window(self.api.windows(),self.api.foreground(),workspace)

    def finish(self):
        windows = self.api.windows()
        clips = [w for w in windows if w['exe'].lower() == 'clipstudiopaint.exe']
        result = {'clip_minimized':False,'codex_foreground':False}
        if len(clips) != 1:
            result['reason'] = 'クリスタのメインウィンドウを一意に確認できません。'
            return result
        result['clip_minimized'] = self.api.minimize(clips[0]['hwnd'])
        if not result['clip_minimized']:
            result['reason'] = 'クリスタにダイアログがあるか、最小化できませんでした。'
            return result
        if self.target is None:
            result['reason'] = 'このプロジェクトのCodexウィンドウを特定できません。'
            return result
        current = self.api.identity(self.target['hwnd'])
        if (not current or any(current[k] != self.target[k] for k in ('pid','exe'))
                or not choose_codex_window([current],current['hwnd'],self.workspace)):
            result['reason'] = '復帰先のウィンドウが変更されています。'
            return result
        result['codex_foreground'] = self.api.activate(current['hwnd'])
        result['target'] = {'pid':current['pid'],'exe':current['exe']}
        if not result['codex_foreground']:
            result['reason'] = 'Windowsが前面への切り替えを許可しませんでした。'
        return result
