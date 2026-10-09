import html
from pathlib import Path
from PIL import Image, ImageDraw
from .detect import flatten_on_white
from .config import write_json, read_json
from .warnings import compact_warnings


def displayed_warnings(result):
    warnings = list(result.get('warnings', []))
    error = result.get('rounding_error_px', 0)
    if error > 4:
        warnings.append(f'枠の位置誤差は最大およそ{error:.1f}px（画面倍率のため）')
    return warnings


def render_review(plan, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with Image.open(plan['source']) as image:
        image = flatten_on_white(image)
    image.thumbnail((1100, 1500))
    sx, sy = image.width/plan['source_size'][0], image.height/plan['source_size'][1]
    draw = ImageDraw.Draw(image)
    for i, panel in enumerate(plan['panels'], 1):
        points = [(round(x*sx), round(y*sy)) for x, y in panel['source_polygon']]
        draw.line(points+points[:1], fill='red', width=3)
        draw.text(points[0], str(i), fill='red', stroke_width=1)
    path = output / f"page{plan['page_number']:04d}.review.png"
    image.save(path)
    return path.name


def render_trial(plan, output):
    """Detected frames (left) next to the paper as drawn on screen (right), for judging the trial page.
    Labels are ASCII because the default PIL font has no Japanese glyphs."""
    output = Path(output)
    with Image.open(output/f"page{plan['page_number']:04d}.review.png") as left,             Image.open(output/'trial_screen.png') as right:
        panels = []
        for image, title in ((left.convert('RGB'), 'LEFT: detected frames (source)'), (right.convert('RGB'), 'RIGHT: paper on screen (drawn, before save)')):
            image = image.resize((max(1, round(image.width*1100/image.height)), 1100))
            panel = Image.new('RGB', (image.width, image.height+36), 'white')
            panel.paste(image, (0, 36))
            ImageDraw.Draw(panel).text((8, 10), title, fill='black')
            panels.append(panel)
        sheet = Image.new('RGB', (sum(p.width for p in panels)+20, panels[0].height), 'gray')
        sheet.paste(panels[0], (0, 0))
        sheet.paste(panels[1], (panels[0].width+20, 0))
    sheet.save(output/'trial.png')
    return 'trial.png'


def report(job, results, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output/'summary.json').is_file():
        previous = read_json(output/'summary.json')['pages']
        combined = {r['page_number']: r for r in previous}
        combined.update({r['page_number']: r for r in results})
        results = [combined[n] for n in sorted(combined)]
    results = [{**r, 'warnings': compact_warnings(r.get('warnings', []), r.get('panels', 0))}
               for r in results]
    write_json(output/'summary.json', {'job': job['name'], 'pages': results})
    rows = []
    for r in results:
        n = r['page_number']
        warnings = ' / '.join(displayed_warnings(r)) or 'なし'
        rows.append(f'<section><h2>ページ{n}：{r.get("panels", 0)}コマ・{html.escape(r["status"])}</h2>'
                    f'<p>警告：{html.escape(warnings)}</p><p>{html.escape(r.get("reason", ""))}</p>'
                    f'<img style="max-width:100%" src="page{n:04d}.review.png" alt="ページ{n}の検出結果"></section>')
    target = str(Path(job['work'])/'project')
    content = '<!doctype html><html lang="ja"><meta charset="utf-8"><title>AutoClip v2 検出結果</title>'
    content += '<h1>検出結果</h1><p>完成先（投入後）：'+html.escape(target)+'。CMCと全ページCLIPを含むフォルダー全体を渡してください。</p>'
    (output/'index.html').write_text(content+''.join(rows)+'</html>', encoding='utf-8')
    succeeded = sum(r['status'] in ('計画済み', '成功', '完了済み') for r in results)
    warned = sum(bool(displayed_warnings(r)) for r in results)
    lines = [f'成功 {succeeded} / スキップ {len(results)-succeeded} / 警告あり {warned}ページ']
    for r in results:
        line = f"ページ{r['page_number']}：{r.get('panels', 0)}コマ・{r['status']}"
        if displayed_warnings(r):
            line += ' / 警告：'+' / '.join(displayed_warnings(r))
        if r.get('reason'):
            line += ' / '+r['reason']+f" / autoclip run --job {job['name']} --page {r['page_number']}"
        lines.append(line)
    lines += [f'確認画像：{output / "index.html"}', f'作業用CMC：{job["project"]}',
              'CMCと全ページCLIPを含む管理フォルダー全体を渡してください。']
    return '\n'.join(lines)
