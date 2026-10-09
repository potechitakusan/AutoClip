"""UI部品を毎回画像から探す。スクリーン座標は永続化しない。"""
from pathlib import Path
import time
import cv2
import numpy as np
from PIL import Image
from .config import ROOT, read_json


class EnvironmentUnavailable(RuntimeError):
    pass


class CanvasMismatch(EnvironmentUnavailable):
    """The detected paper area cannot be the page; nothing has been drawn yet."""


# Allowed relative error of the paper's aspect ratio (--tolerance). A 1px screen rounding on each side is added on top.
TOLERANCE = {'small': .005, 'medium': .015, 'large': .05, 'ignore': None}

# Where a part may sit, as screen fractions (x1, y1, x2, y2). Another palette's look-alike is then never picked.
REGION = {'view_top_left': (0, 0, .5, .5), 'view_bottom_right': (.5, .5, 1, 1)}


def match(screen, template):
    screen, template = np.asarray(screen.convert('RGB')), np.asarray(template.convert('RGB'))
    if template.shape[0] > screen.shape[0] or template.shape[1] > screen.shape[1]:
        return -1, None
    values = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, point = cv2.minMaxLoc(values)
    return score, (point[0]+template.shape[1]/2, point[1]+template.shape[0]/2)


def topmost_match(screen, template, threshold):
    """Centre of the highest on-screen location that matches the template at least as well as the threshold."""
    values = cv2.matchTemplate(np.asarray(screen.convert('RGB')), np.asarray(template.convert('RGB')), cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(values >= threshold)
    if len(ys) == 0:
        return None
    best = min(range(len(ys)), key=lambda i: (ys[i], xs[i]))
    return xs[best]+template.width/2, ys[best]+template.height/2


def matches(screen, template, threshold):
    """Distinct on-screen centres where the template matches at least as well as the threshold, best first."""
    screen, template = np.asarray(screen.convert('RGB')), np.asarray(template.convert('RGB'))
    if template.shape[0] > screen.shape[0] or template.shape[1] > screen.shape[1]:
        return []
    values = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(values >= threshold)
    height, width = template.shape[:2]
    kept = []
    for i in np.argsort(-values[ys, xs])[:2000]:
        x, y = int(xs[i]), int(ys[i])
        # Neighbouring pixels of one real match all score high; only a different place counts as another match.
        if all(abs(x-a) >= width or abs(y-b) >= height for a, b in kept):
            kept.append((x, y))
    return [(x+width/2, y+height/2) for x, y in kept]


def check_canvas(rect, size, tolerance='medium'):
    """Reject a paper area whose shape cannot be the page, before anything is drawn."""
    if tolerance not in TOLERANCE:
        raise ValueError('誤差の許容は small / medium / large / ignore から選んでください。')
    w, h = rect[2]-rect[0], rect[3]-rect[1]
    if w <= 0 or h <= 0:
        raise CanvasMismatch('用紙領域を認識できません。校正部品 view_top_left / view_bottom_right を採り直してください。')
    allowed = TOLERANCE[tolerance]
    if allowed is None:
        return
    expected, actual = size[0]/size[1], w/h
    if abs(actual/expected-1) > allowed+1/w+1/h:
        raise CanvasMismatch(f'用紙領域の縦横比が合いません（画面 {w}×{h}px、用紙 {size[0]}×{size[1]}px）。'
                             '校正部品 view_top_left / view_bottom_right を周辺を含めて採り直してください。')


def detect_canvas(screen, view):
    x1, y1, x2, y2 = view
    gray = np.asarray(screen.convert('L'))[y1:y2, x1:x2]
    mask = (gray > 120).astype(np.uint8)*255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    if count < 2:
        raise ValueError('キャンバスを検出できません。')
    x, y, w, h, _ = stats[1:][np.argmax(stats[1:, cv2.CC_STAT_AREA])]
    return [int(x+x1), int(y+y1), int(x+x1+w), int(y+y1+h)]


def screen_points(poly, canvas_size, rect):
    x1, y1, x2, y2 = rect
    sx, sy = (x2-x1)/canvas_size[0], (y2-y1)/canvas_size[1]
    ideal = [(x1+x*sx, y1+y*sy) for x, y in poly]
    points = [(round(x), round(y)) for x, y in ideal]
    error = max(max(abs(x-a)/sx, abs(y-b)/sy) for (x, y), (a, b) in zip(ideal, points))
    return points, error


class UI:
    def __init__(self, desktop, profile, work_root=ROOT/'work'):
        self.desktop = desktop
        self.work_root = Path(work_root).resolve()
        self.directory = Path(profile)
        try:
            self.profile = read_json(self.directory/'profile.json')
        except (ValueError, OSError) as exc:
            raise EnvironmentUnavailable('校正がありません。autoclip setup prepare --cmc <CMC> を実行してください。') from exc
        session = self.profile.get('session')
        if session and any(session[k] != desktop.identity()[k] for k in ('screen', 'dpi')):
            raise EnvironmentUnavailable('校正後に画面設定が変わりました。autoclip setup prepare --recalibrate --cmc <CMC> を実行してください。')
        if not self.profile.get('verified'):
            raise EnvironmentUnavailable('校正が未完了です。autoclip setup check を実行してください。')

    # A tool icon looks different when its tool is already selected; both states are calibrated.
    ALTERNATE = {'frame_tool': 'frame_tool_on'}

    def locate(self, name, threshold=.85):
        try:
            return self._locate(name, threshold)
        except EnvironmentUnavailable:
            alternate = self.ALTERNATE.get(name)
            if alternate and alternate in self.profile['parts']:
                return self._locate(alternate, threshold)
            raise

    # Parts that repeat on screen (one folder icon per frame row): the topmost match is the newest frame.
    TOPMOST = {'top_folder'}

    def _locate(self, name, threshold=.85):
        item = self.profile['parts'].get(name)
        if item is None:
            raise EnvironmentUnavailable('校正部品が不足しています。autoclip setup prepare --recalibrate --cmc <CMC> を実行してください。')
        with Image.open(self.directory/item['image']) as template:
            for _ in range(2):
                screen = self.desktop.capture()
                left, top = 0, 0
                if name in REGION:
                    fx1, fy1, fx2, fy2 = REGION[name]
                    left, top = int(screen.width*fx1), int(screen.height*fy1)
                    screen = screen.crop((left, top, int(screen.width*fx2), int(screen.height*fy2)))
                score, center = match(screen, template)
                if center:
                    center = (center[0]+left, center[1]+top)
                if score >= threshold:
                    if name in self.TOPMOST:
                        center = topmost_match(screen, template, threshold) or center
                    dx, dy = item.get('click_offset', [0, 0])
                    return (round(center[0]+dx), round(center[1]+dy))
        raise EnvironmentUnavailable(f'校正が古いです（部品 {name} が見つかりません）。autoclip setup prepare --recalibrate --cmc <CMC> を実行してください。')

    def click(self, name, double=False):
        self.desktop.click(self.locate(name), double=double, modal=True)

    def ensure_off(self, name):
        for _ in range(2):
            image = self.desktop.capture()
            scores = []
            for state in ('off', 'on'):
                item = self.profile['parts'][name+'_'+state]
                with Image.open(self.directory/item['image']) as template:
                    scores.append(match(image, template)[0])
            if max(scores) >= getattr(self, 'match_threshold', .85):
                if scores[0] >= scores[1]:
                    return
                self.click(name+'_on')
                # Confirm the resulting state from a fresh capture.
                self.locate(name+'_off')
                return
        raise EnvironmentUnavailable('チェック状態を識別できません。校正をやり直してください。')

    def picker(self, path, dialog='file_dialog'):
        path = Path(path).resolve()
        if not path.is_relative_to(self.work_root):
            raise EnvironmentUnavailable('work外のファイルはGUIへ投入できません。')
        if not path.is_file():
            raise ValueError('投入ファイルがありません。')
        deadline = time.monotonic()+6   # the dialog needs a moment to appear
        while True:
            try:
                self.locate(dialog)
                break
            except EnvironmentUnavailable:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.3)
        for _ in range(3):
            self.desktop.press('ALT', 'N', modal=True)
            self.desktop.press('CTRL', 'A', modal=True)
            self.desktop.text(str(path), modal=True)
            if self.desktop.file_name_text() == str(path):
                self.desktop.press('ENTER', modal=True)
                self.desktop.wait_main()
                return
        raise ValueError('ファイル名の入力を確認できません。')

    def open_page(self, path):
        self.desktop.guard()
        self.desktop.document = None
        self.desktop.press('CTRL', 'O')
        try:
            self.picker(path)
        except ValueError:
            self.desktop.press('ESC', modal=True)
            self.desktop.wait_main()
            raise
        # Bind before any edit. Generic application-only titles require M3 work.
        self.desktop.document = Path(path).stem
        self.desktop.guard()

    def restore_guard(self, path):
        self.desktop.guard()
        if Path(path).stem in self.desktop.title(self.desktop.hwnd):
            raise EnvironmentUnavailable('投入先と同名のタブが開いています。対象の作業用タブを保存せず閉じてから再実行してください。')

    def canvas(self, size=None, tolerance='medium'):
        self.click('view_menu')
        self.click('fit_canvas')
        # View bounds are runtime window-relative, never calibration coordinates.
        top_left = self.locate('view_top_left')
        bottom_right = self.locate('view_bottom_right')
        if bottom_right[0]-top_left[0] < 100 or bottom_right[1]-top_left[1] < 100:
            raise CanvasMismatch('ビューの範囲を認識できません。校正部品 view_top_left / view_bottom_right を採り直してください。')
        view = [*top_left, *bottom_right]
        rect = detect_canvas(self.desktop.capture(), view)
        if size:
            check_canvas(rect, size, tolerance)
        return rect

    def snapshot(self, rect, path):
        """Save the paper area as it looks on screen, for the trial page check."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.desktop.capture().crop(tuple(rect)).save(path)

    def select_tool(self, rectangle, width):
        self.click('frame_tool')
        self.click('frame_create')
        self.click('rectangle' if rectangle else 'polyline')
        self.ensure_off('raster')
        self.ensure_off('fill')
        self.click('brush_size')
        self.desktop.press('CTRL', 'A')
        self.desktop.text(f'{width:.3f}')
        self.desktop.press('ENTER')

    def frame(self, panel, size, rect, width, frames_only):
        self.select_tool(panel['rectangle'], width)
        points, error = screen_points(panel.get('frame_polygon') or panel['canvas_polygon'], size, rect)
        if panel['rectangle']:
            xs, ys = zip(*points)
            self.desktop.drag([min(xs), min(ys), max(xs), max(ys)])
        else:
            for point in points:
                self.desktop.click(point)
            self.desktop.press('ENTER')
        self.click('top_folder', double=True)
        self.desktop.press('CTRL', 'A')
        self.desktop.text(panel['folder_name'])
        self.desktop.press('ENTER')
        if not frames_only:
            self.click('file_menu')
            self.click('import_menu')
            self.click('image_menu')
            try:
                self.picker(panel['image_path'], 'image_dialog')
            except ValueError:
                self.desktop.press('ESC', modal=True)
                self.desktop.wait_main()
                raise
        # Next frame must be a root sibling, even when an imported image is selected.
        self.click('top_folder')
        return error

    def save(self, path=None):
        """Ctrl+S and wait until the file on disk has actually been rewritten.

        The OS window title may be only the generic application name (no '*'), so the file itself is watched.
        """
        before = None
        if path is not None:
            stat = Path(path).stat()
            before = (stat.st_mtime_ns, stat.st_size)
        self.desktop.press('CTRL', 'S')
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            self.desktop.guard()
            if path is not None:
                stat = Path(path).stat()
                if (stat.st_mtime_ns, stat.st_size) != before:
                    time.sleep(1.0)       # let the writer finish before the file is read
                    return
            elif '*' not in self.desktop.title(self.desktop.hwnd):
                return
            time.sleep(.2)
        raise ValueError('保存完了を確認できません。')

    def close(self):
        self.desktop.guard()
        self.desktop.press('CTRL', 'W')
        self.desktop.document = None
        # The confirmation dialog can take a moment to appear and does not always disable the main window,
        # so look for its title instead (up to 3 seconds; no dialog means nothing was unsaved).
        deadline = time.monotonic()+3
        shown = False
        while time.monotonic() < deadline and not shown:
            try:
                self.locate('save_dialog')
                shown = True
            except EnvironmentUnavailable:
                time.sleep(.2)
        if shown:
            self.click('discard_button')
        self.desktop.wait_main()
