"""Standalone RealESRGAN inference. This module never imports OpenCV or ComfyUI."""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import time

import numpy as np
from PIL import Image
import torch
from torch.nn import functional as F
from safetensors.torch import load_file

from .config import ROOT, now, read_json, sha256, write_json
from .models import inspect_model
from .rrdb import RRDBNet
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def relative(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def audit(event, **fields):
    path = ROOT / "work/logs/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False) + "\n")


def model_settings(model, long_edge=4096):
    """Settings for an explicit model file; no ComfyUI workflow or .env involved."""
    model = Path(model)
    info = inspect_model(model)
    if not info["compatible"]:
        raise ValueError(f"Unsupported upscale model {model.name}: {info['reason']}")
    return {"model": model, "model_sha256": sha256(model), "long_edge": int(long_edge),
            "num_block": info["num_block"], "scale": 4, "resize_method": "area"}


def output_size(size, long_edge):
    w, h = size
    return (max(1, round(w * long_edge / max(size))), max(1, round(h * long_edge / max(size))))


class Upscaler:
    def __init__(self, settings, device="cuda", tile=256, pad=32, precision="fp32"):
        if device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA requested but unavailable; use --device cpu explicitly")
        if tile < 32 or pad < 8 or pad >= tile:
            raise ValueError("Invalid tile/pad sizes")
        if precision == "fp16" and device != "cuda":
            raise ValueError("fp16 requires CUDA")
        self.settings, self.tile, self.pad = settings, tile, pad
        self.device = torch.device(device)
        self.dtype = torch.float16 if precision == "fp16" else torch.float32
        self.precision = precision
        self.model = RRDBNet(settings.get("num_block", 23))
        self.model.load_state_dict(load_file(str(settings["model"]), device="cpu"), strict=True)
        self.model.eval().requires_grad_(False).to(device=self.device, dtype=self.dtype)

    @torch.inference_mode()
    def infer(self, rgb, tile):
        h, w = rgb.shape[:2]
        # Accumulate on CPU to bound GPU memory independently of the full output size.
        result = torch.empty((1, 3, h*4, w*4), dtype=torch.float32)
        count = math.ceil(w/tile)*math.ceil(h/tile)
        step = 0
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                x2, y2 = min(x+tile, w), min(y+tile, h)
                left, top = max(0, x-self.pad), max(0, y-self.pad)
                right, bottom = min(w, x2+self.pad), min(h, y2+self.pad)
                patch = torch.from_numpy(rgb[top:bottom, left:right].copy()).permute(2,0,1).unsqueeze(0)
                patch = patch.to(device=self.device, dtype=self.dtype) / 255.
                prediction = self.model(patch)
                result[:, :, y*4:y2*4, x*4:x2*4] = prediction[:, :, (y-top)*4:(y2-top)*4,
                                                                        (x-left)*4:(x2-left)*4].float().cpu()
                step += 1
                print(f"  tile {step}/{count}", flush=True)
        return result.clamp_(0, 1)

    def run(self, input_path, output_path, long_edge=None):
        input_path, output_path = Path(input_path), Path(output_path)
        if input_path.resolve() == output_path.resolve():
            raise ValueError("Input and output must differ")
        edge = long_edge or self.settings["long_edge"]
        if not 16 <= edge <= 16384:
            raise ValueError("Long edge must be 16..16384")
        key = {"input_sha256": sha256(input_path), "model_sha256": self.settings["model_sha256"],
               "long_edge": edge, "resize_method": "area", "tile": self.tile, "pad": self.pad,
               "precision": self.precision, "engine": "autoclip-rrdb-x4-v1"}
        metadata_path = output_path.with_suffix(".upscale.json")
        if metadata_path.exists() and output_path.exists():
            previous = read_json(metadata_path)
            if previous.get("job_key") == digest(key) and previous.get("output_sha256") == sha256(output_path):
                with Image.open(output_path) as im:
                    if list(im.size) == previous["output_size_px"]:
                        print(f"cached: {output_path}", flush=True)
                        return previous
        started = time.monotonic()
        with Image.open(input_path) as source:
            if source.mode not in ("RGB", "L"):
                raise ValueError("Minimal runner accepts RGB/L opaque images only")
            if source.getexif().get(274, 1) != 1:
                raise ValueError("Normalize EXIF orientation first")
            rgb = np.array(source.convert("RGB"))
        tile = self.tile
        while True:
            try:
                result = self.infer(rgb, tile)
                break
            except torch.cuda.OutOfMemoryError:
                if tile <= 64:
                    raise
                tile //= 2
                torch.cuda.empty_cache()
                print(f"CUDA OOM: restarting image with tile={tile}", flush=True)
        size = output_size((rgb.shape[1], rgb.shape[0]), edge)
        result = F.interpolate(result, size=(size[1], size[0]), mode="area")
        pixels = (result[0].permute(1,2,0).numpy()*255).round().clip(0,255).astype(np.uint8)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(".tmp.png")
        Image.fromarray(pixels).save(tmp)
        os.replace(tmp, output_path)
        metadata = {**key, "job_key": digest(key), "input": relative(input_path), "output": relative(output_path),
                    "input_size_px": [rgb.shape[1],rgb.shape[0]], "output_size_px": list(size),
                    "effective_tile": tile, "output_sha256": sha256(output_path),
                    "duration_seconds": round(time.monotonic()-started, 2), "completed_at": now(),
                    "torch_version": torch.__version__, "device": str(self.device),
                    "note": "Same weights/architecture and area-resize policy; tiling differs from ComfyUI feather blending."}
        write_json(metadata_path, metadata)
        audit("upscale", output=relative(output_path), seconds=metadata["duration_seconds"])
        print(f"saved: {output_path} {size} {metadata['duration_seconds']}s", flush=True)
        return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--long-edge", type=int)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--precision", choices=["fp32", "fp16"], default="fp32")
    parser.add_argument("--tile", type=int, default=256)
    parser.add_argument("--pad", type=int, default=32)
    args = parser.parse_args()
    settings = model_settings(args.model, args.long_edge or 4096)
    runner = Upscaler(settings, args.device, args.tile, args.pad, args.precision)
    runner.run(args.input, args.output, args.long_edge)


if __name__ == "__main__":
    main()
