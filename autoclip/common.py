from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]  # scripts, defaults, templates, the built-in .venv
# Workspace data (input/, work/, output/, profile/) lives under ROOT; normally the same folder.
ROOT = Path(os.environ["AUTOCLIP_ROOT"]).resolve() if os.environ.get("AUTOCLIP_ROOT") else CODE_ROOT


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def layout_digest(layout):
    return digest({k: v for k, v in layout.items() if k not in {"approval", "updated_at"}})


def resolve(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def relative(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def work_directory(settings):
    """Keep each project's runtime files under its own work/ directory."""
    path = resolve(settings.get("work_dir", "work")).resolve()
    if not path.is_relative_to((ROOT / "work").resolve()):
        raise ValueError("work_dir must be inside work/")
    return path


def natural_key(value):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(value))]


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")


def list_sources(config):
    """Source pages in natural order. `source_dir` accepts PNG/JPG; `source_glob` is the legacy form."""
    import glob
    if config.get("source_dir"):
        folder = resolve(config["source_dir"])
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
        files.sort(key=lambda p: natural_key(p.name))
    else:
        files = sorted(map(Path, glob.glob(str(resolve(config["source_glob"])))), key=natural_key)
    if config.get("page_limit"):
        files = files[:config["page_limit"]]
    return files


def job_config_path(slug):
    return ROOT / "work" / slug / "job.json"


def load_config(path):
    config = read_json(resolve(path))
    if config["mapping"]["fit"] not in {"contain", "cover", "independent_axes"}:
        raise ValueError("Unknown mapping.fit")
    if config["mapping"]["fit"] == "independent_axes" and config["mapping"].get("frame_workflow") != "template_split":
        raise ValueError("independent_axes requires the template_split workflow")
    return config


def env_values(path=".env"):
    values = {}
    for line in resolve(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Existing .env contains duplicate wrapping quotes and a fullwidth underscore.
        values[key.strip().replace("＿", "_")] = value.strip().strip("\"'")
    return values


def assert_source(layout):
    if sha256(resolve(layout["source"]["path"])) != layout["source"]["sha256"]:
        raise ValueError("Source changed; analyze and review again")


def audit(event, **fields):
    path = ROOT / "work/logs/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False) + "\n")
