"""ページ単位の警告文を、保存時と表示時で共通化する。"""
import re


def quality_warning(affected, panel_count):
    """affected は (コマ番号, 必要倍率) の列。"""
    low = min(value for _, value in affected)
    high = max(value for _, value in affected)
    low, high = (f'{value:.2f}'.rstrip('0').rstrip('.') for value in (low, high))
    magnification = low if low == high else f'{low}〜{high}'
    if len(affected) == panel_count:
        scope = f'{panel_count}コマとも'
    else:
        names = '・'.join(name for name, _ in affected)
        scope = f'{len(affected)}コマ（{names}）'
    return (f'画質が低下します：{scope}必要倍率{magnification}倍'
            '（拡大すると改善します：--upscale builtin/external）')


def compact_warnings(warnings, panel_count=0):
    """旧完了記録のコマ別画質警告も扱い、再集約しても件数を保つ。"""
    counts, affected = {}, {}
    quality_key = None
    for warning in warnings:
        old = re.fullmatch(r'AC_page\d+_(p\d+)：必要倍率 ([\d.]+)倍、画質が低下します。', warning)
        if old:
            if quality_key is None:
                quality_key = warning
                counts[quality_key] = 0
            affected[old[1]] = float(old[2])
            continue
        counted = re.fullmatch(r'(.*)（(\d+)件）', warning)
        text, count = (counted[1], int(counted[2])) if counted else (warning, 1)
        counts[text] = counts.get(text, 0) + count
    result = []
    for text, count in counts.items():
        if text == quality_key:
            result.append(quality_warning(sorted(affected.items()), panel_count))
        else:
            result.append(text if count == 1 else f'{text}（{count}件）')
    return result
