"""Job set-up for agents: scan input/, record the user's answers, report status.

Everything printed here has a size that does not depend on the number of pages, so an agent can run
these commands on a 100-page manga for the same token cost as on a 5-page one. Details go to files.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path

from PIL import Image

from . import envscan
from .common import (CODE_ROOT, IMAGE_EXTENSIONS, ROOT, job_config_path, list_sources, natural_key, now, read_json,
                     relative, resolve, write_json)
from .models import inspect_model

FIT_CHOICES = {"stretch": "fill the whole basic frame; rows and columns are scaled independently",
               "keep_aspect": "keep the aspect ratio and allow margins (NOT IMPLEMENTED YET)"}
FIT_AVAILABLE = ("stretch",)
UPSCALE_CHOICES = ("builtin", "external", "none")
LOSSLESS_SCALE = 1.25  # enlarging a page by up to this factor is left to the resampler


def slugify(text):
    slug = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-_")
    return slug or "job"


def short_list(items, limit=8):
    items = list(items)
    return ", ".join(map(str, items[:limit])) + (f" +{len(items) - limit} more" if len(items) > limit else "")


def image_folders(root):
    """Folders under input/ that directly hold PNG/JPG pages, with a compact fingerprint each."""
    found = []
    for folder in sorted({p.parent for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXTENSIONS},
                         key=lambda p: natural_key(str(p))):
        files = sorted((p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS),
                       key=lambda p: natural_key(p.name))
        sizes = Counter()
        for path in files:  # header-only reads; cheap even for hundreds of pages
            with Image.open(path) as image:
                sizes[image.size] += 1
        stems = Counter(p.stem.lower() for p in files)
        found.append({"path": relative(folder), "count": len(files),
                      "formats": dict(Counter(p.suffix.lower().lstrip(".") for p in files)),
                      "sizes": [{"size_px": list(s), "pages": n} for s, n in sizes.most_common(3)],
                      "size_variants": len(sizes), "first": files[0].name, "last": files[-1].name,
                      "duplicate_stems": [s for s, n in stems.items() if n > 1][:5]})
    return found


def project_facts(cmc):
    """Read-only facts about a CMC project and its first page."""
    from .project import inspect_project
    from .split_frames import basic_frame
    info = inspect_project(cmc)
    pages = info["pages"]
    facts = {"path": relative(cmc), "pages": len(pages), "missing_pages": [p["page_number"] for p in pages if not p["exists"]],
             "clip_names": [Path(p["path"]).name for p in pages[:2]]}
    first = pages[0]["clip"] if pages and pages[0]["clip"] else None
    if first:
        mm, dpi = (first["CanvasWidth"], first["CanvasHeight"]), first["CanvasResolution"]
        facts.update(canvas_mm=list(mm), dpi=dpi, pixel_size=[round(v * dpi / 25.4) for v in mm],
                     trim_mm=[first["CropFrameWidth"], first["CropFrameHeight"]], bleed_mm=first["CropFrameDitch"],
                     untouched=not any(r.get("ComicFrameLineMipmap") or r["LayerName"].startswith("AC_")
                                       for page in pages if page["clip"] for r in page["clip"]["layers"]))
        try:
            facts["basic_frame_mm"] = basic_frame(resolve(pages[0]["path"]))["rect_mm"]
        except (ValueError, KeyError) as exc:
            facts["basic_frame_error"] = str(exc)
    return facts


def pair_projects(images, projects):
    """Pair image folders with CMC projects: same top-level input folder first."""
    pairs = []
    for folder in images:
        parts = Path(folder["path"]).parts
        top = parts[1] if len(parts) > 1 else ""
        same = [p for p in projects if Path(p["path"]).parts[1:2] == (top,)]
        pairs.append({"images": folder["path"], "project": same[0]["path"] if len(same) == 1 else None,
                      "candidates": [p["path"] for p in (same or projects)]})
    return pairs


def environment(search=()):
    roots = [Path(p) for p in search] + envscan.default_roots()
    pythons, models = envscan.find(roots)
    venv = CODE_ROOT / ".venv/Scripts/python.exe"
    result = {"builtin_venv": venv.is_file(), "external_pythons": [], "models": []}
    for python in pythons:
        probe = envscan.probe_python(python)
        result["external_pythons"].append({"path": str(python), "ok": probe["ok"], "cuda": probe.get("cuda"),
                                           "torch": probe.get("torch")})
    result["models"] = [{"path": str(m["path"]), "compatible": m["compatible"], "reason": m["reason"]} for m in models]
    return result


def profile_files():
    folder = ROOT / "profile"
    return sorted(relative(p) for p in folder.glob("*.json")) if folder.is_dir() else []


def scan(search=(), discover_env=False):
    if search and not discover_env:
        raise ValueError('--search requires --discover-env; ask the user for permission before searching environments')
    root = ROOT / "input"
    if not root.is_dir():
        raise ValueError("input/ does not exist; create it and place the manga pages and the CMC project there")
    images = image_folders(root)
    projects = [project_facts(p) for p in sorted(root.rglob("*.cmc"), key=lambda p: natural_key(str(p)))]
    report = {"at": now(), "images": images, "projects": projects, "pairs": pair_projects(images, projects),
              "environment": environment(search) if discover_env else {
                  "builtin_venv": (CODE_ROOT / '.venv/Scripts/python.exe').is_file(),
                  "external_pythons": [], "models": [], "search_skipped": True}, "profiles": profile_files(),
              "svg_files": len(list(root.rglob("*.svg")))}
    write_json(ROOT / "work/scan.json", report)
    lines = []
    for pair in report["pairs"]:
        folder = next(f for f in images if f["path"] == pair["images"])
        sizes = ", ".join(f'{s["size_px"][0]}x{s["size_px"][1]} x{s["pages"]}' for s in folder["sizes"])
        lines.append(f'images {pair["images"]}: {folder["count"]} pages {folder["formats"]} {sizes}'
                     + (f' ({folder["size_variants"]} size variants)' if folder["size_variants"] > 3 else "")
                     + (f' DUPLICATE-NAMES {folder["duplicate_stems"]}' if folder["duplicate_stems"] else ""))
        lines.append(f'  project: {pair["project"] or "NOT PAIRED; candidates: " + short_list(pair["candidates"])}')
    for project in projects:
        note = (f'canvas {project["canvas_mm"][0]:g}x{project["canvas_mm"][1]:g}mm {project["dpi"]:g}dpi, '
                f'basic frame {project["basic_frame_mm"]}' if "basic_frame_mm" in project else
                project.get("basic_frame_error", "no readable canvas"))
        lines.append(f'cmc {project["path"]}: {project["pages"]} pages; {note}'
                     + ("" if project.get("untouched", True) else " [ALREADY EDITED]")
                     + (f' MISSING {project["missing_pages"]}' if project["missing_pages"] else ""))
    env = report["environment"]
    pythons = short_list(f'{e["path"]} ok={e["ok"]} cuda={e["cuda"]}' for e in env["external_pythons"]) or "none"
    models = short_list(f'{Path(m["path"]).name}{"" if m["compatible"] else " (unsupported)"}' for m in env["models"]) or "none"
    if env.get('search_skipped'):
        lines.append('env: search skipped; ask whether an existing environment is available and request permission to search')
    else:
        lines.append(f'env: builtin .venv {"yes" if env["builtin_venv"] else "no"}; external python {pythons}; models {models}')
    lines.append(f'profiles: {short_list(report["profiles"]) or "none (GUI calibration needed)"}; svg files: {report["svg_files"]}')
    print("\n".join(lines))
    print("details: work/scan.json")
    return report


def build_config(slug, images, project, defaults, facts):
    """New job configuration derived from measured facts; nothing here is typed by hand."""
    cmc = resolve(project)
    config = {k: copy.deepcopy(defaults[k]) for k in ("schema_version", "page_start", "page_limit", "detection", "upscale")}
    dpi = facts["dpi"]
    cw, ch = facts["canvas_mm"]
    tw, th = facts["trim_mm"]
    trim = [(cw - tw) / 2, (ch - th) / 2, (cw + tw) / 2, (ch + th) / 2]
    line = round(defaults["frame_line_mm"] * dpi / 25.4, 2)
    work = f"work/{slug}"
    config.update(
        job={"name": slug, "created_at": now()},
        input_dir=relative(cmc.parent), source_project=relative(cmc), source_dir=images,
        output_dir=f"output/{slug}", work_dir=work,
        project=f"{work}/project/{cmc.name}",
        gui_calibration=f'profile/{facts["pixel_size"][0]}x{facts["pixel_size"][1]}.json',
        canvas={"width_mm": cw, "height_mm": ch, "dpi": dpi, "pixel_size": facts["pixel_size"], "trim_mm": trim,
                "bleed_mm": facts["bleed_mm"], "basic_frame_mm": facts["basic_frame_mm"],
                "verified_by": "read from the CMC/CLIP metadata by autoclip configure"},
        mapping={**copy.deepcopy(defaults["mapping"]), "frame_line_width_px": line},
        options={"fit_policy": None, "upscale": None, "quality_loss_accepted": False, "answers": {}})
    return config


def record_answer(config, key, value, how="user reply relayed by the agent"):
    config["options"][key] = value
    config["options"]["answers"][key] = {"value": value, "at": now(), "by": how}


def configure(name=None, images=None, project=None, fit=None, upscale=None, long_edge=None, python=None,
              model=None, accept_quality_loss=False, profile=None, search=()):
    slug = slugify(name or (Path(images).name if images else ""))
    path = job_config_path(slug)
    if path.exists():
        config = read_json(path)
    else:
        if not (images and project):
            raise ValueError("A new job needs --images and --project (see `scan`)")
        defaults = read_json(CODE_ROOT / "config/defaults.json")
        folder = resolve(images).resolve()
        cmc = resolve(project).resolve()
        for item in (folder, cmc):
            if not item.is_relative_to((ROOT / "input").resolve()):
                raise ValueError(f"{item.name} must be inside input/")
        if not folder.is_dir() or not cmc.is_file() or cmc.suffix.lower() != ".cmc":
            raise ValueError("--images must be a folder and --project a .cmc file")
        facts = project_facts(cmc)
        if facts["missing_pages"]:
            raise ValueError(f'CMC pages missing on disk: {facts["missing_pages"]}')
        if "basic_frame_mm" not in facts:
            raise ValueError("Unsupported canvas: " + facts.get("basic_frame_error", "unreadable"))
        sources = list_sources({"source_dir": relative(folder), "page_limit": None})
        if not sources:
            raise ValueError("No PNG/JPG pages in --images")
        if len(sources) > facts["pages"]:
            raise ValueError(f'{len(sources)} images but the CMC has only {facts["pages"]} pages; add pages in CLIP STUDIO first')
        config = build_config(slug, relative(folder), relative(cmc), defaults, facts)
        config["job"]["images"], config["job"]["cmc_pages"] = len(sources), facts["pages"]
        config["job"]["notes"] = [] if len(sources) == facts["pages"] else [
            f'{facts["pages"] - len(sources)} CMC pages beyond the images stay as empty basic-frame pages']
        if not facts["untouched"]:
            config["job"]["notes"].append("CMC already contains edited pages; calibration needs an untouched copy")
    if profile:
        config["gui_calibration"] = relative(resolve(profile))
    if fit:
        if fit not in FIT_CHOICES:
            raise ValueError(f"--fit must be one of {sorted(FIT_CHOICES)}")
        if fit not in FIT_AVAILABLE:
            raise ValueError(f"fit '{fit}' is not implemented yet; offer 'stretch' instead and say so to the user")
        record_answer(config, "fit_policy", fit)
    if upscale:
        apply_upscale(config, upscale, python, model, search)
    if long_edge:
        config["upscale"]["long_edge"] = "auto" if long_edge == "auto" else int(long_edge)
    if accept_quality_loss:
        record_answer(config, "quality_loss_accepted", True, "explicit user consent relayed by the agent")
    write_json(path, config)
    print(f"job {slug}: {relative(path)}")
    for line in pending_questions(config):
        print("ASK: " + line)
    return path


def apply_upscale(config, upscale, python, model, search):
    if upscale not in UPSCALE_CHOICES:
        raise ValueError(f"--upscale must be one of {UPSCALE_CHOICES}")
    block = config["upscale"]
    if upscale != "none":
        model_path = Path(model) if model else None
        if model_path is None:
            raise ValueError("--model is required (a .safetensors RRDBNet x4 file); ask permission before `scan --discover-env` lists candidates")
        info = inspect_model(model_path)
        if not info["compatible"]:
            raise ValueError(f"Model not usable: {info['reason']}")
        block["model"] = str(model_path.resolve())
    if upscale == "external":
        if not python:
            raise ValueError("--python is required for external (ComfyUI python_embeded or a venv python)")
        probe = envscan.probe_python(python)
        if not probe["ok"]:
            raise ValueError(f"External python lacks torch/safetensors/numpy/Pillow: {probe}")
        block["python"] = str(Path(python).resolve())
    elif upscale == "builtin":
        if not (CODE_ROOT / ".venv/Scripts/python.exe").is_file():
            raise ValueError("Built-in environment missing; run scripts/setup.ps1 -BuiltinUpscale first (docs/SETUP.html)")
        block["python"] = None
    block["mode"] = upscale
    record_answer(config, "upscale", upscale)


def pending_questions(config):
    options, lines = config["options"], []
    if options.get("fit_policy") is None:
        lines.append("fit_policy -> --fit stretch | keep_aspect(not available). "
                     "stretch: scale rows/columns independently so the basic frame is filled. "
                     "keep_aspect: keep ratio, accept margins.")
    if options.get("upscale") is None:
        lines.append("upscale -> --upscale builtin | external --python P | none ; add --model M. "
                     "none loses sharpness when panels are enlarged.")
    elif options["upscale"] == "none" and not options.get("quality_loss_accepted"):
        lines.append("quality_loss -> --accept-quality-loss only if the user accepts blurrier enlarged panels "
                     "(not needed if every panel is enlarged <=1.25x).")
    if not resolve(config["gui_calibration"]).is_file():
        lines.append(f'profile -> GUI calibration missing ({config["gui_calibration"]}); follow docs/guides/profile.html')
    return lines
