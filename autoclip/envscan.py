"""Find an existing ComfyUI / StabilityMatrix Python and upscale model. Read-only: nothing is installed or changed."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .common import CODE_ROOT, env_values
from .models import inspect_model

PYTHONS = ("python_embeded/python.exe", "venv/Scripts/python.exe", ".venv/Scripts/python.exe",
           "Packages/*/venv/Scripts/python.exe", "Data/Packages/*/venv/Scripts/python.exe")
MODEL_DIRS = ("models/upscale_models", "models/ESRGAN", "models/RealESRGAN",
              "Data/Models/ESRGAN", "Data/Models/RealESRGAN", "Data/Models/upscale_models")
PROBE = """
import json
r = {}
for name in ("torch", "safetensors", "numpy", "PIL"):
    try:
        r[name] = getattr(__import__(name), "__version__", "ok")
    except Exception:
        r[name] = None
try:
    import torch
    r["cuda"] = bool(torch.cuda.is_available())
except Exception:
    r["cuda"] = False
print("PROBE" + json.dumps(r))
"""


def default_roots():
    """Roots to look in: an explicit AUTOCLIP_SEARCH list and, if present, COMFY_DIR from the local .env."""
    roots = [Path(p) for p in os.environ.get("AUTOCLIP_SEARCH", "").split(os.pathsep) if p]
    try:
        comfy = env_values(".env").get("COMFY_DIR")
        if comfy:
            roots.append(Path(comfy))
    except OSError:
        pass
    return roots


def find(roots):
    pythons, models, seen = [], [], set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for base in [root, *list(root.parents)[:2]]:
            for pattern in PYTHONS:
                for path in sorted(base.glob(pattern)):
                    if path.resolve() not in seen:
                        seen.add(path.resolve()); pythons.append(path)
            for folder in MODEL_DIRS:
                for path in sorted((base / folder).glob("*.safetensors")):
                    if path.resolve() not in seen:
                        seen.add(path.resolve())
                        models.append({"path": path, **inspect_model(path)})
    return pythons, models


def probe_python(python, timeout=120):
    """Import-check an interpreter without writing bytecode. Returns the package versions or an error."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        done = subprocess.run([str(python), "-B", "-s", "-c", PROBE], capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=str(CODE_ROOT))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    for line in done.stdout.splitlines():
        if line.startswith("PROBE"):
            info = json.loads(line[5:])
            info["ok"] = all(info[k] for k in ("torch", "safetensors", "numpy", "PIL"))
            return info
    return {"ok": False, "error": (done.stderr or "no probe output")[-200:]}
