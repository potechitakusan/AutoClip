"""Run one pipeline stage for every page of a job, and report status.

Console output is a fixed-size summary. Per-page detail is written to work/<job>/logs/ and
work/<job>/progress.json, so the agent's token use does not grow with the page count.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from . import analysis
from . import operations as ops
from .common import (CODE_ROOT, ROOT, audit, job_config_path, load_config, now, read_json, relative, resolve, sha256,
                     work_directory, write_json)
from .jobs import LOSSLESS_SCALE, short_list

STAGES = ("init", "calibrate", "analyze", "approve", "upscale", "prepare", "check", "import", "all")


def load_job(slug):
    path = job_config_path(slug)
    if not path.is_file():
        raise ValueError(f"No such job: {slug} (run `configure` first)")
    config = load_config(relative(path))
    config["_slug"] = slug
    return config


def log_file(config, stage):
    folder = work_directory(config) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'{stage}-{time.strftime("%Y%m%d-%H%M%S")}.log'


def layouts(config, page=None):
    paths = sorted(resolve(config["output_dir"]).glob("*.layout.json"))
    if page:
        paths = [p for p in paths if p.name == f"page{page:04d}.layout.json"]
    if not paths:
        raise ValueError("No layouts; run the analyze stage first")
    return paths


def need_answers(config):
    missing = [k for k in ("fit_policy", "upscale") if config["options"].get(k) is None]
    if missing:
        raise ValueError(f"Unanswered options {missing}; ask the user, then run `configure` (see `status`)")


def focus_clip(delay=1.5):
    """Bring the single CLIP STUDIO window to the front so the user need not do it."""
    from .window_handoff import Windows
    api = Windows()
    clips = [w for w in api.windows() if w["exe"].lower() == "clipstudiopaint.exe"]
    if len(clips) != 1:
        raise ValueError("Expected exactly one CLIP STUDIO PAINT window; start it and open nothing else from the job")
    if not api.activate(clips[0]["hwnd"]):
        raise ValueError("Windows refused to bring CLIP STUDIO to the front; click its taskbar icon and rerun")
    time.sleep(delay)


def progress(config, stage, done, total, current=None, state="running"):
    write_json(work_directory(config) / "progress.json",
               {"stage": stage, "done": done, "total": total, "current": current, "state": state, "at": now()})


def run_stage(config, stage, page=None, reviewer=None, ack_warnings=False, focus=True):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage}; stages: {', '.join(STAGES)}")
    log = log_file(config, stage)
    with log.open("w", encoding="utf-8") as stream, contextlib.redirect_stdout(stream):
        try:
            summary = globals()["stage_" + stage](config, page=page, reviewer=reviewer, ack=ack_warnings, focus=focus)
        except BaseException as exc:
            print(f"STOP: {exc}")
            stream.flush()
            write_json(work_directory(config) / "last-error.json", {"stage": stage, "error": str(exc), "log": relative(log), "at": now()})
            raise
    print(f"{stage}: {summary}")
    print(f"log: {relative(log)}")
    return summary


def stage_init(config, **_):
    from .project import initialize_project
    initialize_project(config)
    return "work copy and original backup ready"


def confirm_review(config, reviewer, ack):
    """Only an actual human response to this dialog authorizes approval."""
    import ctypes
    import os
    if os.name != 'nt':
        raise ValueError('Review dialog requires Windows; review and approve separately')
    reviewer = reviewer or 'ユーザー'
    index = resolve(config['output_dir'])/'index.html'
    if not index.is_file():
        raise ValueError('Review index missing')
    os.startfile(str(index))
    flagged = any(l['warnings'] or any(p['warnings'] for p in l['panels']) for l in
                  (read_json(path) for path in layouts(config)))
    message = ('解析結果のページをすべて確認してください。' +
               ('警告ページもすべて確認してください。' if flagged else '') +
               '\n確認が済んだ場合だけ「はい」を押すと、拡大とクリスタへの移送を開始します。'
               '\n「いいえ」は中止します。\n実行中はキーボードとマウスを操作しないでください。')
    # Yes/No, default No. Neither a CLI flag nor a timeout substitutes for review.
    answer = ctypes.windll.user32.MessageBoxW(None, message, 'AutoClip：解析結果の確認', 0x4 | 0x100 | 0x40000)
    if answer != 6:
        raise ValueError('Human review was not confirmed; import has not started')
    stage_approve(config, reviewer=reviewer, ack=flagged or ack)


def stage_all(config, reviewer=None, ack=False, page=None, focus=True, **_):
    """Resume the full job; stop for real human review before any artwork import."""
    if page is not None or not focus:
        raise ValueError('run all handles the whole job and requires GUI focus')
    need_answers(config)
    stage_init(config)
    if not profile_verified(config):
        raise ValueError('AGENT_CALIBRATION_REQUIRED: connect Computer Use to the local CLIP desktop and run '
                         '`profile computer --step prepare`, observe controls through Computer Use, '
                         'then `profile computer --step finish`. See AGENTS.md and docs/PROFILE.html. '
                         'Rerun `run all` after verification; no artwork import has started.')
    seed = work_directory(config)/'template/seed.json'
    if not seed.is_file() or read_json(seed).get('state') != 'complete':
        run_stage(config, 'calibrate')
    if not list(resolve(config['output_dir']).glob('*.layout.json')):
        run_stage(config, 'analyze')
    from .common import list_sources
    if len(layouts(config)) != len(list_sources(config)):
        raise ValueError('Analysis page count differs from source images; recover analysis before importing')
    if not all(analysis.is_approved(read_json(path)) for path in layouts(config)):
        confirm_review(config, reviewer, ack)
    # Check all approvals before invoking the upscaler or changing artwork pages.
    for path in layouts(config):
        analysis.require_approval(read_json(path))
    for stage in ('upscale', 'prepare', 'check', 'import'):
        run_stage(config, stage)
    return 'all pages saved and layer hierarchy verified'


def stage_calibrate(config, focus=True, **_):
    from .split_gui import calibrate
    need_answers(config)
    stage_init(config)
    profile = resolve(config["gui_calibration"])
    if not profile.is_file():
        raise ValueError(f"GUI profile missing: {relative(profile)}; run `profile auto` (docs/PROFILE.html)")
    if focus:
        focus_clip()
    calibrate(config, relative(profile))
    return "basic-frame template created and copied to every page"


def profile_verified(config):
    path = resolve(config["gui_calibration"])
    return path.is_file() and read_json(path).get("split", {}).get("verified") is True


def stage_analyze(config, **_):
    need_answers(config)
    if not profile_verified(config):
        raise ValueError("GUI profile missing or unverified; the frame line width comes from it. "
                         "Run `profile auto` first (docs/PROFILE.html)")
    work = work_directory(config)
    if list(resolve(config["output_dir"]).glob("*.operations*.json")) or list((work / "state").glob("*.json")):
        raise ValueError("Operations/state exist; use a new job name for re-analysis")
    result = analysis.analyze(config, quiet=True)
    return f'{result["pages"]} pages, {result["panels"]} panels, warnings on {len(result["flagged"])}: {short_list(result["flagged"], 10)}'


def stage_approve(config, reviewer=None, ack=False, page=None, **_):
    """Record the human's confirmation. Only call after the user said they reviewed the pages."""
    reviewer = reviewer or 'ユーザー'
    approved, skipped = 0, []
    for path in layouts(config, page):
        layout = read_json(path)
        if analysis.is_approved(layout):
            approved += 1
            continue
        try:
            analysis.approve(path, reviewer, ack, index=False)
            approved += 1
        except ValueError as exc:
            skipped.append(f'{layout["page_id"]}: {exc}')
    analysis.write_index(resolve(config["output_dir"]))
    return f"approved {approved}, not approved {len(skipped)}" + (f" ({short_list(skipped, 5)})" if skipped else "")


def copy_unscaled(job):
    """No-model path: the crop is the output. Same provenance record the model path writes."""
    source, target = resolve(job["input"]), resolve(job["output"])
    meta = target.with_suffix(".upscale.json")
    if target.is_file() and meta.is_file() and sha256(target) == read_json(meta).get("output_sha256"):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    write_json(meta, {"input_sha256": job["input_sha256"], "output_sha256": sha256(target),
                      "input_size_px": job["input_size_px"], "output_size_px": job["input_size_px"],
                      "engine": "copy-no-upscale", "completed_at": now()})


def runner_command(config, manifests):
    block = config["upscale"]
    common = ["--jobs", *map(str, manifests), "--model", block["model"], "--tile", str(block["tile"]),
              "--pad", str(block["tile_pad"]), "--device", block["device"], "--precision", block["precision"]]
    if block["mode"] == "external":
        return [block["python"], "-B", "-s", str(CODE_ROOT / "scripts/upscale_runner.py"), *common]
    return [str(CODE_ROOT / ".venv/Scripts/python.exe"), "-B", "-m", "autoclip.upscale", *common]


def stage_upscale(config, page=None, **_):
    need_answers(config)
    block = config["upscale"]
    if block["mode"] not in ("builtin", "external", "none"):
        raise ValueError("Upscale mode unset")
    output = resolve(config["output_dir"])
    manifests, images = [], []
    for path in layouts(config, page):
        manifest = ops.crop_layout(path, output, policy=block)
        manifests.append(manifest)
        images += read_json(manifest)["images"]
    lossy = [i for i in images if i["long_edge"] is None and i["required_magnification"] > LOSSLESS_SCALE]
    if lossy and block["mode"] == "none" and not config["options"].get("quality_loss_accepted"):
        worst = max(i["required_magnification"] for i in lossy)
        raise ValueError(f"{len(lossy)} of {len(images)} panels would be enlarged up to {worst:.2f}x without upscaling "
                         "and look blurry. Ask the user; if they accept, rerun `configure --accept-quality-loss`.")
    for job in images:
        if job["long_edge"] is None:
            copy_unscaled(job)
    modelled = [i for i in images if i["long_edge"] is not None]
    if modelled:
        command = runner_command(config, manifests)
        if block["mode"] == "builtin" and not Path(command[0]).is_file():
            raise ValueError("Built-in environment missing; run scripts/setup.ps1 -BuiltinUpscale (docs/SETUP.html)")
        started = time.monotonic()
        done = subprocess.run(command, cwd=str(CODE_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(done.stdout[-4000:])
        if done.returncode:
            tail = " | ".join(done.stderr.strip().splitlines()[-3:])
            raise ValueError(f"upscaler exited {done.returncode}: {tail}")
        print(f"model time {time.monotonic() - started:.0f}s")
    for manifest in manifests:  # fail now, not at import time
        jobs = read_json(manifest)
        ops.check_upscaled(read_json(resolve(f'{config["output_dir"]}/{jobs["page_id"]}.layout.json')), jobs)
    worst = max((i["required_magnification"] for i in images), default=0)
    return (f"{len(manifests)} pages, {len(images)} panels: {len(images) - len(modelled)} copied, "
            f"{len(modelled)} through the model; largest enlargement {worst:.2f}x")


def stage_prepare(config, page=None, **_):
    made = kept = 0
    for path in layouts(config, page):
        layout = read_json(path)
        target = resolve(config["output_dir"]) / f'{layout["page_id"]}.operations.json'
        if target.exists():
            kept += 1
        else:
            ops.build_operations(path, config, False)
            made += 1
    return f"operations created {made}, kept {kept}"


def operation_files(config, page=None):
    files = [p.with_name(p.name.replace(".layout.json", ".operations.json")) for p in layouts(config, page)]
    missing = [p.name for p in files if not p.is_file()]
    if missing:
        raise ValueError(f"Missing operations {short_list(missing, 5)}; run prepare")
    return files


def stage_check(config, page=None, **_):
    from .gui import check
    files = operation_files(config, page)
    for path in files:
        check(path, config["gui_calibration"])
    return f"{len(files)} pages: files, hashes and calibration agree"


def stage_import(config, page=None, focus=True, **_):
    from .completion import run_stage_with_handoff
    from .gui import check, run
    files = operation_files(config, page)
    for path in files:  # fail fast, before any desktop input
        check(path, config["gui_calibration"])

    def operation():
        done = 0
        for index, path in enumerate(files, 1):
            progress(config, "import", index - 1, len(files), path.name.split(".")[0])
            if index == 1 and focus:
                focus_clip()
            try:
                run(path, config["gui_calibration"])
            except BaseException as exc:
                progress(config, "import", index - 1, len(files), path.name.split(".")[0], "interrupted")
                raise ValueError(f'{path.name.split(".")[0]}: {exc}') from exc
            done += 1
        progress(config, "import", done, len(files), None, "complete")
        return work_directory(config) / "progress.json"

    run_stage_with_handoff(config, "import", operation)
    return (f"{len(files)} pages saved and layer hierarchy verified; "
            f"completed CMC: {resolve(config['project'])}")


def status(config):
    """Fixed-size state summary and the single next action."""
    options = config["options"]
    work, output = work_directory(config), resolve(config["output_dir"])
    sources = len(__import__("autoclip.common", fromlist=["list_sources"]).list_sources(config))
    seeded = (work / "template/seed.json")
    seeded = seeded.is_file() and read_json(seeded).get("state") == "complete"
    paths = sorted(output.glob("*.layout.json")) if output.is_dir() else []
    loaded = [read_json(p) for p in paths]
    approved = sum(1 for l in loaded if analysis.is_approved(l))
    flagged = [l["page_id"] for l in loaded if l["warnings"] or any(p["warnings"] for p in l["panels"])]
    ready = [p for p in paths if p.with_name(p.name.replace(".layout.json", ".upscale-jobs.json")).is_file()]
    operations = [p for p in paths if p.with_name(p.name.replace(".layout.json", ".operations.json")).is_file()]
    states = [read_json(p) for p in sorted((work / "state").glob("page*.json"))] if (work / "state").is_dir() else []
    complete = sum(1 for s in states if s.get("state") == "complete")
    broken = [s["page_id"] for s in states if s.get("state") != "complete"]
    profile = profile_verified(config)
    if config["options"].get("fit_policy") is None or options.get("upscale") is None:
        nxt = "ask the user the open questions (`configure` prints them), then run `configure` with their answers"
    elif not profile:
        nxt = "GUI profile missing or unverified: connect local Computer Use, then `profile computer --step prepare` / --step finish (docs/PROFILE.html)"
    elif not seeded:
        nxt = "run stage calibrate (init + basic-frame template + page copies)"
    elif not paths:
        nxt = "run stage analyze"
    elif approved < len(paths):
        nxt = f"user reviews output/{config['_slug']}/index.html; after they confirm, run stage approve (reviewer defaults to ユーザー)"
    elif len(ready) < len(paths):
        nxt = "run stage upscale"
    elif len(operations) < len(paths):
        nxt = "run stage prepare, then stage check"
    elif broken:
        nxt = f"recover interrupted pages {short_list(broken, 5)} using docs/guides/recovery.html (do not delete state)"
    elif complete < len(paths):
        nxt = "run stage import"
    else:
        nxt = (f"done: open completed CMC {resolve(config['project'])} in CLIP STUDIO; "
               "input/ is the unchanged original")
    answers = ", ".join(f"{k}={options.get(k)}" for k in ("fit_policy", "upscale", "quality_loss_accepted"))
    lines = [f'job {config["_slug"]}: {sources} images / CMC pages {config["job"].get("cmc_pages", "?")}; answers: {answers}',
             f'profile {"ok" if profile else "MISSING"} | template {"ok" if seeded else "pending"} | analyzed {len(paths)} | '
             f'approved {approved} | upscaled {len(ready)} | operations {len(operations)} | imported {complete}'
             + (f' | interrupted {short_list(broken, 5)}' if broken else ""),
             f'warnings: {short_list(flagged, 10) or "none"}', f"next: {nxt}"]
    print("\n".join(lines))
    return nxt


def list_jobs():
    jobs = sorted(p.parent.name for p in (ROOT / "work").glob("*/job.json"))
    print("jobs: " + (short_list(jobs, 20) or "none (run `scan`, then `configure`)"))
    return jobs
