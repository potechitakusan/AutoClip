"""Launcher for running the upscaler with an external interpreter (e.g. ComfyUI's python_embeded).

The embedded interpreter ignores the working directory and PYTHONPATH, so the repository is added
explicitly. Run it with `python -B -s scripts/upscale_runner.py ...` so nothing is written next to the
external installation.
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoclip.inference import main  # noqa: E402

if __name__ == "__main__":
    main()
