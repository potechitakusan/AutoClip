import argparse
import traceback
from pathlib import Path
from .config import ROOT, save_job, write_json, read_json, natural_key, error_message, sha256, job_directory
from .clip import copy_project, canvas_geometry, inspect_project
from .detect import detect
from .plan import make_plan, FITS
from .report import render_review, report, render_trial
from .desktop import Desktop
from .ui import UI, TOLERANCE
from .importer import import_page
from .upscale import prepare_assets, quality_warnings
from .calibrate import PARTS
from .setup import setup


def completed(work, number, target):
    try:
        done = read_json(Path(work)/'pages'/f'page{number:04d}.done')
        return done if done['sha256'] == sha256(target) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def review_result(output):
    import ctypes
    import os
    import webbrowser
    if os.name != 'nt':
        raise ValueError('確認ダイアログにはWindowsが必要です。')
    webbrowser.open((output/'index.html').resolve().as_uri())
    return ctypes.windll.user32.MessageBoxW(None,
        '解析結果と警告ページをすべて確認してください。\n確認後「はい」で投入を開始します。\n'
        '実行中はキーボードとマウスに触れないでください。',
        'AutoClip v2：解析結果の確認', 0x4 | 0x100 | 0x40000) == 6


def run(args, root=ROOT):
    job = save_job(args, root)
    work = Path(job['work'])
    project = copy_project(job['cmc'], work)
    job['project'] = project['cmc']
    write_json(work/'job.json', job)
    images = sorted((p for p in Path(job['images']).iterdir()
                     if p.suffix.lower() in ('.png', '.jpg', '.jpeg') and p.is_file()),
                    key=lambda p: natural_key(p.name))
    if not images:
        raise ValueError('画像が見つかりません。')
    if len(images) > len(project['pages']):
        raise ValueError('CMCのページ数が画像枚数より少ないです。')
    if args.page is not None and not 1 <= args.page <= len(images):
        raise ValueError('ページ番号が範囲外です。')
    output = root/'output'/job['name']
    results, plans = [], []
    # Trial page: a fresh job builds page 1 only, so a wrong calibration shows before the whole work is spent.
    trial = (not args.dry_run and not args.all and args.page is None
             and not any(completed(work, n, page['path']) for n, page in enumerate(project['pages'][:len(images)], 1)))
    for n, (source, page) in enumerate(zip(images, project['pages']), 1):
        if (args.page is not None and n != args.page) or (trial and n != 1):
            continue
        done = completed(work, n, page['path']) if not args.dry_run else None
        if done:
            results.append({'page_number': n, 'panels': done['panels'], 'status': '完了済み',
                            'warnings': done.get('warnings', []),
                            'rounding_error_px': done.get('rounding_error_px', 0)})
            continue
        try:
            detection = detect(source)
            geometry = canvas_geometry(work/'backups'/Path(page['path']).name)
            plan = make_plan(detection, geometry, n, page['path'], work/'assets', job['fit'])
            quality_warnings(plan, job)
            write_json(work/'pages'/f'page{n:04d}.plan.json', plan)
            render_review(plan, output)
            plans.append(plan)
            results.append({'page_number': n, 'panels': len(plan['panels']),
                            'status': '計画済み', 'warnings': plan['warnings']})
        except Exception:
            (work/'logs').mkdir(parents=True, exist_ok=True)
            with (work/'logs'/'analysis.log').open('a', encoding='utf-8') as log:
                log.write(f'ページ{n}\n'+traceback.format_exc())
            results.append({'page_number': n, 'panels': 0, 'status': '失敗',
                            'reason': '画像または用紙情報を読めません。詳細はログを参照してください。'})
    report(job, results, output)
    if not args.dry_run and plans:
        if job['review'] and not review_result(output):
            raise ValueError('確認が取り消されました。投入していません。')
        print('画面操作中はキーボードとマウスに触れないでください。')
        desktop = Desktop()
        ui = UI(desktop, root/'profile/ui', root/'work')
        desktop.focus()
        try:
            by_number = {r['page_number']: r for r in results}
            for plan in plans:
                try:
                    result = import_page(plan, ui, work, job['frames_only'],
                        asset_preparer=lambda p, g: prepare_assets(p, g, job, root),
                        tolerance=job['tolerance'], snapshot=output/'trial_screen.png' if trial else None)
                except Exception as exc:
                    by_number[plan['page_number']].update(status='失敗', reason=error_message(exc))
                    raise
                by_number[plan['page_number']].update(result)
                if trial and result['status'] == '成功':
                    render_trial(plan, output)
                report(job, results, output)
            desktop.minimize()
        except Exception:
            report(job, results, output)
            raise
    print(report(job, results, output))
    if trial and results and results[0]['status'] == '成功':
        print(f'試運転：1ページ目だけ処理しました。{output/"trial.png"} を見て、枠の数・位置・形と画像の収まりを'
              f'確認してから、残りは autoclip run --job {job["name"]} で処理します。')
    return 0


def status(name, root=ROOT):
    work = job_directory(name, root)
    job = read_json(work/'job.json')
    project = inspect_project(job.get('project', job['cmc']))
    images = [p for p in Path(job['images']).iterdir() if p.is_file() and p.suffix.lower() in ('.png', '.jpg', '.jpeg')]
    count = sum(bool(completed(work, n, page['path']))
                for n, page in enumerate(project['pages'][:len(images)], 1))
    lines = [f'作業 {name}：完了 {count} / {len(images)}ページ']
    summary = root/'output'/name/'summary.json'
    if summary.is_file():
        for result in read_json(summary)['pages']:
            n = result['page_number']
            if result['status'] == '失敗' and not completed(work, n, project['pages'][n-1]['path']):
                lines.append(f"ページ{n}：失敗・スキップ / {result.get('reason', '投入を完了できませんでした。')}")
    if count == len(images):
        lines.append('次：処理済みです。CMCとページCLIPを含む管理フォルダー全体を渡してください。')
    else:
        try:
            profile = read_json(root/'profile/ui/profile.json')
        except (ValueError, OSError):
            profile = None
        if profile is None:
            next_command = f'autoclip setup prepare --cmc "{job["cmc"]}"'
        elif not profile.get('verified'):
            next_command = 'autoclip setup check'
        else:
            next_command = f'autoclip run --job {name}'
        lines.append('次の1コマンド：'+next_command)
    lines.append(f'作業用CMC：{job.get("project", "未作成")}')
    print('\n'.join(lines))
    return 0


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description='AutoClip v2')
    sub = parser.add_subparsers(dest='command', required=True)
    model = sub.add_parser('fetch-model')
    model.add_argument('--dest', default='models')
    command = sub.add_parser('run')
    command.add_argument('images', nargs='?')
    command.add_argument('--cmc')
    command.add_argument('--job')
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--page', type=int)
    command.add_argument('--upscale', choices=('none', 'builtin', 'external'))
    command.add_argument('--frames-only', action='store_true', default=None)
    command.add_argument('--review', action='store_true', default=None)
    command.add_argument('--python')
    command.add_argument('--model')
    command.add_argument('--long-edge', choices=('auto',))
    command.add_argument('--tolerance', choices=TOLERANCE, help='用紙領域の縦横比の許容誤差（既定 medium。ignore は検査しない）')
    command.add_argument('--fit', choices=FITS, help='cover（既定）=縦横比を保って基本枠を埋め、はみ出しは隠す / stretch=縦横を別々に合わせる')
    command.add_argument('--all', action='store_true', help='試運転（先頭1ページだけ）を省いて全ページを処理する')
    state = sub.add_parser('status')
    state.add_argument('--job', required=True)
    calibration = sub.add_parser('setup')
    calibration.add_argument('--recalibrate', action='store_true')
    steps = calibration.add_subparsers(dest='step', required=True)
    prepare = steps.add_parser('prepare')
    prepare.add_argument('--cmc', required=True)
    prepare.add_argument('--recalibrate', action='store_true', default=argparse.SUPPRESS)
    shot = steps.add_parser('shot')
    shot.add_argument('name')
    crop = steps.add_parser('crop')
    crop.add_argument('--name', required=True, choices=PARTS)
    crop.add_argument('--rect', required=True, nargs=4, type=int)
    crop.add_argument('--click-offset', nargs=2, type=int, default=[0, 0])
    click = steps.add_parser('click')
    click.add_argument('x', type=int)
    click.add_argument('y', type=int)
    click.add_argument('more', nargs='*', type=int, help='続けてクリックする X Y の組（メニューは1回の呼び出しで開く必要があるため）')
    check = steps.add_parser('check')
    check.add_argument('--tolerance', choices=TOLERANCE)
    args = parser.parse_args(argv)
    try:
        if args.command == 'fetch-model':
            from .fetch_model import fetch
            print(fetch(args.dest))
            return 0
        if args.command == 'run':
            return run(args, root)
        if args.command == 'status':
            return status(args.job, root)
        return setup(args, root)
    except Exception as exc:
        from .config import job_directory
        try:
            log_directory = job_directory(getattr(args, 'job', None) or 'v2-errors', root)/'logs'
            log_directory.mkdir(parents=True, exist_ok=True)
            (log_directory/'command.log').write_text(traceback.format_exc(), encoding='utf-8')
        except (ValueError, OSError):
            pass
        print(error_message(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
