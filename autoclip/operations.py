from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageDraw

from .analysis import require_approval, validate_layout
from .common import (assert_source, audit, digest, layout_digest, now, read_json,
                     relative, resolve, sha256, work_directory, write_json)
from .project import inspect_project


def size_for_edge(size, edge):
    """Output size for a long-edge target; `None` means the crop is used as it is."""
    if edge is None:
        return [int(v) for v in size]
    return [max(1, round(v * edge / max(size))) for v in size]


def magnification(layout):
    """Canvas pixels per source pixel (x, y) at the planned placement."""
    transform = mapping(layout)
    if "scale_xy" in transform:
        return transform["scale_xy"]
    return [transform["scale"]] * 2


def target_long_edge(size, needed, policy):
    """Long edge for one crop under an upscale policy; None = no model upscale."""
    mode, setting = policy.get("mode", "builtin"), policy.get("long_edge", 4096)
    if mode == "none":
        return None
    if setting != "auto":
        return int(setting)
    if needed <= policy.get("skip_below_scale", 1.25):
        return None
    # Never ask the x4 model for more than its native factor, nor an unbounded canvas.
    edge = min(math.ceil(max(size) * needed), max(size) * 4, 16384)
    return edge + edge % 2


def crop_layout(layout_path, output_dir, long_edge=4096, policy=None):
    layout = read_json(layout_path)
    validate_layout(layout); assert_source(layout)
    policy = policy or {"mode": "builtin", "long_edge": long_edge}
    needed_xy = magnification(layout)
    version = layout_digest(layout)
    base = Path(output_dir) / "crops" / layout["page_id"] / version[:12]
    base.mkdir(parents=True, exist_ok=True)
    images = []
    with Image.open(resolve(layout["source"]["path"])) as image:
        image = image.convert("RGB")
        for panel in layout["panels"]:
            crop = image.crop(panel["crop_px"])
            target = base / (panel["id"] + ".png")
            crop.save(target)
            edge = target_long_edge(crop.size, max(needed_xy), policy)
            images.append({"panel_id": panel["id"], "input": relative(target),
                           "input_sha256": sha256(target), "input_size_px": list(crop.size),
                           "output": relative(Path(output_dir) / "upscaled" / layout["page_id"] / version[:12] / target.name),
                           "long_edge": edge, "required_magnification": round(max(needed_xy), 4),
                           "expected_size_px": size_for_edge(crop.size, edge)})
    jobs = {"schema_version": 1, "page_id": layout["page_id"], "layout_sha256": version,
            "long_edge": policy.get("long_edge", long_edge), "mode": policy.get("mode", "builtin"),
            "images": images, "status": "prepared_for_upscale", "at": now()}
    manifest = Path(output_dir) / (layout["page_id"] + ".upscale-jobs.json")
    write_json(manifest, jobs)
    audit("crop", page=layout["page_id"], panels=len(images))
    return manifest


def mapping(layout):
    if layout["mapping"].get("frame_workflow") == "template_split":
        from .split_frames import axis_mapping
        return axis_mapping(layout)
    canvas, choice = layout["canvas"], layout["mapping"]
    if choice.get("rotation_degrees", 0) not in (0, 90, 180, 270):
        raise ValueError("Only quarter-turn rotations are supported")
    rotation = choice.get("rotation_degrees", 0)
    w, h = layout["source"]["size_px"]
    rw, rh = (h, w) if rotation in (90, 270) else (w, h)
    factor = canvas["dpi"] / 25.4
    regions = {"trim": canvas["trim_mm"], "canvas": [0, 0, canvas["width_mm"], canvas["height_mm"]]}
    if choice["target_region"] == "bleed":
        x1,y1,x2,y2 = canvas["trim_mm"]
        b = canvas["bleed_mm"]
        regions["bleed"] = [x1-b,y1-b,x2+b,y2+b]
    rect = choice.get("target_rect_mm", regions.get(choice["target_region"]))
    if rect is None:
        raise ValueError("Unknown target region; supply target_rect_mm")
    x1,y1,x2,y2 = [v*factor for v in rect]
    if not (0 <= x1 < x2 <= canvas["pixel_size"][0] and 0 <= y1 < y2 <= canvas["pixel_size"][1]):
        raise ValueError("Target region lies outside the manuscript")
    sx, sy = (x2-x1)/rw, (y2-y1)/rh
    scale = min(sx,sy) if choice["fit"] == "contain" else max(sx,sy)
    ox, oy = (x1+x2-rw*scale)/2, (y1+y2-rh*scale)/2
    return {"scale": scale, "offset_px": [ox,oy], "rotation_degrees": rotation,
            "source_size_px": [w,h], "target_rect_px": [x1,y1,x2,y2],
            "image_rect_px": [ox,oy,ox+rw*scale,oy+rh*scale],
            "clipped_source_px": [max(0,(x1-ox)/scale), max(0,(y1-oy)/scale),
                                  max(0,(ox+rw*scale-x2)/scale),max(0,(oy+rh*scale-y2)/scale)],
            "fit": choice["fit"], "units": "canvas pixels"}


def transform_box(box, transform):
    x1,y1,x2,y2 = box
    w,h = transform["source_size_px"]
    rotation = transform["rotation_degrees"]
    if rotation == 90:
        box = [h-y2,x1,h-y1,x2]
    elif rotation == 180:
        box = [w-x2,h-y2,w-x1,h-y1]
    elif rotation == 270:
        box = [y1,w-x2,y2,w-x1]
    scales = transform.get("scale_xy") or [transform["scale"]]*2
    return [round(v*scales[i%2]+transform["offset_px"][i%2],6) for i,v in enumerate(box)]


def check_upscaled(layout, jobs):
    if jobs["layout_sha256"] != layout_digest(layout):
        raise ValueError("Upscale jobs are stale; crop and upscale again")
    expected = {p["id"]: p for p in layout["panels"]}
    actual = [v["panel_id"] for v in jobs["images"]]
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise ValueError("Upscaled panel count/IDs do not match layout")
    directories={resolve(j["output"]).parent for j in jobs["images"]}
    actual_files={p.resolve() for directory in directories for p in directory.glob("*.png")}
    expected_files={resolve(j["output"]).resolve() for j in jobs["images"]}
    if actual_files != expected_files:
        raise ValueError("Missing or extra upscaled PNG files")
    verified = {}
    for job in jobs["images"]:
        path = resolve(job["output"])
        box=expected[job["panel_id"]]["crop_px"]
        expected_size=size_for_edge((box[2]-box[0],box[3]-box[1]),job.get("long_edge",jobs["long_edge"]))
        if job["expected_size_px"]!=expected_size:
            raise ValueError("Upscale manifest dimensions differ from layout crop")
        if sha256(resolve(job["input"])) != job["input_sha256"]:
            raise ValueError("Crop hash mismatch")
        with Image.open(path) as image:
            if list(image.size) != job["expected_size_px"]:
                raise ValueError(f"Upscale dimensions differ: {path.name}")
            image.verify()
        metadata = read_json(path.with_suffix(".upscale.json"))
        if metadata["input_sha256"] != job["input_sha256"] or metadata["output_sha256"] != sha256(path):
            raise ValueError("Upscale provenance/hash mismatch")
        verified[job["panel_id"]] = {**job, "sha256": metadata["output_sha256"]}
    return verified


def build_operations(layout_path, config, draft=False):
    layout = read_json(layout_path)
    output = resolve(config["output_dir"])
    path = output / (layout["page_id"] + (".operations.draft.json" if draft else ".operations.json"))
    if (work_directory(config) / "state" / (layout["page_id"] + ".json")).exists():
        raise ValueError("Run state exists; retain the original operations and follow recovery instructions")
    if path.exists() and not draft:
        raise ValueError("Operations already exist; reuse them instead of replacing the baseline")
    if draft:
        validate_layout(layout); assert_source(layout)
    else:
        require_approval(layout)
    jobs = read_json(output / (layout["page_id"] + ".upscale-jobs.json"))
    images = check_upscaled(layout, jobs)
    project = inspect_project(resolve(config["project"]))
    page = next((p for p in project["pages"] if p["page_number"] == layout["page_number"]), None)
    # Jobs created by `configure` use the page file the CMC itself registers; older configs name a pattern.
    target = (resolve(config["clip_pattern"].format(number=layout["page_number"])) if config.get("clip_pattern")
              else (resolve(page["path"]) if page else None))
    if page is None or resolve(page["path"]).resolve() != target.resolve():
        raise ValueError("Target is not the CMC's registered page; register it in CLIP STUDIO")
    clip = page["clip"]
    c = layout["canvas"]
    if clip is None or any(abs(clip[k]-v) > .001 for k,v in (
        ("CanvasWidth", c["width_mm"]), ("CanvasHeight", c["height_mm"]), ("CanvasResolution", c["dpi"]))):
        raise ValueError("Actual manuscript dimensions differ from reviewed layout")
    transform = mapping(layout)
    split = None
    if layout["mapping"].get("frame_workflow") == "template_split":
        from .split_frames import basic_frame, subdivision, verify_template
        template = verify_template(config)
        seeded = read_json(work_directory(config) / "template/seed.json")
        if seeded["state"] != "complete" or sha256(target) != template["sha256"]:
            raise ValueError("Target is not an unchanged byte-copy of the calibrated template")
        if basic_frame(target)["rect_mm"] != c["basic_frame_mm"]:
            raise ValueError("Target basic frame differs from the reviewed layout")
        split = subdivision(layout)
    operations = []
    for p in sorted(layout["panels"], key=lambda x:x["reading_order"]):
        image = images[p["id"]]
        placed = transform_box(p["crop_px"], transform)
        source_box = p["crop_px"]
        scale = transform.get("scale", 1)
        operations.append({"panel_id": p["id"], "folder_name": f'AC_{layout["page_id"]}_{p["id"]}',
                           "image_layer_name": f'AC_{layout["page_id"]}_{p["id"]}_image',
                           "frame_type": "native_clip_studio_frame_border_folder",
                           "outer_rect_px": transform_box(p["outer_px"], transform),
                           "frame_inner_rect_px": transform_box(p["inner_px"], transform),
                           "frame_line_width_px": p.get("border_inset_px",5)*scale,
                           "image": image["output"], "image_sha256": image["sha256"],
                           "image_size_px": image["expected_size_px"], "placement_rect_px": placed,
                           "uniform_image_scale": scale*(source_box[2]-source_box[0])/image["expected_size_px"][0],
                           "rotation_degrees": transform["rotation_degrees"],
                           "state": "pending"})
        if split:
            op = operations[-1]
            rect = split["leaves"][p["id"]]["rect_px"]
            width, bleed = split["frame_line_width_px"], split["image_overscan_px"]
            op.update(frame_path_rect_px=rect, palette_index=split["leaves"][p["id"]]["palette_index"],
                      outer_rect_px=[v+(-width/2 if i<2 else width/2) for i,v in enumerate(rect)],
                      frame_inner_rect_px=[v+(width/2 if i<2 else -width/2) for i,v in enumerate(rect)],
                      frame_line_width_px=width,
                      placement_rect_px=[v+(-bleed if i<2 else bleed) for i,v in enumerate(rect)],
                      source_inner_px=p["inner_px"], source_crop_px=p["crop_px"],
                      image_fill="inner_crop_stretch_xy_with_overscan")
            del op["uniform_image_scale"]
    plan = {"schema_version":1, "page_id": layout["page_id"], "layout":relative(layout_path),
            "layout_sha256": layout_digest(layout), "state":"draft_review_pending" if draft else "ready",
            "project": relative(resolve(config["project"])), "target_clip": relative(target),
            "target_clip_before_sha256": sha256(target), "canvas": c, "transform":transform,
            "outside_panels": layout["mapping"]["outside_panels"], "operations":operations,
            "approval_required": True, "created_at":now()}
    if "work_dir" in config:
        plan["work_dir"] = relative(work_directory(config))
    if split:
        plan.update(frame_workflow="template_split", subdivision=split,
                    template_sha256=template["sha256"], template=template["path"])
    plan["plan_sha256"] = digest({k:v for k,v in plan.items() if k not in ("created_at","state")})
    write_json(path, plan)
    audit("operations", page=layout["page_id"], draft=draft)
    return path


def verify_operations(path, target_sha256=None):
    plan = read_json(path)
    if plan["state"] != "ready":
        raise ValueError("Draft operations cannot be imported")
    computed = digest({k:v for k,v in plan.items() if k not in ("created_at","state","plan_sha256")})
    if computed != plan["plan_sha256"]:
        raise ValueError("Operations changed; regenerate from reviewed layout")
    layout = read_json(resolve(plan["layout"]))
    require_approval(layout)
    if layout_digest(layout) != plan["layout_sha256"]:
        raise ValueError("Layout changed after operations were generated")
    if sha256(resolve(plan["target_clip"])) != (target_sha256 or plan["target_clip_before_sha256"]):
        raise ValueError("Target changed; inspect saved state before rerun")
    if plan.get("frame_workflow") == "template_split":
        if sha256(resolve(plan["template"])) != plan["template_sha256"]:
            raise ValueError("Calibrated template changed")
    for op in plan["operations"]:
        if sha256(resolve(op["image"])) != op["image_sha256"]:
            raise ValueError("Upscaled image changed")
        with Image.open(resolve(op["image"])) as image:
            if list(image.size) != op["image_size_px"]:
                raise ValueError("Upscale dimensions changed")
    return plan


def calibrated_placement(plan, calibration):
    """Add GUI safety bleed to an execution copy, retaining the reviewed baseline."""
    if plan.get("frame_workflow") != "template_split":
        return plan
    split = calibration.get("split", {})
    if not split.get("verified") or split.get("workflow") != "template_split":
        raise ValueError("Native subdivision calibration has not been verified")
    tolerance = split["geometry_tolerance_screen_px"]
    if not 0 < tolerance <= 2.5:
        raise ValueError("Unsafe frame geometry tolerance")
    x1, y1, x2, y2 = calibration["canvas_screen_rect"]
    w, h = calibration["canvas_size_px"]
    if x2 <= x1 or y2 <= y1:
        raise ValueError("Invalid calibrated canvas rectangle")
    error = tolerance * max(w / (x2-x1), h / (y2-y1))
    result = deepcopy(plan)
    for op in result["operations"]:
        # Include half the stroke and raster placement rounding; do not relax
        # geometry validation or change any frame path, source crop or approval.
        bleed = math.ceil(error + op["frame_line_width_px"] / 2 + .5)
        if bleed > 100:
            raise ValueError("Required calibrated image overscan exceeds 100 pixels")
        r, p = op["frame_path_rect_px"], op["placement_rect_px"]
        op["placement_rect_px"] = [min(p[i], r[i]-bleed) if i < 2
                                   else max(p[i], r[i]+bleed) for i in range(4)]
    return result


def prepare_assets(path, calibration=None):
    """Create canvas-sized transparent PNGs for position-preserving GUI paste."""
    plan = verify_operations(path)
    if calibration is not None:
        plan = calibrated_placement(plan, calibration)
    layout = read_json(resolve(plan["layout"]))
    base = work_directory(plan) / "import-assets" / plan["page_id"] / plan["plan_sha256"][:12]
    if calibration is not None and plan.get("frame_workflow") == "template_split":
        base = base / digest([op["placement_rect_px"] for op in plan["operations"]])[:12]
    base.mkdir(parents=True, exist_ok=True)
    size = tuple(plan["canvas"]["pixel_size"])
    dpi = plan["canvas"]["dpi"]
    transform = plan["transform"]
    is_split = plan.get("frame_workflow") == "template_split"
    if transform["rotation_degrees"] != 0 or (transform["fit"] != "contain" and not is_split):
        raise ValueError("GUI recipe is validated for unrotated contain placement only")
    def placed(image, rect):
        x1,y1,x2,y2 = [round(v) for v in rect]
        full = Image.new("RGBA",size,(0,0,0,0))
        full.paste(image.resize((x2-x1,y2-y1),Image.Resampling.LANCZOS),(x1,y1))
        return full
    context = Image.open(resolve(layout["source"]["path"])).convert("RGBA")
    d = ImageDraw.Draw(context)
    for panel in layout["panels"]:
        x1,y1,x2,y2 = panel["outer_px"]
        d.rectangle((x1,y1,x2-1,y2-1),fill=(0,0,0,0))
    context = placed(context,transform["image_rect_px"])
    context_name = f'AC_{plan["page_id"]}_outside'
    context_path = base / (context_name+".png")
    context.save(context_path,dpi=(dpi,dpi))
    assets = [{"kind":"outside", "name":context_name,"path":relative(context_path),"sha256":sha256(context_path)}]
    for op in plan["operations"]:
        with Image.open(resolve(op["image"])) as image:
            image = image.convert("RGBA")
            if is_split:
                # Use the artwork inside the original border. The new native
                # border is drawn by CSP; overscan is clipped by that real frame.
                image = split_image_content(image, op)
            canvas = placed(image,op["placement_rect_px"])
        target = base / (op["image_layer_name"]+".png")
        canvas.save(target,dpi=(dpi,dpi))
        assets.append({"kind":"panel","panel_id":op["panel_id"],"name":op["image_layer_name"],
                       "path":relative(target),"sha256":sha256(target)})
    result = {"plan_sha256":plan["plan_sha256"],"canvas_size_px":list(size),"assets":assets,
              "placement_rects_px": {op["panel_id"]: op["placement_rect_px"] for op in plan["operations"]}}
    write_json(base/"assets.json",result)
    return base/"assets.json"


def split_image_content(image, op):
    crop, inner = op["source_crop_px"], op["source_inner_px"]
    sx, sy = image.width/(crop[2]-crop[0]), image.height/(crop[3]-crop[1])
    rect = [(inner[i]-crop[i%2])*(sx if i%2 == 0 else sy) for i in range(4)]
    result = image.crop(tuple(round(v) for v in rect))
    if result.width < 1 or result.height < 1:
        raise ValueError("Empty image interior")
    if result.getextrema()[3][0] != 255:
        raise ValueError("Transparent artwork would leave gaps inside the native frame")
    return result
