import sqlite3
import shutil
from pathlib import Path
from .config import sha256, now


def canvas_geometry(path):
    with database(path) as con:
        c = dict(con.execute('SELECT * FROM Canvas').fetchone())
    dpi = float(c['CanvasResolution'])
    unit = c['CanvasUnit']
    if unit not in (0, 2):
        raise ValueError('用紙の単位が未対応です。')
    factor = dpi / 25.4 if unit == 2 else 1
    w, h = float(c['CanvasWidth']), float(c['CanvasHeight'])
    bw, bh = c.get('CropFrameInnerWidth'), c.get('CropFrameInnerHeight')
    warnings = []
    offset_keys = ('CropFrameInnerOffsetX', 'CropFrameInnerOffsetY', 'CropFrameCropOffsetX',
                   'CropFrameCropOffsetY', 'CropFrameInnerOffsetBasePosition')
    if not bw or not bh or any(c.get(k, 0) for k in offset_keys):
        bw, bh = w, h
        warnings.append('基本枠が未対応のため用紙全体に配置しました。')
    rect = [(w-bw)/2, (h-bh)/2, (w+bw)/2, (h+bh)/2]
    return {'size_px': [round(w*factor), round(h*factor)],
            'basic_frame_px': [v*factor for v in rect], 'dpi': dpi, 'warnings': warnings}


def copy_project(cmc, work):
    cmc, work = Path(cmc).resolve(), Path(work).resolve()
    destination = work / 'project'
    if destination.resolve().parent != work or (work/'backups').resolve().parent != work:
        raise ValueError('作業フォルダー外へのリンクを拒否しました。')
    info = inspect_project(cmc)
    if not info['pages'] or any(not p['exists'] for p in info['pages']):
        raise ValueError('CMCの参照ページが見つかりません。')
    if destination.is_relative_to(cmc.parent) or cmc.is_relative_to(destination):
        raise ValueError('原本と作業先が重なっています。')
    files = [cmc] + [Path(p['path']) for p in info['pages']]
    destination.mkdir(parents=True, exist_ok=True)
    backups = work / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    # CMC is copied last: its presence marks a complete initial copy.
    target_cmc = destination / cmc.name
    initial = not target_cmc.exists()
    for source in files[1:] + files[:1]:
        target = destination / source.name
        if source.is_symlink() or target.is_symlink():
            raise ValueError('原本または作業先がリンクです。')
        if initial and not target.exists():
            shutil.copy2(source, target)
        if not target.exists():
            raise ValueError('作業コピーが欠けています。別の作業名を指定してください。')
        if source != cmc:
            backup = backups / source.name
            if not backup.exists():
                shutil.copy2(source, backup)
    return inspect_project(target_cmc)


def verify_saved(path, panels, frames_only=False):
    info = inspect_clip(path)
    layers = info['layers']
    frames = [r for r in layers if r.get('LayerFolder') in (1, 17)
              and r.get('VectorNormalType') == 3 and r.get('ComicFrameLineMipmap')]
    expected = {p['folder_name'] for p in panels}
    if len(frames) != len(panels) or {r['LayerName'] for r in frames} != expected:
        raise ValueError('保存後のコマ枠数またはフォルダー名が合いません。')
    by_id = {r['MainId']: r for r in layers}
    root = next((r for r in layers if r.get('LayerName') == '' and r.get('LayerFolder')), None)
    if root is None:
        raise ValueError('保存後のルート階層を読めません。')
    top, seen, current = set(), set(), root.get('LayerFirstChildIndex')
    while current:
        if current in seen or current not in by_id:
            raise ValueError('保存後の階層が壊れています。')
        seen.add(current)
        top.add(current)
        current = by_id[current].get('LayerNextIndex')
    for frame in frames:
        if frame['MainId'] not in top:
            raise ValueError('コマ枠が最上位階層にありません。')
        child = frame.get('LayerFirstChildIndex')
        if not frames_only and (not child or child not in by_id):
            raise ValueError('コマ枠内の画像が保存されていません。')
    return info

def relative(path):
    return str(Path(path).resolve())

def database(path):
    data = Path(path).read_bytes()
    start = data.find(b"SQLite format 3\0")
    if start < 0:
        raise ValueError("No readable SQLite metadata found")
    # Deserialize into memory. The input file is never opened for database writes.
    con = sqlite3.connect(":memory:")
    con.deserialize(data[start:])
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con

def inspect_clip(path):
    with database(path) as con:
        c = dict(con.execute("SELECT * FROM Canvas").fetchone())
        cols = [r[1] for r in con.execute("PRAGMA table_info(Layer)")]
        wanted = [v for v in ("MainId", "LayerName", "LayerType", "LayerFolder", "LayerFirstChildIndex", "LayerNextIndex", "ComicFrameLineMipmap", "VectorNormalType") if v in cols]
        layers = [dict(r) for r in con.execute("SELECT " + ",".join(wanted) + " FROM Layer")]
        result = {key: c.get(key) for key in ("CanvasWidth", "CanvasHeight", "CanvasUnit", "CanvasResolution",
                   "CropFrameWidth", "CropFrameHeight", "CropFrameDitch", "ComicPageIndex",
                   "CropFrameInnerWidth", "CropFrameInnerHeight", "CropFrameInnerOffsetX", "CropFrameInnerOffsetY")}
        result["layers"] = layers
        result["file_sha256"] = sha256(path)
        return result

def inspect_project(path):
    path = Path(path).resolve()
    with database(path) as con:
        project = dict(con.execute("SELECT * FROM Project").fetchone())
        nodes = {r["_PW_ID"]: dict(r) for r in con.execute("SELECT * FROM CanvasNode")}
    root = nodes[project["ProjectRootCanvasNode"]]
    current, seen, pages = root["FirstChildIndex"], set(), []
    while current:
        if current in seen:
            raise ValueError("Cyclic page references")
        seen.add(current)
        row = nodes[current]
        link = row.get("LinkPath")
        if row["Type"] != 2 or not link or not link.startswith(".:"):
            raise ValueError("Unsupported page reference; verify in CLIP STUDIO")
        target = (path.parent / link[2:]).resolve()
        if target.parent != path.parent:
            raise ValueError("Page link escapes project directory")
        pages.append({"page_number": len(pages)+1, "node_id": current, "link": link,
                      "path": relative(target), "exists": target.is_file(),
                      "clip": None})
        current = row["NextIndex"]
    return {"cmc": relative(path), "cmc_sha256": sha256(path), "pages": pages,
            "inspection": "Read-only private metadata; GUI verification remains required",
            "at": now()}
