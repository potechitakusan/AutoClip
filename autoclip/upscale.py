"""外接矩形だけを拡縮し、CSPのコマ枠にクリップを任せる。"""
import math
from pathlib import Path
from PIL import Image
from .detect import flatten_on_white
from .config import ROOT
from .warnings import compact_warnings, quality_warning
import subprocess


class UpscaleFailed(ValueError):
    """モデル失敗はGUIの再試行をせず、そのページだけスキップする。"""


def required_magnification(panel):
    source_x, source_y = zip(*panel['source_polygon'])
    canvas_x, canvas_y = zip(*panel['canvas_polygon'])
    return max((max(canvas_x)-min(canvas_x))/(max(source_x)-min(source_x)),
               (max(canvas_y)-min(canvas_y))/(max(source_y)-min(source_y)))


def target_long_edge(size, needed, policy):
    # operations.target_long_edge の auto 方針。x4のネイティブ倍率と最大辺を上限にする。
    if policy.get('upscale', 'none') == 'none' or needed <= 1.25:
        return None
    edge = min(math.ceil(max(size)*needed), max(size)*4, 16384)
    return edge + edge % 2


def output_size(size, long_edge):
    # upscale.output_size と同じ丸め。
    return tuple(max(1, round(v*long_edge/max(size))) for v in size)


def quality_warnings(plan, policy):
    warnings = list(plan['warnings'])
    if not policy.get('frames_only') and policy.get('upscale', 'none') == 'none':
        affected = []
        for panel in plan['panels']:
            needed = required_magnification(panel)
            if needed > 1.25:
                affected.append((panel['folder_name'].rsplit('_', 1)[-1], needed))
        if affected:
            warning = quality_warning(affected, len(plan['panels']))
            if warning not in warnings:
                warnings.append(warning)
    plan['warnings'] = compact_warnings(warnings, len(plan['panels']))


def runner_command(policy, source, target, edge, root=ROOT):
    mode = policy['upscale']
    python = Path(policy.get('python') or '') if mode == 'external' else root/'.venv/Scripts/python.exe'
    model = Path(policy.get('model') or '')
    if not python.is_file() or not model.is_file():
        raise UpscaleFailed('拡大用Pythonまたはモデルがありません。指定パスを確認して再実行してください。')
    common = ['--input', str(source), '--output', str(target), '--model', str(model),
              '--long-edge', str(edge), '--tile', '256', '--pad', '32',
              '--device', 'cuda', '--precision', 'fp32']
    return [str(python), '-B', '-s', str(root/'scripts/upscale_runner.py'), *common]


def upscale_crop(image, asset_path, needed, policy, root=ROOT):
    source = asset_path.parent/'crops'/asset_path.name
    target = asset_path.parent/'upscaled'/asset_path.name
    source.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    edge = target_long_edge(image.size, needed, policy)
    try:
        image.save(source)
        command = runner_command(policy, source, target, edge, root)
        done = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
        # 推論の英語出力はログだけに保存。外部Pythonは -B -s で読取利用する。
        target.with_suffix('.log').write_text(done.stdout+'\n'+done.stderr, encoding='utf-8')
        if done.returncode:
            raise UpscaleFailed('コマ画像の拡大に失敗しました。詳細は拡大ログを参照してください。')
        with Image.open(target) as result:
            result.load()
            if result.size != output_size(image.size, edge):
                raise UpscaleFailed('拡大画像の寸法が合いません。')
            return result.convert('RGB')
    except UpscaleFailed:
        raise
    except Exception as exc:
        raise UpscaleFailed('コマ画像の拡大を完了できません。指定パスと拡大ログを確認してください。') from exc


def prepare_assets(plan, canvas_px_per_screen_px=0, policy=None, root=ROOT):
    policy = policy or {"upscale": "none"}
    margin = 12 + math.ceil(canvas_px_per_screen_px)
    with Image.open(plan['source']) as source:
        source = flatten_on_white(source).convert('RGBA')
    width, height = plan['canvas_size']
    for panel in plan['panels']:
        sx, sy = panel['scale']
        if sx <= 0 or sy <= 0:
            raise ValueError('コマの拡縮率が不正です。')
        xs, ys = zip(*panel['source_polygon'])
        cx, cy = panel['canvas_polygon'][0]
        px, py = panel['source_polygon'][0]
        tx, ty = cx-px*sx, cy-py*sy
        # 座標を先に丸め、逆写像した矩形をLANCZOSで同じ位置へ写す。
        left = max(0, math.floor(min(xs)*sx+tx-margin))
        top = max(0, math.floor(min(ys)*sy+ty-margin))
        right = min(width, math.ceil(max(xs)*sx+tx+margin))
        bottom = min(height, math.ceil(max(ys)*sy+ty+margin))
        if left >= right or top >= bottom:
            raise ValueError('コマ画像の配置範囲が用紙外です。')
        box = ((left-tx)/sx, (top-ty)/sy, (right-tx)/sx, (bottom-ty)/sy)
        # ソース端を超える余白は端の画素を延長する。ページ外の文字レイヤーは作らない。
        x1, y1, x2, y2 = box
        pad = [max(0, math.ceil(-x1)), max(0, math.ceil(-y1)),
               max(0, math.ceil(x2-source.width)), max(0, math.ceil(y2-source.height))]
        expanded = source
        if any(pad):
            import numpy as np
            expanded = Image.fromarray(np.pad(np.asarray(source),
                ((pad[1], pad[3]), (pad[0], pad[2]), (0, 0)), mode='edge'))
        box = (x1+pad[0], y1+pad[1], x2+pad[0], y2+pad[1])
        needed = required_magnification(panel)
        if target_long_edge((math.ceil(x2-x1), math.ceil(y2-y1)), needed, policy) is not None:
            bounds = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]))
            raw = expanded.crop(bounds).convert('RGB')
            enlarged = upscale_crop(raw, Path(panel['image_path']), needed, policy, root)
            ux, uy = enlarged.width/raw.width, enlarged.height/raw.height
            box = ((box[0]-bounds[0])*ux, (box[1]-bounds[1])*uy,
                   (box[2]-bounds[0])*ux, (box[3]-bounds[1])*uy)
            expanded = enlarged.convert('RGBA')
        crop = expanded.resize((right-left, bottom-top), Image.Resampling.LANCZOS, box=box)
        canvas = Image.new('RGBA', (width, height))
        canvas.paste(crop, (left, top))
        path = Path(panel['image_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(path, dpi=(plan['dpi'], plan['dpi']))
    return margin
