"""Synthetic manga pages for tests: black-bordered rectangular panels with simple artwork.

Nothing here is real manga, so these pages can be approved by tests and used in public examples.
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

LAYOUTS = (
    # panels as fractions of the working area: (x1, y1, x2, y2)
    [(0, 0, .5, .5), (.5, 0, 1, .5), (0, .5, .5, 1), (.5, .5, 1, 1)],
    [(0, 0, 1, .4), (0, .4, .5, 1), (.5, .4, 1, 1)],
    [(0, 0, .6, .5), (.6, 0, 1, .5), (0, .5, 1, 1)],
    [(0, 0, 1, .33), (0, .33, 1, .66), (0, .66, 1, 1)],
    [(0, 0, .5, .3), (.5, 0, 1, .3), (0, .3, 1, .7), (0, .7, .5, 1), (.5, .7, 1, 1)],
)


def make_page(path, size=(1819, 2551), variant=0, seed=0):
    rng = random.Random(seed)
    width, height = size
    page = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(page)
    margin_x, margin_y, gutter, border = round(width * .06), round(height * .06), round(width * .02), 6
    area = (margin_x, margin_y, width - margin_x, height - margin_y)
    for fx1, fy1, fx2, fy2 in LAYOUTS[variant % len(LAYOUTS)]:
        x1 = round(area[0] + (area[2] - area[0]) * fx1 + (gutter / 2 if fx1 else 0))
        y1 = round(area[1] + (area[3] - area[1]) * fy1 + (gutter / 2 if fy1 else 0))
        x2 = round(area[0] + (area[2] - area[0]) * fx2 - (gutter / 2 if fx2 < 1 else 0))
        y2 = round(area[1] + (area[3] - area[1]) * fy2 - (gutter / 2 if fy2 < 1 else 0))
        tint = tuple(rng.randint(235, 252) for _ in range(3))
        draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=tint)
        for _ in range(6):  # a few dark strokes so the interior is not blank
            ax, ay = rng.randint(x1 + 40, x2 - 40), rng.randint(y1 + 40, y2 - 40)
            bx, by = rng.randint(x1 + 40, x2 - 40), rng.randint(y1 + 40, y2 - 40)
            draw.line((ax, ay, bx, by), fill=(40, 40, 40), width=rng.randint(3, 9))
        draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="black", width=border)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        page.save(path, quality=95)
    else:
        page.save(path)
    return path


def make_pages(folder, count, size=(1819, 2551), jpeg_every=0):
    folder = Path(folder)
    paths = []
    for i in range(1, count + 1):
        suffix = ".jpg" if jpeg_every and i % jpeg_every == 0 else ".png"
        paths.append(make_page(folder / f"page-{i:03d}{suffix}", size, variant=i - 1, seed=i))
    return paths


def make_project(folder, pages=3, canvas_mm=(182.0, 257.0), dpi=350.0, trim_mm=(148.0, 210.0), bleed_mm=5.0,
                 basic_mm=(125.0, 180.0), name="project"):
    """A minimal CMC plus page CLIPs that only contain the metadata the read-only inspector uses."""
    import sqlite3
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    def database(statements):
        con = sqlite3.connect(":memory:")
        for statement, args in statements:
            con.execute(statement, args)
        con.commit()
        data = con.serialize()
        con.close()
        return data

    nodes = [("CREATE TABLE Project(_PW_ID INTEGER PRIMARY KEY, ProjectRootCanvasNode INTEGER)", ()),
             ("CREATE TABLE CanvasNode(_PW_ID INTEGER PRIMARY KEY, Type INTEGER, LinkPath TEXT, FirstChildIndex INTEGER, NextIndex INTEGER)", ()),
             ("INSERT INTO Project VALUES(1, 1)", ()), ("INSERT INTO CanvasNode VALUES(1, 1, NULL, 2, 0)", ())]
    for number in range(1, pages + 1):
        nodes.append(("INSERT INTO CanvasNode VALUES(?, 2, ?, 0, ?)",
                      (number + 1, f".:page{number:04d}.clip", number + 2 if number < pages else 0)))
    (folder / f"{name}.cmc").write_bytes(database(nodes))
    columns = ("CanvasWidth,CanvasHeight,CanvasUnit,CanvasResolution,CropFrameWidth,CropFrameHeight,CropFrameDitch,"
               "ComicPageIndex,CropFrameInnerWidth,CropFrameInnerHeight,CropFrameInnerOffsetX,CropFrameInnerOffsetY,"
               "CropFrameUnitKind,CropFrameShow,CropFrameCropOffsetX,CropFrameCropOffsetY,CropFrameInnerOffsetBasePosition")
    for number in range(1, pages + 1):
        values = (*canvas_mm, 2, dpi, *trim_mm, bleed_mm, number - 1, *basic_mm, 0, 0, 2, 1, 0, 0, 0)
        statements = [(f"CREATE TABLE Canvas({columns})", ()),
                      (f"INSERT INTO Canvas VALUES({','.join('?' * len(values))})", values),
                      ("CREATE TABLE Layer(MainId INTEGER, LayerName TEXT, LayerType INTEGER, LayerFolder INTEGER, "
                       "LayerFirstChildIndex INTEGER, LayerNextIndex INTEGER, ComicFrameLineMipmap INTEGER, VectorNormalType INTEGER)", ()),
                      ("INSERT INTO Layer VALUES(1, '', 0, 1, 2, 0, NULL, NULL)", ()),
                      ("INSERT INTO Layer VALUES(2, 'Paper', 0, 0, 0, 0, NULL, NULL)", ())]
        (folder / f"page{number:04d}.clip").write_bytes(database(statements))
    return folder / f"{name}.cmc"


def make_transparent_page(path, size=(1819, 2551), variant=0, seed=0):
    """Same page as make_page, but with a transparent paper: only the ink is opaque."""
    from PIL import ImageChops, ImageOps
    path = Path(path)
    white = make_page(path.with_suffix(".tmp.png"), size, variant, seed)
    with Image.open(white) as page:
        alpha = ImageOps.invert(page.convert("L")).point(lambda v: 0 if v < 24 else min(255, v * 4))
    white.unlink()
    ink = Image.new("RGBA", size, (30, 30, 30, 0))
    ink.putalpha(alpha)
    ink.save(path)
    return path


def diagonal_polygons(variant='horizontal'):
    """Integer, clockwise quadrilaterals in a 100x100 working region."""
    if variant == 'horizontal':
        return [[[0,0],[100,0],[100,60],[0,40]], [[0,50],[100,70],[100,100],[0,100]]]
    if variant == 'vertical':
        return [[[0,0],[40,0],[60,100],[0,100]], [[50,0],[100,0],[100,100],[70,100]]]
    if variant == 'nested':
        return [[[0,0],[40,0],[40,48],[0,40]], [[50,0],[100,0],[100,60],[50,50]],
                [[0,50],[100,70],[100,100],[0,100]]]
    if variant == 'windmill':
        return [[[0,0],[60,0],[50,25],[0,30]], [[70,0],[100,0],[100,60],[75,50]],
                [[100,70],[100,100],[40,100],[50,75]], [[30,100],[0,100],[0,40],[25,50]]]
    raise ValueError(variant)


def make_diagonal_page(path, variant='horizontal', transparent=False):
    page = Image.new('RGBA' if transparent else 'RGB',(600,840),
                     (255,255,255,0) if transparent else 'white')
    draw = ImageDraw.Draw(page)
    for poly in diagonal_polygons(variant):
        # The fixtures, like make_page, describe half-open outer rectangles.
        xmax,ymax = max(p[0] for p in poly),max(p[1] for p in poly)
        points = [(40+5*x-(x == xmax),40+7.6*y-(y == ymax)) for x,y in poly]
        draw.polygon(points,outline='black',width=6)
    page.save(path)
    return Path(path)


def frame_vector_chunks(polygons):
    """Build synthetic external chunks for the observed quadrilateral vector encoding."""
    import struct
    def chunk(name,body):
        return name+struct.pack('>Q',len(body))+body
    result = b''
    for ident,points in polygons.items():
        payload = bytearray(452)
        for k,(x,y) in enumerate(points):
            struct.pack_into('>dd',payload,100+88*k,x+.001,y+.001)
        ref = f'vector-{ident}'.encode('ascii')
        body = struct.pack('>Q',len(ref))+ref+struct.pack('>Q',len(payload))+payload
        result += chunk(b'CHNKExta',body)
    return result
