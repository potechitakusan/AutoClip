from pathlib import Path
from .detect import is_rectangle
from .warnings import compact_warnings

FITS = ('cover', 'stretch')
# Below this share of the page hidden by `cover`, the crop is too small to be worth a warning.
HIDDEN_WARNING = .10


def clip_polygon(points, rect):
    """Sutherland-Hodgman clip of a polygon to an axis-aligned rectangle."""
    x1, y1, x2, y2 = rect
    edges = [(lambda p: p[0] >= x1, lambda a, b: (x1, a[1]+(b[1]-a[1])*(x1-a[0])/(b[0]-a[0]))),
             (lambda p: p[0] <= x2, lambda a, b: (x2, a[1]+(b[1]-a[1])*(x2-a[0])/(b[0]-a[0]))),
             (lambda p: p[1] >= y1, lambda a, b: (a[0]+(b[0]-a[0])*(y1-a[1])/(b[1]-a[1]), y1)),
             (lambda p: p[1] <= y2, lambda a, b: (a[0]+(b[0]-a[0])*(y2-a[1])/(b[1]-a[1]), y2))]
    result = [tuple(p) for p in points]
    for inside, cross in edges:
        points, result = result, []
        for i, current in enumerate(points):
            previous = points[i-1]
            if inside(current):
                if not inside(previous):
                    result.append(cross(previous, current))
                result.append(current)
            elif inside(previous):
                result.append(cross(previous, current))
        if not result:
            return []
    cleaned = [p for i, p in enumerate(result) if p != result[i-1]]
    return [list(p) for p in cleaned]


def polygon_area(points):
    return abs(sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(points, points[1:]+points[:1])))/2


def make_plan(detection, geometry, number, clip_path, assets, fit='cover'):
    """Place the detected panels on the basic frame.

    cover   : one scale for both axes, so the picture keeps its aspect ratio and fills the frame without gaps.
              What does not fit is hidden: panels are clipped to the basic frame.
    stretch : each axis is fitted to the frame on its own (the picture is distorted if the aspect ratios differ).
    """
    if fit not in FITS:
        raise ValueError('配置方式は cover / stretch から選んでください。')
    panels = detection['panels']
    vertices = [v for p in panels for v in p['source_polygon']]
    bounds = [min(v[0] for v in vertices), min(v[1] for v in vertices),
              max(v[0] for v in vertices), max(v[1] for v in vertices)]
    x1, y1, x2, y2 = geometry['basic_frame_px']
    width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
    if fit == 'cover':
        sx = sy = max((x2-x1)/width, (y2-y1)/height)
        tx = (x1+x2)/2-(bounds[0]+bounds[2])/2*sx
        ty = (y1+y2)/2-(bounds[1]+bounds[3])/2*sy
    else:
        sx, sy = (x2-x1)/width, (y2-y1)/height
        tx, ty = x1-bounds[0]*sx, y1-bounds[1]*sy
    warnings = list(detection['warnings'])+list(geometry['warnings'])
    hidden = max(1-(x2-x1)/(width*sx), 1-(y2-y1)/(height*sy))
    if fit == 'cover' and hidden >= HIDDEN_WARNING:
        axis = '左右' if (x2-x1)/(width*sx) < (y2-y1)/(height*sy) else '上下'
        warnings.append(f'元画像と用紙の縦横比が違うため、縦横比を保って基本枠を埋めます（{axis}の約{hidden*100:.0f}%が隠れます）。')
    result = []
    for i, panel in enumerate(panels, 1):
        name = f'AC_page{number:04d}_p{i:03d}'
        poly = [[x*sx+tx, y*sy+ty] for x, y in panel['source_polygon']]
        item = {**panel, 'canvas_polygon': poly, 'folder_name': name,
                'rectangle': is_rectangle(panel['source_polygon']),
                'image_path': str(Path(assets)/f'{name}.png'), 'scale': [sx, sy]}
        if fit == 'cover':
            # The picture keeps the full polygon; only the drawn frame is cut to the basic frame.
            clipped = clip_polygon(poly, [x1, y1, x2, y2])
            if len(clipped) < 3 or polygon_area(clipped) < 4:
                warnings.append(f'{name} は基本枠の外にあるため作成しません。')
                continue
            item['frame_polygon'] = clipped
        result.append(item)
    if not result:
        raise ValueError('コマが基本枠の中に残りません。')
    return {'page_number': number, 'clip_path': str(clip_path), 'source': detection['source'],
            'source_size': detection['source_size'], 'canvas_size': geometry['size_px'],
            'dpi': geometry['dpi'], 'frame_line_px': .4*geometry['dpi']/25.4, 'panels': result,
            'warnings': compact_warnings(warnings),
            'detection_correction_px': 0}
