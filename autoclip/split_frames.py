"""Basic-frame geometry and byte-copy lifecycle. CLIP metadata is read only."""
from __future__ import annotations

import math
import shutil
from pathlib import Path

from .common import (audit, now, read_json, relative, resolve, sha256,
                     work_directory, write_json)
from .project import database, inspect_clip, inspect_project


WORKFLOW = "template_split"


def basic_frame(path):
    with database(path) as con:
        c = dict(con.execute("SELECT * FROM Canvas").fetchone())
    # Only the centred, millimetre manuscript variant measured in this workspace.
    if c["CanvasUnit"] != 2 or c["CropFrameUnitKind"] != 2 or not c["CropFrameShow"]:
        raise ValueError("Visible millimetre basic frame required for calibration")
    keys = ("CropFrameInnerOffsetX", "CropFrameInnerOffsetY", "CropFrameCropOffsetX",
            "CropFrameCropOffsetY", "CropFrameInnerOffsetBasePosition")
    if any(c[k] != 0 for k in keys):
        raise ValueError("Offset basic frames require a separate GUI calibration")
    w, h = c["CropFrameInnerWidth"], c["CropFrameInnerHeight"]
    cw, ch = c["CanvasWidth"], c["CanvasHeight"]
    if not (0 < w <= cw and 0 < h <= ch):
        raise ValueError("Invalid basic frame dimensions")
    return {"rect_mm": [(cw-w)/2, (ch-h)/2, (cw+w)/2, (ch+h)/2],
            "canvas_mm": [cw, ch], "dpi": c["CanvasResolution"]}


def group_bounds(panels):
    boxes = [p["outer_px"] for p in panels]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def axis_mapping(layout):
    c, m = layout["canvas"], layout["mapping"]
    if (m.get("frame_workflow") != WORKFLOW or m["target_region"] != "basic_frame"
            or m["fit"] != "independent_axes" or m.get("rotation_degrees", 0) != 0):
        raise ValueError("Template splitting requires unrotated independent_axes/basic_frame")
    src = group_bounds(layout["panels"])
    dest = [v*c["dpi"]/25.4 for v in c["basic_frame_mm"]]
    if not (0 < dest[0] < dest[2] < c["pixel_size"][0]
            and 0 < dest[1] < dest[3] < c["pixel_size"][1]):
        raise ValueError("Basic frame must lie inside the manuscript")
    scales = [(dest[i+2]-dest[i])/(src[i+2]-src[i]) for i in (0, 1)]
    offset = [dest[i]-src[i]*scales[i] for i in (0, 1)]
    w, h = layout["source"]["size_px"]
    return {"scale_xy": scales, "offset_px": offset, "rotation_degrees": 0,
            "source_size_px": [w, h], "source_frame_rect_px": src,
            "target_rect_px": dest, "image_rect_px": [*offset, offset[0]+w*scales[0], offset[1]+h*scales[1]],
            "fit": "independent_axes", "units": "canvas pixels"}


def subdivision(layout):
    """A deterministic guillotine tree; reject layouts it cannot reproduce.

    Small detector variations (at most one original pixel) may be aligned to
    their shared row edge. The review includes those explicit adjustments.
    """
    transform = axis_mapping(layout)
    width = layout["mapping"]["frame_line_width_px"]
    overscan = layout["mapping"].get("image_overscan_px", 12)
    if not isinstance(width, (float,int)) or not math.isfinite(width) or width <= 0:
        raise ValueError("Frame width must be a positive finite pixel value")
    if not isinstance(overscan,int) or isinstance(overscan,bool) or not 1 <= overscan <= 100:
        raise ValueError("Image overscan must be an integer from 1 to 100 pixels")
    scales, offset = transform["scale_xy"], transform["offset_px"]
    tolerance = layout["mapping"].get("edge_alignment_tolerance_px", 1)
    if not 0 <= tolerance <= 1:
        raise ValueError("Edge alignment tolerance must be between zero and one source pixel")
    cuts, leaves, adjustments = [], {}, []

    def mapped(box):
        return [v*scales[i%2]+offset[i%2] for i, v in enumerate(box)]

    def visit(panels, box, key):
        if len(panels) == 1:
            panel = panels[0]
            delta = [a-b for a, b in zip(box, panel["outer_px"])]
            if max(abs(v) for v in delta) > tolerance:
                raise ValueError(f"{panel['id']}: isolated frame cannot be made by subdivision")
            if any(delta):
                adjustments.append({"panel_id": panel["id"], "edge_delta_source_px": delta})
            leaves[panel["id"]] = {"node": key, "source_rect_px": box, "rect_px": mapped(box)}
            return
        # Prefer horizontal strips, then split each strip vertically.
        for axis in (1, 0):
            ordered = sorted(panels, key=lambda p: p["outer_px"][axis])
            for index in range(1, len(ordered)):
                low, high = ordered[:index], ordered[index:]
                a = max(p["outer_px"][axis+2] for p in low)
                b = min(p["outer_px"][axis] for p in high)
                if b <= a:
                    continue
                lo_box, hi_box = box.copy(), box.copy()
                lo_box[axis+2], hi_box[axis] = a, b
                centre = (a+b)/2*scales[axis]+offset[axis]
                gap = (b-a)*scales[axis]
                if gap < 1:
                    raise ValueError("Sub-pixel gutters are not GUI calibrated")
                cuts.append({"node": key, "axis": "y" if axis else "x", "rect_px": mapped(box),
                             "position_px": centre, "gap_px": gap, "tool_gap_px": round(gap),
                             "low_node": key+"L", "high_node": key+"H"})
                visit(low, lo_box, key+"L")
                visit(high, hi_box, key+"H")
                return
        raise ValueError("Non-guillotine layout: cannot split a single basic frame into these panels")

    visit(layout["panels"], transform["source_frame_rect_px"], "root")
    # Native split creates upper/right above the retained lower/left folder.
    palette = ["root"]
    for cut in cuts:
        i = palette.index(cut["node"])
        cut["palette_index"] = i
        children = [cut["low_node"], cut["high_node"]]
        palette[i:i+1] = children if cut["axis"] == "y" else children[::-1]
    for leaf in leaves.values():
        leaf["palette_index"] = palette.index(leaf["node"])
    return {"cuts": cuts, "leaves": leaves, "edge_adjustments": adjustments,
            "frame_line_width_px": width, "image_overscan_px": overscan}


def native_frames(info):
    return [r for r in info["layers"] if r.get("VectorNormalType") == 3
            and r.get("ComicFrameLineMipmap") and r["LayerFolder"] in (1, 17)]


def verify_base(path):
    info = inspect_clip(path)
    frames = native_frames(info)
    if len(frames) != 1 or frames[0]["LayerName"] != "AC_base_frame" or frames[0]["LayerFirstChildIndex"]:
        raise ValueError("Template must contain exactly one empty native AC_base_frame")
    if any(r["LayerName"].startswith("AC_") and r["LayerName"] != "AC_base_frame" for r in info["layers"]):
        raise ValueError("Template contains imported content")
    return info


def verify_template(config):
    work = work_directory(config)
    record = read_json(work / "template/template.json")
    base = resolve(record["path"]).resolve()
    if base != work / "template/base.clip" or sha256(base) != record["sha256"]:
        raise ValueError("Template file/hash changed")
    verify_base(base)
    if basic_frame(base)["rect_mm"] != config["canvas"]["basic_frame_mm"]:
        raise ValueError("Configured basic frame differs from calibrated template")
    evidence = resolve(record["evidence"])
    if sha256(evidence) != record["evidence_sha256"] or not record.get("screen_geometry_verified"):
        raise ValueError("Template calibration evidence changed or is missing")
    return record


def seed_pages(config):
    """Move each later page to a retained backup, then byte-copy page one's template.

    A manifest is persisted before every move. An interrupted transaction stops
    for recovery, and a completed transaction only verifies; it never reseeds.
    """
    work = work_directory(config)
    record = verify_template(config)
    project = resolve(config["project"]).resolve()
    if project.parent != work / "project":
        raise ValueError("Project must be inside work_dir/project")
    manifest = work / "template/seed.json"
    if manifest.exists():
        old = read_json(manifest)
        if old["state"] != "complete":
            raise ValueError("Interrupted template copy exists; inspect seed.json and retired files")
        if old["template_sha256"] != record["sha256"]:
            raise ValueError("Template differs from seeded version")
        for item in old["pages"]:
            if item.get("retired") and sha256(resolve(item["retired"])) != item["before_sha256"]:
                raise ValueError("Retired page hash mismatch")
            current = resolve(item["path"])
            if not current.is_file():
                raise ValueError("Seeded page is missing")
            # A completed/importing page is owned by its original operations/state.
            if not (work / "state" / f'page{item["page_number"]:04d}.json').exists():
                if sha256(current) != record["sha256"]:
                    raise ValueError("Seeded page changed; refusing to replace it")
        return manifest
    if list((work / "state").glob("*.json")) or list(resolve(config["output_dir"]).glob("*.operations*.json")):
        raise ValueError("Operations/state already exist; cannot seed pages")
    info = inspect_project(project)
    paths = [resolve(p["path"]).resolve() for p in info["pages"]]
    if not paths or len(set(paths)) != len(paths) or any(p.parent != project.parent or not p.is_file() for p in paths):
        raise ValueError("Invalid or duplicate CMC page paths")
    if sha256(paths[0]) != record["sha256"]:
        raise ValueError("Page one must still be the saved basic-frame template")
    expected_basic = basic_frame(paths[0])
    if any(basic_frame(p) != expected_basic for p in paths):
        raise ValueError("Page basic-frame/canvas dimensions differ")
    retired = work / "backups/before-template-copy"
    if retired.exists():
        raise ValueError("Untracked retirement folder exists")
    retired.mkdir(parents=True)
    shutil.copy2(project, retired / project.name)
    if sha256(project) != sha256(retired / project.name):
        raise ValueError("CMC backup verification failed")
    state = {"state": "started", "template_sha256": record["sha256"], "project": relative(project),
             "cmc_sha256": sha256(project), "at": now(), "pages": []}
    for index, p in enumerate(paths, 1):
        state["pages"].append({"page_number": index, "path": relative(p), "before_sha256": sha256(p),
                               "retired": relative(retired / p.name) if index > 1 else None, "state": "pending"})
    write_json(manifest, state)
    for item in state["pages"]:
        target = resolve(item["path"])
        if item["retired"]:
            dest = resolve(item["retired"]).resolve()
            if dest.parent != retired or target.parent != project.parent or dest.exists():
                raise ValueError("Unsafe retirement path")
            if sha256(target) != item["before_sha256"]:
                raise ValueError("Page changed before retirement")
            target.rename(dest)
            item["state"] = "retired"; write_json(manifest, state)
            if sha256(dest) != item["before_sha256"]:
                raise ValueError("Retired file verification failed")
            # Exclusive creation: never overwrite a file created during the move.
            with resolve(record["path"]).open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        if sha256(target) != record["sha256"]:
            raise ValueError("Template byte-copy verification failed")
        item["state"] = "copied"; write_json(manifest, state)
    if sha256(project) != state["cmc_sha256"]:
        raise ValueError("CMC changed during file copy")
    state.update(state="complete", completed_at=now()); write_json(manifest, state)
    audit("template_seed", manifest=relative(manifest), pages=len(paths))
    return manifest
