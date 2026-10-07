"""Prepare once, collect controls through Computer Use, verify once."""
from __future__ import annotations

import time
import uuid

from PIL import Image

from .common import read_json, relative, resolve, work_directory, write_json
from .profile import detect_canvas_rect, padded, profile_path, refine_rect, scratch_page, verify

REQUIRED = ('new_frame_width', 'new_frame_ok', 'top_folder_name', 'row2_name', 'top_folder_toggle',
            'frame_tool', 'cut_group', 'divide_folder', 'gap_vertical', 'gap_horizontal',
            'selection_tool', 'import_image_menu')


def validate_points(points, screen, window=None):
    for key in REQUIRED:
        point = points.get(key)
        if not isinstance(point, list) or len(point) != 2 or any(type(v) is not int for v in point):
            raise ValueError(f'Missing or invalid Computer Use point: {key}')
        if not (0 <= point[0] < screen[0] and 0 <= point[1] < screen[1]):
            raise ValueError(f'Computer Use point outside screen: {key}')
        if window and not (window[0] <= point[0] < window[2] and window[1] <= point[1] < window[3]):
            raise ValueError(f'Computer Use point outside CLIP STUDIO: {key}')
    height = points['row2_name'][1] - points['top_folder_name'][1]
    if not 20 <= height <= 100:
        raise ValueError('Invalid layer row spacing')
    return height


def main(config, step='prepare', observations=None):
    from .gui import Desktop
    from .stages import focus_clip, stage_init
    folder = work_directory(config)/'profile'
    session_file = folder/'computer-session.json'
    draft_file = folder/'computer-observations.json'
    if step == 'prepare':
        stage_init(config)
        focus_clip()
        desktop = Desktop(None)
        desktop.guard()
        if session_file.is_file():
            session = read_json(session_file)
            if session['environment'] != desktop.environment() or session['canvas'] != config['canvas']:
                raise ValueError('Existing Computer Use session settings differ; preserve it and diagnose before restarting')
        else:
            scratch = scratch_page(config, 'record-computer.clip')
            session = {'token': uuid.uuid4().hex, 'scratch': relative(scratch),
                       'environment': desktop.environment(), 'canvas': config['canvas'],
                       'line_px': config['mapping']['frame_line_width_px']}
            write_json(session_file, session)
            write_json(draft_file, {'token': session['token'], 'points': {},
                                   'blank_screenshot': relative(folder/'computer-blank.png'),
                                   'split_screenshot': relative(folder/'computer-split.png')})
        print(f"Computer Use scratch: {resolve(session['scratch'])}")
        print(f'Observed coordinates: {draft_file}; required: {", ".join(REQUIRED)}')
        print(f"Session token: {session['token']}; screen pixels: {session['environment']['screen_size']}")
        print(f"Frame name: AC_base_frame; line width: {session['line_px']} px")
        print('next: Computer Use opens only this scratch page, captures the blank page, '
              'locates controls, creates the base frame, saves/closes only this page, '
              'and records points plus the selected split-tool screenshot. '
              'Then profile computer --step finish. See AGENTS.md.')
        return
    if step != 'finish':
        raise ValueError('Unknown Computer Use calibration step')
    session = read_json(session_file)
    draft = resolve(observations) if observations else draft_file
    if not draft.resolve().is_relative_to(folder.resolve()):
        raise ValueError('Computer Use observations must be inside this job profile folder')
    data = read_json(draft)
    if (data.get('token') != session['token'] or session['canvas'] != config['canvas']
            or session['line_px'] != config['mapping']['frame_line_width_px']):
        raise ValueError('Computer Use observations belong to a different session or paper')
    points = data.get('points')
    if not isinstance(points, dict):
        raise ValueError('Computer Use observations must contain a points object')
    screen = session['environment']['screen_size']
    height = validate_points(points, screen, session['environment']['window_rect'])
    screenshots = []
    for key in ('blank_screenshot', 'split_screenshot'):
        path = resolve(data[key])
        if not path.resolve().is_relative_to(folder.resolve()) or not path.is_file():
            raise ValueError(f'{key} must be an existing screenshot inside the job profile folder')
        screenshots.append(path)
    with Image.open(screenshots[0]) as image:
        if list(image.size) != screen:
            raise ValueError('Computer Use screenshot coordinates must use original screen pixels')
        rect = refine_rect(image, detect_canvas_rect(image, config['canvas']['pixel_size']))
    with Image.open(screenshots[1]) as image:
        if list(image.size) != screen:
            raise ValueError('Computer Use split screenshot size differs')
    focus_clip()
    desktop = Desktop(None)
    desktop.guard()
    if desktop.environment() != session['environment']:
        raise ValueError('Screen/window/DPI changed since Computer Use preparation')
    from .split_frames import verify_base
    verify_base(resolve(session['scratch']))
    # The observed UI must still agree before any recorded coordinates are used.
    import numpy as np
    guards = [padded([points['cut_group'], points['divide_folder']], 60, screen),
              padded([points['gap_vertical'], points['gap_horizontal']], 50, screen)]
    with Image.open(screenshots[1]) as reference:
        actual = desktop.capture()
        for region in guards:
            if np.abs(np.asarray(actual.crop(region).convert('RGB'), dtype=float) -
                      np.asarray(reference.crop(region).convert('RGB'), dtype=float)).mean() > 3:
                raise ValueError('Split tool changed since the Computer Use observation; inspect a fresh screenshot')
    reference_path = profile_path(config).with_name(profile_path(config).stem+'-split.png')
    points = {k: list(v) for k, v in points.items() if k in REQUIRED and k != 'row2_name'}
    points['hover_away'] = [min(screen[0]-2, rect[2]+10), min(screen[1]-2, rect[3]+4)]
    desktop.u.SetCursorPos(*points['hover_away'])
    time.sleep(.4)
    desktop.capture(reference_path)
    calib = {'schema_version': 1, 'environment': session['environment'], 'canvas_screen_rect': rect,
             'canvas_size_px': config['canvas']['pixel_size'], 'frame_line_width_px': session['line_px'],
             'points': points, 'validated_at': None, 'validated_native_frame_folder_flag': None,
             'notes': ['Controls observed through Computer Use; native verification pending.'],
             'split': {'workflow': 'template_split', 'verified': False,
                       'basic_frame_mm': config['canvas']['basic_frame_mm'], 'reference': relative(reference_path),
                       'guard_regions': guards, 'geometry_tolerance_screen_px': 2.5,
                       'layer_row_height': height, 'gap_unit': 'integer_canvas_pixels',
                       'folder_order': 'upper before lower; right before left'}}
    write_json(profile_path(config), calib)
    from .completion import run_calibration
    print(run_calibration(config, lambda: verify(config), 'profile'))
