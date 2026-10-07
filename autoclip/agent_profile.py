"""Interactive CLI for an agent to locate controls on live calibration screenshots.

Only scratch pages are edited. Coordinates come from the agent's current screenshot,
never from a preset. The resulting profile is unusable until native-frame verification.
"""
from __future__ import annotations

from pathlib import Path
import time

from .common import now, read_json, relative, resolve, sha256, work_directory, write_json
from .profile import detect_canvas_rect, padded, profile_path, refine_rect, scratch_page

GROUPS = {
    'frame-dialog': ('new_frame_width', 'new_frame_ok'),
    'layers': ('top_folder_name', 'row2_name', 'top_folder_toggle'),
    'frame-tool': ('frame_tool',),
    'cut-group': ('cut_group',),
    'divide-folder': ('divide_folder',),
    'properties': ('gap_vertical', 'gap_horizontal', 'selection_tool'),
    'import-menu': ('import_image_menu',),
    'ready': (),
}


def session_path(config):
    return work_directory(config)/'profile/agent-session.json'


def matches_document_tab(desktop, session):
    """Match an agent-observed active tab only when CLIP exposes a generic title."""
    import numpy as np
    proof = session.get('document_tab')
    if not proof or proof['hwnd'] != int(desktop.hwnd) or proof['pid'] != desktop.pid:
        return False
    current = np.asarray(desktop.capture().convert('RGB').crop(proof['rect']), dtype=np.int16)
    reference = np.asarray(proof['pixels'], dtype=np.int16)
    return current.shape == reference.shape and float(np.abs(current - reference).mean()) <= 1.0


def register_document_tab(config, session, rect, snapshot):
    """Bind a visually read label to this session; never issue document input."""
    from PIL import Image
    from .gui import Desktop
    if snapshot != session['screenshot_sha256'] or sha256(resolve(session['screenshot'])) != snapshot:
        raise ValueError('Stale screenshot token for scratch tab; inspect the current screenshot')
    x1, y1, x2, y2 = rect
    left, top, right, bottom = session['environment']['window_rect']
    if not (left <= x1 < x2 <= right and max(0, top) <= y1 < y2 <= top + 160
            and 30 <= x2 - x1 <= 250 and 10 <= y2 - y1 <= 60):
        raise ValueError('Expected a small active document tab label above the canvas')
    import numpy as np
    with Image.open(resolve(session['screenshot'])) as shot:
        pixels = np.asarray(shot.convert('RGB').crop(rect))
    if float(pixels.std()) < 10:
        raise ValueError('Document tab proof must include readable text')
    desktop = Desktop(None)
    proof = {'rect': rect, 'pixels': pixels.tolist(),
             'hwnd': int(desktop.hwnd), 'pid': desktop.pid}
    # Persist only after foreground, environment and live label all agree.
    restore_scratch(desktop, {**session, 'document_tab': proof})
    session['document_tab'] = proof
    write_json(session_path(config), session)


def restore_scratch(desktop, session, attempts=5):
    """Retry focus/title settling only; never replay calibration edits."""
    from ctypes import wintypes as W
    from .window_handoff import Windows
    desktop.u.GetLastActivePopup.argtypes = [W.HWND]
    desktop.u.GetLastActivePopup.restype = W.HWND
    scratch = resolve(session['scratch'])
    api = Windows()
    restored = False
    for attempt in range(attempts):
        if desktop.environment() != session['environment']:
            raise ValueError('Screen/window/DPI changed during calibration; no input sent')
        foreground = desktop.u.GetForegroundWindow()
        if desktop.process(foreground) != desktop.pid:
            restored = True
            target = desktop.u.GetLastActivePopup(desktop.hwnd) or desktop.hwnd
            if desktop.process(target) != desktop.pid:
                raise ValueError('Scratch dialog belongs to a different process; no input sent')
            api.activate(target)
        # Activation may settle late after an approval window closes. Confirm the
        # actual foreground process before inspecting the document title.
        if desktop.process(desktop.u.GetForegroundWindow()) == desktop.pid:
            title = desktop.title(desktop.hwnd)
            if scratch.stem in title or (title.strip() == 'CLIP STUDIO PAINT'
                                        and matches_document_tab(desktop, session)):
                try:
                    desktop.guard(modal=True)
                except ValueError:
                    if desktop.process(desktop.u.GetForegroundWindow()) == desktop.pid:
                        raise
                    # A permission prompt may take focus between title and guard.
                    # Retry activation only; no editing input has been sent.
                else:
                    return restored
        if attempt + 1 < attempts:
            time.sleep(.3)
    raise ValueError('CALIBRATION_FOCUS_RETRY: could not restore and confirm the scratch page; '
                     'no calibration input sent. Finish the approval window, then run '
                     '`profile agent --step capture` and continue from the returned step. '
                     'Do not restart the session or delete its state.')


def owned_desktop(config, session):
    from .gui import Desktop
    desktop = Desktop(None)
    restored = restore_scratch(desktop, session)
    desktop.guard(modal=True)
    phase = session['phase']
    if phase == 'frame-dialog':
        # Some CLIP versions use an owned modeless frame dialog and keep the
        # main HWND enabled. Require the actual same-process dialog instead.
        if 'コマ枠' not in desktop.title() or desktop.u.GetForegroundWindow() == desktop.hwnd:
            raise ValueError('Expected the frame-folder dialog on the agent scratch page')
    elif phase == 'import-menu':
        if not desktop.u.IsWindowEnabled(desktop.hwnd):
            raise ValueError('Unexpected modal dialog during menu calibration')
    else:
        desktop.guard()
    # Returning from another app can dismiss a transient import menu. Reopen
    # only after document ownership and absence of unrelated dialogs are known.
    if restored and phase == 'import-menu':
        desktop.press('ESC'); desktop.press('ESC')
        desktop.press('ALT', 'F'); desktop.press('I')
    return desktop


def capture(config, session, desktop):
    shot = work_directory(config)/'profile/agent-screen.png'
    desktop.capture(shot)
    session['screenshot'] = relative(shot)
    session['screenshot_sha256'] = sha256(shot)
    session['updated_at'] = now()
    write_json(session_path(config), session)
    pending = [key for key in GROUPS[session['phase']] if key not in session['points']]
    print(f"agent calibration: {session['phase']}; locate: {', '.join(pending) or 'none'}")
    print(f"screenshot: {shot}")
    print(f"snapshot: {session['screenshot_sha256']}")
    print('next: profile agent --step point --point KEY --x X --y Y --snapshot HASH'
          if pending else 'next: profile agent --step finish')
    return shot


def start(config, line_px=None):
    from .gui import Desktop
    from .stages import focus_clip
    scratch = scratch_page(config, 'record-agent.clip')
    focus_clip()
    desktop = Desktop(None)
    desktop.guard()
    desktop.open(scratch)
    if '*' in desktop.title(desktop.hwnd):
        raise ValueError('Agent scratch page unexpectedly modified')
    image = desktop.capture()
    rect = refine_rect(image, detect_canvas_rect(image, config['canvas']['pixel_size']))
    line = float(line_px if line_px is not None else config['mapping']['frame_line_width_px'])
    if not 0 < line < 100:
        raise ValueError('Invalid frame line width')
    session = {'schema_version': 1, 'phase': 'frame-dialog', 'scratch': relative(scratch),
               'environment': desktop.environment(), 'canvas_screen_rect': rect, 'points': {},
               'line_px': line, 'canvas_size_px': config['canvas']['pixel_size'],
               'basic_frame_mm': config['canvas']['basic_frame_mm']}
    desktop.press('ALT', 'L'); desktop.press('E'); desktop.press('C')
    if 'コマ枠' not in desktop.title():
        raise ValueError('Frame-folder dialog did not open; stop and inspect CLIP STUDIO')
    return capture(config, session, desktop)


def advance(config, session, desktop):
    phase, points = session['phase'], session['points']
    if not all(key in points for key in GROUPS[phase]):
        return
    if phase == 'frame-dialog':
        desktop.press('CTRL', 'A', modal=True); desktop.text('AC_base_frame', modal=True)
        desktop.click(points['new_frame_width'], modal=True)
        desktop.press('CTRL', 'A', modal=True); desktop.text(str(session['line_px']), modal=True)
        desktop.click(points['new_frame_ok'], modal=True)
        desktop.wait_main()
        session['phase'] = 'layers'
    elif phase == 'layers':
        height = points['row2_name'][1] - points['top_folder_name'][1]
        if not 20 <= height <= 100:
            raise ValueError('Layer row spacing looks wrong; inspect the first two layer names')
        session['row_height'] = height
        session['phase'] = 'frame-tool'
    elif phase in ('frame-tool', 'cut-group', 'divide-folder'):
        key = GROUPS[phase][0]
        desktop.click(points[key])
        session['phase'] = {'frame-tool': 'cut-group', 'cut-group': 'divide-folder',
                            'divide-folder': 'properties'}[phase]
    elif phase == 'properties':
        desktop.press('ALT', 'F'); desktop.press('I')
        session['phase'] = 'import-menu'
    elif phase == 'import-menu':
        desktop.press('ESC'); desktop.press('ESC')
        session['phase'] = 'ready'


def point(config, session, desktop, key, x, y, snapshot):
    if snapshot != session['screenshot_sha256']:
        raise ValueError('Stale or missing screenshot token; inspect a new capture before recording coordinates')
    if key not in GROUPS[session['phase']]:
        raise ValueError(f'Point {key} is not part of the current calibration step')
    width, height = session['environment']['screen_size']
    if x is None or y is None or not (0 <= x < width and 0 <= y < height):
        raise ValueError('Point must be within the captured screen')
    x1, y1, x2, y2 = session['environment']['window_rect']
    if not (x1 <= x < x2 and y1 <= y < y2):
        raise ValueError('Point is outside CLIP STUDIO')
    session['points'][key] = [x, y]
    # Persist each recorded point before any action so errors are recoverable.
    write_json(session_path(config), session)
    advance(config, session, desktop)
    return capture(config, session, desktop)


def finish(config, session, desktop):
    from .profile import verify
    if session['phase'] != 'ready':
        raise ValueError('Agent calibration points are incomplete')
    points = {k: v for k, v in session['points'].items() if k != 'row2_name'}
    rect, screen = session['canvas_screen_rect'], session['environment']['screen_size']
    points['hover_away'] = [min(screen[0] - 2, rect[2] + 10), min(screen[1] - 2, rect[3] + 4)]
    for key in ('frame_tool', 'cut_group', 'divide_folder'):
        desktop.click(points[key])
    desktop.u.SetCursorPos(*points['hover_away'])
    import time
    time.sleep(.4)
    reference = profile_path(config).with_name(profile_path(config).stem + '-split.png')
    desktop.capture(reference)
    calib = {'schema_version': 1, 'environment': session['environment'], 'canvas_screen_rect': rect,
             'canvas_size_px': session['canvas_size_px'], 'frame_line_width_px': session['line_px'],
             'points': points, 'validated_at': None, 'validated_native_frame_folder_flag': None,
             'notes': ['Coordinates located by agent from current calibration screenshots; verification pending.'],
             'split': {'workflow': 'template_split', 'verified': False,
                       'basic_frame_mm': session['basic_frame_mm'], 'reference': relative(reference),
                       'guard_regions': [padded([points['cut_group'], points['divide_folder']], 60, screen),
                                         padded([points['gap_vertical'], points['gap_horizontal']], 50, screen)],
                       'geometry_tolerance_screen_px': 2.5, 'layer_row_height': session['row_height'],
                       'gap_unit': 'integer_canvas_pixels', 'folder_order': 'upper before lower; right before left'}}
    # Save/close only the scratch document opened by start().
    desktop.save(resolve(session['scratch']))
    desktop.press('CTRL', 'W')
    write_json(profile_path(config), calib)
    config['mapping']['frame_line_width_px'] = session['line_px']
    write_json(work_directory(config)/'job.json', {k: v for k, v in config.items() if not k.startswith('_')})
    result = verify(config)
    session['phase'] = 'verified'
    write_json(session_path(config), session)
    return result


def main(config, step='start', key=None, x=None, y=None, snapshot=None, line_px=None, tab_rect=None):
    from .completion import run_calibration
    from .stages import stage_init
    stage_init(config)
    if step == 'start':
        return start(config, line_px)
    session = read_json(session_path(config))
    if session['canvas_size_px'] != config['canvas']['pixel_size'] or session['basic_frame_mm'] != config['canvas']['basic_frame_mm']:
        raise ValueError('Paper settings changed during calibration; start a new session')
    if session['phase'] == 'verified':
        raise ValueError('Session is already verified; use run all')
    if tab_rect is not None:
        if step != 'capture':
            raise ValueError('Document tab registration is allowed only with capture')
        register_document_tab(config, session, tab_rect, snapshot)
    desktop = owned_desktop(config, session)
    if step == 'capture':
        return capture(config, session, desktop)
    if step == 'point':
        return point(config, session, desktop, key, x, y, snapshot)
    if step == 'click':
        if session['phase'] != 'properties':
            raise ValueError('Adjustment clicks are allowed only in the split-tool properties step')
        if snapshot != session['screenshot_sha256'] or x is None or y is None:
            raise ValueError('A current screenshot token and coordinates are required')
        if not (0 <= x < session['environment']['screen_size'][0] and 0 <= y < session['environment']['screen_size'][1]):
            raise ValueError('Adjustment point is outside the screen')
        x1, y1, x2, y2 = session['environment']['window_rect']
        if not (x1 <= x < x2 and y1 <= y < y2):
            raise ValueError('Adjustment point is outside CLIP STUDIO')
        cx1, cy1, cx2, cy2 = session['canvas_screen_rect']
        if cx1 <= x <= cx2 and cy1 <= y <= cy2:
            raise ValueError('Tool-settings adjustment cannot click the drawing canvas')
        desktop.click([x, y])
        for key in GROUPS['properties']:
            session['points'].pop(key, None)
        return capture(config, session, desktop)
    if step == 'finish':
        print(run_calibration(config, lambda: finish(config, session, desktop), 'profile'))
        return profile_path(config)
    raise ValueError('Unknown agent calibration step')
