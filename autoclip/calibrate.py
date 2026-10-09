"""切り抜きテンプレート校正。実機操作は呼び出し側が明示した時だけ。"""
from pathlib import Path
from PIL import Image
from .config import read_json, write_json
from .ui import UI, match, matches, EnvironmentUnavailable
from .importer import import_page
import re

PARTS = ('frame_tool', 'frame_tool_on', 'frame_create', 'rectangle', 'polyline', 'raster_on', 'raster_off',
         'fill_on', 'fill_off', 'brush_size', 'file_menu', 'import_menu', 'image_menu',
         'top_folder', 'file_dialog', 'image_dialog', 'save_dialog', 'discard_button', 'view_menu', 'fit_canvas',
         'view_top_left', 'view_bottom_right')
# Parts that legitimately repeat on screen (one folder icon per frame row).
REPEATING = {'top_folder'}
SMALL_PART = 24


def prepare(desktop, directory, practice, work):
    directory = Path(directory)
    practice = Path(practice).resolve()
    if not practice.is_relative_to(Path(work).resolve()/'project') or practice.is_symlink():
        # CLI uses its own dedicated work practice; caller must not pass an original.
        raise ValueError('練習用ページは校正用work内に用意してください。')
    directory.mkdir(parents=True, exist_ok=True)
    # Uses Desktop's checked clipboard picker adapter supplied by the caller.
    desktop.open_practice(practice)
    desktop.capture().save(directory/'screen.png')
    write_json(directory/'profile.json', {'verified': False, 'parts': {}, 'practice': str(practice)})


def crop(directory, name, rect, offset=(0, 0)):
    directory = Path(directory)
    if name not in PARTS:
        raise ValueError('未知の校正部品です。')
    with Image.open(directory/'screen.png') as screen:
        x1, y1, x2, y2 = rect
        if not (0 <= x1 < x2 <= screen.width and 0 <= y1 < y2 <= screen.height):
            raise ValueError('切り抜き範囲が画面外です。')
        image = screen.crop(rect)
        if image.convert('L').getextrema()[0] == image.convert('L').getextrema()[1]:
            raise ValueError('単色画像は照合用部品に使えません。')
        image.save(directory/f'{name}.png')
        screen.save(directory/f'{name}.observed.png')
    profile = read_json(directory/'profile.json')
    profile['verified'] = False
    profile['parts'][name] = {'image': f'{name}.png', 'click_offset': list(offset),
                              'observed': f'{name}.observed.png'}
    write_json(directory/'profile.json', profile)
    if min(x2-x1, y2-y1) < SMALL_PART:
        return [f'{name} は小さい部品画像です。周辺の特徴を含めて切り抜くと、別の場所への誤一致を避けられます。']
    return []


def click(desktop, directory, point):
    guard_practice(desktop, directory)
    screen = desktop.capture()
    if len(point) != 2 or not (0 <= point[0] < screen.width and 0 <= point[1] < screen.height):
        raise ValueError('クリック位置が画面外です。')
    desktop.guard_point(point)
    desktop.click(point, modal=True)
    desktop.guard(modal=True)
    desktop.capture().save(Path(directory)/'screen.png')


def guard_practice(desktop, directory):
    profile = read_json(Path(directory)/'profile.json')
    practice = Path(profile['practice']).resolve()
    work = Path(profile.get('work', practice.parent.parent)).resolve()
    if not practice.is_relative_to(work/'project') or not practice.is_file():
        raise EnvironmentUnavailable('練習用コピーを確認できません。')
    session = profile.get('session')
    if session and session != desktop.identity():
        raise EnvironmentUnavailable('クリスタまたは画面設定が変わりました。校正をやり直してください。')
    desktop.document = practice.stem
    desktop.guard(modal=True)
    return profile


def shot(desktop, directory, name):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('撮影名には英数字・下線・ハイフンを使ってください。')
    if name in PARTS or name in ('profile',):
        raise ValueError('部品名と同じ撮影名は使えません。')
    guard_practice(desktop, directory)
    screen = desktop.capture()
    screen.save(Path(directory)/f'{name}.png')
    screen.save(Path(directory)/'screen.png')


def check(desktop, directory, plan, work, tolerance='medium'):
    directory = Path(directory)
    profile = read_json(directory/'profile.json')
    profile['verified'] = False
    write_json(directory/'profile.json', profile)
    if any(name not in profile['parts'] for name in PARTS):
        raise EnvironmentUnavailable('校正部品が不足しています。')
    # Mutually exclusive states/menus are checked when opened, not all on one screen.
    for name, item in profile['parts'].items():
        with Image.open(directory/item['image']) as template:
            if template.convert('L').getextrema()[0] == template.convert('L').getextrema()[1]:
                raise EnvironmentUnavailable('校正画像が単色です。')
            with Image.open(directory/item['observed']) as screen:
                if match(screen, template)[0] < .9:
                    raise EnvironmentUnavailable('校正部品の相関が不足しています。')
                if name not in REPEATING and len(matches(screen, template, .9)) > 1:
                    raise EnvironmentUnavailable(f'校正部品 {name} が画面の複数箇所に一致します。周辺を含めて採り直してください。')
    ui = object.__new__(UI)
    ui.desktop, ui.directory, ui.profile = desktop, directory, profile
    ui.work_root = Path(work).resolve()
    ui.match_threshold = .9
    original_locate = ui.locate
    # The "discard" button changes look under the pointer, so it only needs a looser match.
    ui.locate = lambda name, threshold=.9: original_locate(name, .8 if name == 'discard_button' else max(.9, threshold))
    if len(plan['panels']) != 1:
        raise ValueError('校正自己テストは1コマで実行してください。')
    marker = Path(work)/'pages'/f"page{plan['page_number']:04d}.done"
    if marker.exists():
        raise ValueError('自己テスト済みのページです。新しい練習用コピーを指定してください。')
    if profile.get('practice'):
        guard_practice(desktop, directory)
    if desktop.document:
        if desktop.document != Path(plan['clip_path']).stem:
            raise EnvironmentUnavailable('練習用ページ以外が開いています。')
        ui.close()
    from .assets import prepare_assets
    result = import_page(plan, ui, work, False,
                         asset_preparer=prepare_assets if plan.get('source') else None, tolerance=tolerance)
    if result['status'] != '成功':
        raise EnvironmentUnavailable('校正自己テストに失敗しました。')
    profile['verified'] = True
    write_json(directory/'profile.json', profile)
    return result
