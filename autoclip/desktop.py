import ctypes as C
from ctypes import wintypes as W
import os
import time
from pathlib import Path
import numpy as np
from PIL import ImageGrab
from .ui import EnvironmentUnavailable


class MouseInput(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('data', W.DWORD), ('flags', W.DWORD),
                ('time', W.DWORD), ('extra', C.c_size_t)]


class KeyboardInput(C.Structure):
    _fields_ = [('vk', W.WORD), ('scan', W.WORD), ('flags', W.DWORD),
                ('time', W.DWORD), ('extra', C.c_size_t)]


class HardwareInput(C.Structure):
    _fields_ = [('message', W.DWORD), ('low', W.WORD), ('high', W.WORD)]


class InputPayload(C.Union):
    _fields_ = [('mouse', MouseInput), ('keyboard', KeyboardInput), ('hardware', HardwareInput)]


class Input(C.Structure):
    _fields_ = [('type', W.DWORD), ('payload', InputPayload)]

class Desktop:
    def focus(self):
        # Restore only a minimized window; SW_RESTORE would un-maximize a maximized one.
        if self.u.IsIconic(self.hwnd):
            self.u.ShowWindow(self.hwnd, 9)
        for _ in range(5):
            self.u.SetForegroundWindow(self.hwnd)
            time.sleep(.2)
            if self.process(self.u.GetForegroundWindow()) == self.pid:
                return
        raise EnvironmentUnavailable('クリスタを前面に戻せません。')

    def identity(self):
        return {'pid': self.pid, 'hwnd': int(self.hwnd),
                'screen': list(self.capture().size), 'dpi': self.u.GetDpiForWindow(self.hwnd)}

    def guard_point(self, point):
        self.u.WindowFromPoint.argtypes = [W.POINT]
        self.u.WindowFromPoint.restype = W.HWND
        if self.process(self.u.WindowFromPoint(W.POINT(*point))) != self.pid:
            raise EnvironmentUnavailable('クリスタの部品以外はクリックできません。')

    def send(self, kind, flags, vk=0):
        event = Input()
        event.type = kind
        if kind == 1:
            event.payload.keyboard = KeyboardInput(vk, 0, flags, 0, 0)
        else:
            event.payload.mouse = MouseInput(0, 0, 0, flags, 0, 0)
        self.u.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
        if self.u.SendInput(1, C.byref(event), C.sizeof(Input)) != 1:
            raise EnvironmentUnavailable('画面への入力を送信できません。')
    def guard(self, modal=False):
        if self.process(self.u.GetForegroundWindow()) != self.pid:
            raise EnvironmentUnavailable('クリスタ以外が前面です。入力を停止しました。')
        if not modal and not self.u.IsWindowEnabled(self.hwnd):
            raise EnvironmentUnavailable('予期しないダイアログが開いています。')
        title = self.title(self.hwnd)
        # Some builds expose only the generic OS title; then the tab cannot be told apart and is not enforced.
        if self.document and title.strip() != 'CLIP STUDIO PAINT' and self.document not in title:
            raise EnvironmentUnavailable('作業ページを確認できません。入力を停止しました。')

    def open_practice(self, path):
        path = Path(path).resolve()
        from .config import ROOT
        if not path.is_relative_to(ROOT/'work') or not path.is_file():
            raise EnvironmentUnavailable('校正用作業コピー以外は開けません。')
        self.document = None
        self.press('CTRL', 'O')
        deadline = time.monotonic()+5
        while '開く' not in self.title() and time.monotonic() < deadline:
            time.sleep(.1)
        if '開く' not in self.title():
            raise EnvironmentUnavailable('ファイル選択画面を確認できません。')
        for _ in range(3):
            self.press('ALT', 'N', modal=True)
            self.press('CTRL', 'A', modal=True)
            self.text(str(path), modal=True)
            if self.file_name_text() == str(path):
                self.press('ENTER', modal=True)
                self.wait_main()
                self.document = path.stem
                self.guard()
                return
        raise EnvironmentUnavailable('練習用ページの入力を確認できません。')

    def discard_dialog(self):
        return not self.u.IsWindowEnabled(self.hwnd)

    def minimize(self):
        self.u.ShowWindow(self.hwnd, 6)
        if self.return_window != self.hwnd and self.u.IsWindow(self.return_window):
            self.u.SetForegroundWindow(self.return_window)

    def __init__(self, calibration=None):
        if os.name != "nt":
            raise EnvironmentUnavailable('Windowsのデスクトップが必要です。')
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.u.SetProcessDPIAware()
        self.u.GetForegroundWindow.restype = W.HWND
        self.return_window = self.u.GetForegroundWindow()
        self.u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.u.IsWindowVisible.argtypes = [W.HWND]
        self.u.IsWindowEnabled.argtypes = [W.HWND]
        self.u.IsWindow.argtypes = [W.HWND]
        self.u.ShowWindow.argtypes = [W.HWND, C.c_int]
        self.u.GetDpiForWindow.argtypes = [W.HWND]
        self.u.SetForegroundWindow.argtypes = [W.HWND]
        self.u.OpenClipboard.argtypes = [W.HWND]
        self.u.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
        self.u.SetClipboardData.restype = W.HANDLE
        self.k.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
        self.k.GlobalAlloc.restype = W.HANDLE
        self.k.GlobalLock.argtypes = [W.HANDLE]
        self.k.GlobalLock.restype = C.c_void_p
        self.k.GlobalUnlock.argtypes = [W.HANDLE]
        windows = []
        callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def collect(hwnd, _):
            if self.u.IsWindowVisible(hwnd) and "CLIP STUDIO PAINT" in self.title(hwnd):
                r = self.rect(hwnd)
                # A minimized window (left that way by the previous command) reports a tiny rectangle.
                if self.u.IsIconic(hwnd) or (r[2]-r[0] > 1000 and r[3]-r[1] > 700):
                    windows.append(hwnd)
            return True
        self.u.EnumWindows(callback(collect), 0)
        if len(windows) != 1:
            raise EnvironmentUnavailable('対象のクリスタを1つだけ表示してください。')
        self.hwnd = windows[0]
        self.pid = self.process(self.hwnd)
        self.calibration = calibration
        self.document = None

    def process(self, hwnd):
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return pid.value

    def title(self, hwnd=None):
        buffer = C.create_unicode_buffer(2048)
        self.u.GetWindowTextW(hwnd or self.u.GetForegroundWindow(), buffer, len(buffer))
        return buffer.value

    def rect(self, hwnd):
        r = W.RECT()
        self.u.GetWindowRect(hwnd,C.byref(r))
        return [r.left,r.top,r.right,r.bottom]

    def press(self, *keys, modal=False):
        self.guard(modal)
        codes = {"CTRL":17,"ALT":18,"SHIFT":16,"ENTER":13,"ESC":27,"TAB":9,
                 "HOME":36,"LEFT":37,"UP":38,"RIGHT":39,"DOWN":40}
        values = [codes[k] if k in codes else ord(k.upper()) for k in keys]
        for key in values:
            self.send(1, 0, key)
        time.sleep(.035)
        for key in reversed(values):
            self.send(1, 2, key)
        time.sleep(.18)

    def text(self, value, modal=False):
        self.guard(modal)
        payload = (value+"\0").encode("utf-16le")
        memory = self.k.GlobalAlloc(2,len(payload))
        pointer = self.k.GlobalLock(memory)
        if not pointer:
            raise EnvironmentUnavailable('クリップボードのメモリーを確保できません。')
        C.memmove(pointer,payload,len(payload))
        self.k.GlobalUnlock(memory)
        for _ in range(20):
            if self.u.OpenClipboard(self.hwnd):
                break
            time.sleep(.05)
        else:
            raise EnvironmentUnavailable('クリップボードを使用できません。')
        try:
            self.u.EmptyClipboard()
            if not self.u.SetClipboardData(13,memory):
                raise EnvironmentUnavailable('クリップボードに書き込めません。')
        finally:
            self.u.CloseClipboard()
        self.press("CTRL","V",modal=modal)

    def click(self, point, double=False, modal=False):
        self.guard(modal)
        self.u.SetCursorPos(*point)
        for _ in range(2 if double else 1):
            self.send(0, 2)
            self.send(0, 4)
            time.sleep(.075)
        time.sleep(.15)

    def drag(self, rect):
        self.guard()
        x1,y1,x2,y2 = rect
        self.u.SetCursorPos(x1,y1)
        self.send(0, 2)
        try:
            for t in np.linspace(0,1,25):
                self.guard()
                self.u.SetCursorPos(round(x1+(x2-x1)*t),round(y1+(y2-y1)*t))
                time.sleep(.012)
        finally:
            self.send(0, 4)
        time.sleep(.25)

    def capture(self, path=None):
        image = ImageGrab.grab()
        if path:
            Path(path).parent.mkdir(parents=True,exist_ok=True)
            image.save(path)
        return image

    def wait_main(self, timeout=15):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.guard(modal=True)
            if self.u.IsWindowEnabled(self.hwnd) and self.u.GetForegroundWindow() == self.hwnd:
                time.sleep(.6)
                return
            time.sleep(.15)
        raise EnvironmentUnavailable('クリスタがキャンバスに戻りません。')

    def file_name_text(self):
        """Read only the focused edit of the current picker; never guess coordinates."""
        self.guard(modal=True)
        class GUIThreadInfo(C.Structure):
            _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD)] + [
                (name, W.HWND) for name in ('active','focus','capture','menu','move','caret')] + [('rect', W.RECT)]
        info = GUIThreadInfo(); info.cbSize = C.sizeof(info)
        self.u.GetGUIThreadInfo.argtypes = [W.DWORD, C.POINTER(GUIThreadInfo)]
        thread = self.u.GetWindowThreadProcessId(self.u.GetForegroundWindow(), None)
        if not self.u.GetGUIThreadInfo(thread, C.byref(info)) or not info.focus:
            return ''
        if self.process(info.focus) != self.pid:
            return ''
        kind = C.create_unicode_buffer(100)
        self.u.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetClassNameW(info.focus, kind, len(kind))
        if kind.value.lower() != 'edit':
            return ''
        buffer = C.create_unicode_buffer(32768)
        self.u.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                                             W.UINT, W.UINT, C.POINTER(C.c_size_t)]
        result = C.c_size_t()
        if not self.u.SendMessageTimeoutW(info.focus, 13, len(buffer), C.addressof(buffer),
                                         2, 1000, C.byref(result)):
            return ''
        return buffer.value
