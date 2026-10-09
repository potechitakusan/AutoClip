"""Download the official RealESRGAN x4plus weights and convert them to .safetensors.

The upstream file is a pickle (.pth). It is only opened with torch.load(weights_only=True) inside the
built-in environment, and only after its SHA-256 matches the pinned value; the converted file is a plain
tensor container that the upscaler can read without executing any code.
"""
from __future__ import annotations

import hashlib
import subprocess
import urllib.request
from pathlib import Path

from .config import ROOT, sha256

URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
PTH_SHA256 = "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"  # official v0.1.0 release asset
CONVERT = """
import sys, torch
from safetensors.torch import save_file
state = torch.load(sys.argv[1], map_location="cpu", weights_only=True)
state = state.get("params_ema") or state.get("params") or state
save_file({k: v.contiguous() for k, v in state.items()}, sys.argv[2])
"""


def fetch(dest="models"):
    folder = Path(dest)
    if not folder.is_absolute():
        folder = ROOT / folder
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "RealESRGAN_x4plus.safetensors"
    if target.is_file():
        return f"already present: {target}"
    venv = ROOT / ".venv/Scripts/python.exe"
    if not venv.is_file():
        raise ValueError("Built-in environment missing; run scripts/setup.ps1 -BuiltinUpscale first (docs/SETUP.md)")
    pth = folder / "RealESRGAN_x4plus.pth.download"
    with urllib.request.urlopen(URL, timeout=60) as response, pth.open("wb") as stream:
        digest = hashlib.sha256()
        while block := response.read(1 << 20):
            digest.update(block)
            stream.write(block)
    if digest.hexdigest() != PTH_SHA256:
        pth.unlink()
        raise ValueError(f"Downloaded file hash {digest.hexdigest()} differs from the pinned value; not converting")
    done = subprocess.run([str(venv), "-B", "-c", CONVERT, str(pth), str(target)], capture_output=True, text=True)
    pth.unlink()
    if done.returncode:
        raise ValueError("Conversion failed: " + done.stderr.strip()[-300:])
    return f"saved {target} (sha256 {sha256(target)})"
