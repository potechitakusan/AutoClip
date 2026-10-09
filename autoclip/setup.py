"""校正CLI。練習用コピーだけに操作を限定する。"""
import shutil
import uuid
from pathlib import Path
from PIL import Image, ImageDraw
from . import calibrate
from .config import read_json, write_json
from .clip import inspect_project, inspect_clip, canvas_geometry
from .plan import make_plan
from .desktop import Desktop


def setup(args, root):
    directory, work = root/'profile/ui', root/'work/v2-setup'
    if args.step == 'crop':
        warnings = calibrate.crop(directory, args.name, args.rect, args.click_offset)
        print('\n'.join(['部品画像を保存しました。']+['注意：'+w for w in warnings]))
        return 0
    print('校正の画面操作中はキーボードとマウスに触れないでください。')
    desktop = Desktop()
    desktop.focus()
    if args.step == 'prepare':
        if (directory/'profile.json').exists() and not args.recalibrate:
            raise ValueError('校正を作り直す場合は setup prepare --recalibrate --cmc を指定してください。')
        info = inspect_project(args.cmc)
        source = Path(info['pages'][0]['path'])
        if any(r.get('VectorNormalType') == 3 and r.get('ComicFrameLineMipmap') for r in inspect_clip(source)['layers']):
            raise ValueError('練習用にはコマ枠のないページを持つCMCを指定してください。')
        practice = work/'project'/f'AC_setup_{uuid.uuid4().hex[:12]}.clip'
        practice.parent.mkdir(parents=True, exist_ok=True)
        if not practice.resolve().is_relative_to((root/'work').resolve()):
            raise ValueError('校正用コピーの保存先がwork外です。')
        shutil.copy2(source, practice)
        backup = work/'backups'/practice.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        calibrate.prepare(desktop, directory, practice, work)
        profile = read_json(directory/'profile.json')
        numbers = [int(p.stem[4:]) for p in (work/'pages').glob('page*.done') if p.stem[4:].isdigit()]
        profile.update(work=str(work.resolve()), session=desktop.identity(),
                       selftest_page=max(numbers, default=0)+1)
        write_json(directory/'profile.json', profile)
        print(f'練習用ページ：{practice}\n画面：{directory/"screen.png"}')
    elif args.step == 'shot':
        calibrate.shot(desktop, directory, args.name)
        print(f'画面：{directory/"screen.png"}')
    elif args.step == 'click':
        points = [(args.x, args.y)]
        if len(args.more) % 2:
            raise ValueError('追加のクリック位置は X Y の組で指定してください。')
        points += list(zip(args.more[0::2], args.more[1::2]))
        for point in points:
            calibrate.click(desktop, directory, point)
        print(f'画面：{directory/"screen.png"}')
    else:
        profile = calibrate.guard_practice(desktop, directory)
        practice = Path(profile['practice'])
        geometry = canvas_geometry(work/'backups'/practice.name)
        source = work/'assets/setup-color.png'
        source.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new('RGB', (240, 320), 'cyan')
        ImageDraw.Draw(image).rectangle((0, 0, 120, 160), fill='magenta')
        image.save(source)
        detection = {'source': str(source), 'source_size': [240, 320], 'warnings': [],
                     'panels': [{'source_polygon': [[0, 0], [240, 0], [240, 320], [0, 320]]}]}
        plan = make_plan(detection, geometry, profile.get('selftest_page', 1), practice, work/'assets', 'stretch')
        calibrate.check(desktop, directory, plan, work, getattr(args, 'tolerance', None) or 'medium')
        desktop.minimize()
        print('1コマ作成・画像投入・保存・SQLite確認に合格し、校正を検証済みにしました。')
    return 0
