"""ページ単位のバックアップ・一回再試行。.doneだけで再開する。"""
from pathlib import Path
import shutil
import traceback
from .config import read_json, write_json, sha256, now, error_message
from .clip import verify_saved
from .ui import EnvironmentUnavailable, CanvasMismatch
from .upscale import UpscaleFailed


def import_page(plan, ui, work, frames_only=False, verifier=verify_saved, asset_preparer=None,
                tolerance='medium', snapshot=None):
    work = Path(work).resolve()
    target = Path(plan['clip_path']).resolve()
    if not target.is_relative_to(work/'project') or target.is_symlink():
        raise EnvironmentUnavailable('作業コピー以外への投入を拒否しました。')
    number = plan['page_number']
    marker = work/'pages'/f'page{number:04d}.done'
    backup = work/'backups'/target.name
    if not backup.is_file() or backup.is_symlink():
        raise EnvironmentUnavailable('原本バックアップがありません。')
    if marker.exists():
        try:
            if read_json(marker).get('sha256') == sha256(target):
                return {'status': '完了済み', 'rounding_error_px': read_json(marker).get('rounding_error_px', 0)}
        except (ValueError, OSError):
            pass
        marker.unlink()
    # A missing/mismatched done always starts with the original backup.
    ui.restore_guard(target)
    shutil.copy2(backup, target)
    reason = ''
    for attempt in range(2):
        opened = False
        try:
            ui.open_page(target)
            opened = True
            rect = ui.canvas(plan['canvas_size'], tolerance)
            if not frames_only and asset_preparer:
                granularity = max(plan['canvas_size'][0]/(rect[2]-rect[0]),
                                  plan['canvas_size'][1]/(rect[3]-rect[1]))
                asset_preparer(plan, granularity)
            error = 0
            for panel in plan['panels']:
                error = max(error, ui.frame(panel, plan['canvas_size'], rect, plan['frame_line_px'], frames_only))
            if snapshot:
                ui.snapshot(rect, snapshot)
            ui.save(target)
            verifier(target, plan['panels'], frames_only)
            digest = sha256(target)
            ui.close()
            opened = False
            write_json(marker, {'sha256': digest, 'panels': len(plan['panels']), 'at': now(),
                                'rounding_error_px': error, 'warnings': plan.get('warnings', []),
                                'detection_correction_px': plan.get('detection_correction_px', 0)})
            return {'status': '成功', 'rounding_error_px': error}
        except CanvasMismatch:
            # Nothing was drawn: close the untouched tab, restore, and stop the whole run (every page would repeat it).
            try:
                ui.close()
            except Exception as close_error:
                raise EnvironmentUnavailable('作業タブを閉じられないため自動復元を停止しました。') from close_error
            shutil.copy2(backup, target)
            raise
        except EnvironmentUnavailable:
            # Unsafe focus/unknown tab: never restore a file while it might be open.
            raise
        except Exception as exc:
            logs = work/'logs'
            logs.mkdir(parents=True, exist_ok=True)
            (logs/f'page{number:04d}.log').write_text(traceback.format_exc(), encoding='utf-8')
            reason = error_message(exc)
            if opened:
                try:
                    ui.close()
                except Exception as close_error:
                    raise EnvironmentUnavailable('作業タブを閉じられないため自動復元を停止しました。') from close_error
            shutil.copy2(backup, target)
            if isinstance(exc, UpscaleFailed):
                break
    return {'status': '失敗', 'reason': reason}


def import_pages(plans, ui, work, frames_only=False):
    results = []
    for plan in plans:
        result = import_page(plan, ui, work, frames_only)
        results.append({'page_number': plan['page_number'], 'panels': len(plan['panels']),
                        'warnings': plan['warnings'], **result})
    ui.desktop.minimize()
    return results
