"""Read-only inspection of the supplied SQLite metadata; never a binary writer."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from .common import ROOT, audit, now, read_json, relative, resolve, sha256, work_directory, write_json


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
                      "clip": inspect_clip(target) if target.is_file() else None})
        current = row["NextIndex"]
    return {"cmc": relative(path), "cmc_sha256": sha256(path), "pages": pages,
            "inspection": "Read-only private metadata; GUI verification remains required",
            "at": now()}


def backup_project(project, destination):
    project, destination = Path(project).resolve(), Path(destination).resolve()
    if not destination.is_relative_to(ROOT / "work"):
        raise ValueError("Backup destination must be inside work/")
    if destination.exists():
        raise ValueError("Backup destination exists; choose a new name")
    info = inspect_project(project)
    destination.mkdir(parents=True)
    files = [project] + [ROOT / p["path"] for p in info["pages"]]
    manifest = []
    for source in files:
        target = destination / source.name
        shutil.copy2(source, target)
        if sha256(source) != sha256(target):
            raise ValueError("Backup hash verification failed")
        manifest.append({"source": relative(source), "backup": relative(target), "sha256": sha256(target)})
    write_json(destination / "backup-manifest.json", {"at": now(), "files": manifest})
    audit("backup", path=relative(destination))
    return manifest


def initialize_project(config):
    """Copy a new input bundle once; never replace an existing work project."""
    source_root = resolve(config["input_dir"]).resolve()
    source_project = resolve(config["source_project"]).resolve()
    work = work_directory(config)
    target = resolve(config["project"]).resolve()
    backup = work / "backups/original"
    manifest_path = work / "initialization.json"
    if not source_root.is_relative_to((ROOT / "input").resolve()):
        raise ValueError("input_dir must be inside input/")
    if not source_project.is_relative_to(source_root):
        raise ValueError("source_project must be inside input_dir")
    if target.parent != work / "project" or target.name != source_project.name:
        raise ValueError("Project must retain its filename inside work_dir/project/")
    files = sorted(p for p in source_root.rglob("*") if p.is_file())
    if not files or any(p.is_symlink() or not p.resolve().is_relative_to(source_root) for p in files):
        raise ValueError("Input bundle is empty or contains external links")
    hashes = {p.relative_to(source_root).as_posix(): sha256(p) for p in files}
    identity = {"input_dir": relative(source_root), "source_project": relative(source_project),
                "project": relative(target), "files": hashes}
    if manifest_path.exists():
        previous = read_json(manifest_path)
        if any(previous.get(k) != v for k, v in identity.items()):
            raise ValueError("Input bundle/config changed; preserve this run and use a new work_dir")
        for name, expected in hashes.items():
            if sha256(backup / name) != expected:
                raise ValueError("Original backup hash mismatch")
        if not target.is_file() or any(not p["exists"] for p in inspect_project(target)["pages"]):
            raise ValueError("Working project is missing files; inspect before recovery")
        return manifest_path
    if target.parent.exists() or backup.exists():
        raise ValueError("Untracked project/backup exists; refusing to overwrite")
    info = inspect_project(source_project)
    if not info["pages"] or any(not p["exists"] for p in info["pages"]):
        raise ValueError("Source CMC has missing pages")
    shutil.copytree(source_root, backup)
    shutil.copytree(source_project.parent, target.parent)
    for name, expected in hashes.items():
        if sha256(source_root / name) != expected or sha256(backup / name) != expected:
            raise ValueError("Input/backup changed during copy")
    for p in source_project.parent.rglob("*"):
        if p.is_file() and sha256(p) != sha256(target.parent / p.relative_to(source_project.parent)):
            raise ValueError("Working project copy hash mismatch")
    write_json(work / "source-project-inspection.json", info)
    write_json(manifest_path, {**identity, "backup": relative(backup), "at": now()})
    audit("initialize_project", project=relative(target), manifest=relative(manifest_path))
    return manifest_path
