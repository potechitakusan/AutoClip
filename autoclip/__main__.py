import argparse
import sys
from pathlib import Path

from .common import ROOT, load_config, read_json, resolve, write_json


def build_parser():
    parser = argparse.ArgumentParser(description="AutoClip: manga pages -> CLIP STUDIO frame-border folders")
    parser.add_argument("--config", help="legacy: explicit config file (prefer --job)")
    parser.add_argument("--job", help="job name (work/<job>/job.json); optional when only one job exists")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="inspect input/; environment discovery requires user permission")
    scan.add_argument("--discover-env", action="store_true", help="search existing environments only after the user permits it")
    scan.add_argument("--search", nargs="*", default=[], help="extra folders to search with --discover-env, after user permission")

    configure = commands.add_parser("configure", help="create or update a job and record the user's answers")
    configure.add_argument("--name")
    configure.add_argument("--images", help="folder with the PNG/JPG pages (inside input/)")
    configure.add_argument("--project", help=".cmc file (inside input/)")
    configure.add_argument("--fit", choices=["stretch", "keep_aspect"])
    configure.add_argument("--upscale", choices=["builtin", "external", "none"])
    configure.add_argument("--long-edge", help="'auto' (default) or a pixel count")
    configure.add_argument("--python", help="external python.exe (ComfyUI python_embeded / venv)")
    configure.add_argument("--model", help="x4 RRDBNet .safetensors")
    configure.add_argument("--accept-quality-loss", action="store_true")
    configure.add_argument("--profile")
    configure.add_argument("--search", nargs="*", default=[])

    after = argparse.ArgumentParser(add_help=False)  # lets `--job` follow the sub-command as well
    after.add_argument("--job", default=argparse.SUPPRESS)
    commands.add_parser("status", parents=[after], help="fixed-size job status and the next action")

    run = commands.add_parser("run", parents=[after], help="run one stage for all pages")
    from .stages import STAGES
    run.add_argument("stage", choices=STAGES)
    run.add_argument("--page", type=int, help="limit to one page number")
    run.add_argument("--reviewer", default="ユーザー", help="reviewer name (default: ユーザー)")
    run.add_argument("--ack-warnings", action="store_true", help="approve: the human also reviewed the warnings")
    run.add_argument("--no-focus", action="store_true", help="do not bring CLIP STUDIO to the front automatically")

    profile = commands.add_parser("profile", parents=[after], help="GUI profile for this screen and paper size")
    profile.add_argument("action", choices=["computer", "agent", "auto", "record", "verify", "show"])
    profile.add_argument("--line-px", type=float, help="record: frame line width typed into CLIP STUDIO (default from the job)")
    profile.add_argument('--step', choices=['prepare', 'start', 'capture', 'point', 'click', 'finish'])
    profile.add_argument('--observations', help='computer finish: observed coordinates JSON inside the job profile folder')
    profile.add_argument('--point', help='agent: control name printed by the current step')
    profile.add_argument('--x', type=int)
    profile.add_argument('--y', type=int)
    profile.add_argument('--snapshot', help='agent: SHA-256 of the screenshot the coordinates came from')
    profile.add_argument('--tab-rect', nargs=4, type=int, help='agent capture: visually identified active scratch tab label rectangle (exclude modified marker)')

    fetch = commands.add_parser("fetch-model", help="download and convert the official RealESRGAN x4plus weights")
    fetch.add_argument("--dest", type=Path, default=Path("models"))

    # Legacy / low-level commands (used by the stages and by maintainers).
    commands.add_parser("analyze")
    commands.add_parser("init")
    calibration = commands.add_parser("calibrate"); calibration.add_argument("--calibration")
    commands.add_parser("seed-frames")
    review = commands.add_parser("review"); review.add_argument("layout", type=Path)
    correction = commands.add_parser("correct"); correction.add_argument("layout", type=Path); correction.add_argument("patch", type=Path)
    approval = commands.add_parser("approve"); approval.add_argument("layout", type=Path); approval.add_argument("--reviewer", default="ユーザー"); approval.add_argument("--ack-warnings", action="store_true")
    crop = commands.add_parser("crop"); crop.add_argument("layout", type=Path, nargs="?"); crop.add_argument("--all", action="store_true"); crop.add_argument("--long-edge", type=int, default=4096)
    operations = commands.add_parser("operations"); operations.add_argument("layout", type=Path); operations.add_argument("--draft", action="store_true")
    check = commands.add_parser("verify"); check.add_argument("operations", type=Path)
    assets = commands.add_parser("assets"); assets.add_argument("operations", type=Path)
    inspect = commands.add_parser("inspect-project"); inspect.add_argument("project", type=Path, nargs="?"); inspect.add_argument("--output", type=Path, default=Path("work/project-inspection.json"))
    backup = commands.add_parser("backup"); backup.add_argument("destination", type=Path)
    return parser


def job_config(args):
    from . import stages
    slug = args.job
    if not slug:
        jobs = [p.parent.name for p in (ROOT / "work").glob("*/job.json")]
        if len(jobs) != 1:
            raise ValueError("Several or no jobs; pass --job NAME (`status` without a job lists them)" if jobs else "No job yet; run `scan` and `configure`")
        slug = jobs[0]
    return stages.load_job(slug)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if args.command == "scan":
        from .jobs import scan
        scan(args.search, discover_env=args.discover_env)
        return
    if args.command == "configure":
        from .jobs import configure
        configure(args.name, args.images, args.project, args.fit, args.upscale, args.long_edge, args.python,
                  args.model, args.accept_quality_loss, args.profile, args.search)
        return
    if args.command == "fetch-model":
        from .fetch_model import fetch
        print(fetch(args.dest))
        return
    if args.command in ("status", "run", "profile"):
        from . import stages
        if args.command == "status" and not args.job and len(list((ROOT / "work").glob("*/job.json"))) != 1:
            stages.list_jobs()
            return
        config = job_config(args)
        if args.command == "status":
            stages.status(config)
        elif args.command == "run":
            stages.run_stage(config, args.stage, args.page, args.reviewer, args.ack_warnings, not args.no_focus)
        else:
            if args.action == 'computer':
                from .computer_profile import main as computer_profile
                computer_profile(config, args.step or 'prepare', args.observations)
            elif args.action == 'agent':
                from .agent_profile import main as agent_profile
                agent_profile(config, args.step or 'start', args.point, args.x, args.y, args.snapshot, args.line_px, args.tab_rect)
            else:
                from . import profile as gui_profile
                gui_profile.main(config, args.action, args.line_px)
        return
    if not (args.config or args.job):
        raise ValueError("Pass --job NAME (or --config FILE for a legacy config)")
    config = load_config(args.config if not args.job else f"work/{args.job}/job.json")
    from . import analysis
    from . import operations as ops
    from . import project
    if args.command == "init":
        print(project.initialize_project(config))
    elif args.command == "calibrate":
        from .split_gui import calibrate
        print(calibrate(config, args.calibration or config["gui_calibration"]))
    elif args.command == "seed-frames":
        from .split_frames import seed_pages
        print(seed_pages(config))
    elif args.command == "analyze":
        analysis.analyze(config)
    elif args.command == "review":
        layout = read_json(args.layout)
        analysis.derive_geometry(layout); analysis.validate_layout(layout)
        if not analysis.is_approved(layout):
            layout["approval"] = None
        write_json(args.layout, layout)
        analysis.render_review(layout, args.layout.with_name(layout["page_id"] + ".review.png"))
        analysis.write_index(args.layout.parent)
    elif args.command == "correct":
        analysis.correct(args.layout, args.patch)
    elif args.command == "approve":
        analysis.approve(args.layout, args.reviewer, args.ack_warnings)
    elif args.command == "crop":
        if args.all == bool(args.layout):
            raise ValueError("Specify one layout or --all")
        paths = sorted(resolve(config["output_dir"]).glob("*.layout.json")) if args.all else [args.layout]
        for path in paths:
            print(ops.crop_layout(path, resolve(config["output_dir"]), args.long_edge))
    elif args.command == "operations":
        print(ops.build_operations(args.layout, config, args.draft))
    elif args.command == "verify":
        plan = ops.verify_operations(args.operations); print(plan["page_id"], "verified")
    elif args.command == "assets":
        print(ops.prepare_assets(args.operations))
    elif args.command == "inspect-project":
        write_json(args.output, project.inspect_project(args.project or resolve(config["project"]))); print(args.output)
    elif args.command == "backup":
        project.backup_project(resolve(config["project"]), args.destination)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError, KeyError, OSError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        sys.exit(2)
