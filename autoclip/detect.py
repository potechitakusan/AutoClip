import cv2
import numpy as np
from PIL import Image
from .warnings import compact_warnings


def flatten_on_white(image):
    rgba = image.convert('RGBA')
    return Image.alpha_composite(Image.new('RGBA', rgba.size, 'white'), rgba).convert('RGB')


def rectangle(box):
    x1, y1, x2, y2 = box
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def is_rectangle(poly):
    if len(poly) != 4:
        return False
    return all(a[0] == b[0] or a[1] == b[1] for a, b in zip(poly, poly[1:]+poly[:1]))


def detect(path):
    with Image.open(path) as source:
        rgb = flatten_on_white(source)
        gray = np.asarray(rgb.convert('L'))
        transparent = source.mode in ('RGBA', 'LA', 'PA') or 'transparency' in source.info
    h, w = gray.shape
    mask = (gray < 225).astype(np.uint8)*255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    panels, warnings = [], []
    if transparent:
        warnings.append('透明背景を白に合成しました。')
    for contour in contours:
        if cv2.contourArea(contour) < w*h*.03:
            continue
        x, y, cw, ch = cv2.boundingRect(contour)
        box = [x, y, x+cw, y+ch]
        approx = cv2.approxPolyDP(contour, cv2.arcLength(contour, True)*.006, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            poly = approx[:, 0, :].tolist()
        else:
            poly = rectangle(box)
            warnings.append('四角形でない輪郭を外接矩形に置き換えました。')
        panels.append({'outer_px': box, 'source_polygon': poly})
    if not panels:
        panels = [{'outer_px': [0, 0, w, h], 'source_polygon': rectangle([0, 0, w, h])}]
        warnings.append('コマを検出できないためページ全体を1コマにしました。')
    return {'source': str(path), 'source_size': [w, h], 'panels': reading_order(panels),
            'warnings': compact_warnings(warnings)}

def reading_order(panels):
    """Group by top edge, then right-to-left; explicitly only a heuristic."""
    rows = []
    for panel in sorted(panels, key=lambda p: p["outer_px"][1]):
        x1, y1, x2, y2 = panel["outer_px"]
        row = next((r for r in rows if abs(r[0]["outer_px"][1] - y1) < min(
            r[0]["outer_px"][3] - r[0]["outer_px"][1], y2 - y1) * .2), None)
        if row is None:
            rows.append([panel])
        else:
            row.append(panel)
    ordered = [p for row in rows for p in sorted(row, key=lambda p: -p["outer_px"][0])]
    for i, panel in enumerate(ordered, 1):
        panel["reading_order"] = i
    return ordered
