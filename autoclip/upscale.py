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

from .common import (audit, digest, env_values, now, read_json, relative, resolve,
                     sha256, write_json)
from .models import inspect_model
from .rrdb import RRDBNet


def workflow_settings(env_path=".env"):
    env = env_values(env_path)
    workflow = Path(env["COMFY_UPSCALE"])
    data = read_json(workflow)
    nodes = {n["id"]: n for n in data["nodes"]}
    links = {link[0]: link for link in data["links"]}

    def source(node, name):
        slot = next(i for i in node["inputs"] if i["name"] == name)
        if slot.get("link") is None:
            return None
        return nodes[links[slot["link"]][1]]

    outputs = [n for n in nodes.values() if n["type"] == "SaveImage" and n.get("mode", 0) == 0]
    if len(outputs) != 1:
        raise ValueError("Expected exactly one active PNG SaveImage output")
    resize = source(outputs[0], "images")
    if resize["type"] != "ResizeImageMaskNode":
        raise ValueError("Unsupported workflow: expected ResizeImageMaskNode before SaveImage")
    named = resize.get("widgets_values_named", {})
    if named.get("resize_type") != "scale longer dimension" or named.get("scale_method") != "area":
        raise ValueError("Only longer-dimension area resize is implemented")
    long_node = source(resize, "resize_type.longer_size")
    if long_node is not None and long_node["type"] != "PrimitiveInt":
        raise ValueError("Unsupported dynamic long-edge input")
    long_edge = (long_node.get("widgets_values_named", {}).get("value", long_node["widgets_values"][0])
                 if long_node else named["resize_type.longer_size"])
    upscale = source(resize, "input")
    if upscale["type"] != "ImageUpscaleWithModel":
        raise ValueError("Unsupported upscale node")
    loader = source(upscale, "upscale_model")
    if loader["type"] != "UpscaleModelLoader" or source(upscale, "image")["type"] != "LoadImage":
        raise ValueError("Only direct LoadImage -> ImageUpscaleWithModel is supported")
    model_name = loader.get("widgets_values_named", {}).get("model_name", loader["widgets_values"][0])
    if model_name != "RealESRGAN_x4plus.safetensors":
        raise ValueError("This minimal runner supports RealESRGAN_x4plus.safetensors only")
    model = Path(env["COMFY_DIR"]) / "models/upscale_models" / model_name
    if not model.is_file():
        raise ValueError("Configured upscale model not found")
    return {"model": model, "model_sha256": sha256(model), "long_edge": int(long_edge),
            "workflow_sha256": sha256(workflow), "scale": 4, "resize_method": "area"}


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
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=Path, nargs="+", help="one or more *.upscale-jobs.json; the model is loaded once")
    parser.add_argument("--model", type=Path, help="x4 RRDBNet .safetensors; without it the .env ComfyUI workflow is read")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--long-edge", type=int)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--precision", choices=["fp32", "fp16"], default="fp32")
    parser.add_argument("--tile", type=int, default=256)
    parser.add_argument("--pad", type=int, default=32)
    args = parser.parse_args()
    if bool(args.jobs) == bool(args.input and args.output):
        parser.error("Provide either --jobs or --input and --output")
    settings = model_settings(args.model, args.long_edge or 4096) if args.model else workflow_settings(args.env)
    runner = Upscaler(settings, args.device, args.tile, args.pad, args.precision)
    if args.jobs:
        done = 0
        for path in args.jobs:
            jobs = read_json(path)
            for job in jobs["images"]:
                edge = job.get("long_edge", jobs["long_edge"])
                if edge is None:
                    continue  # copied without a model by the pipeline
                if sha256(resolve(job["input"])) != job["input_sha256"]:
                    raise ValueError("Crop changed since jobs manifest was created")
                runner.run(resolve(job["input"]), resolve(job["output"]), edge)
                done += 1
        print(f"upscaled {done} images", flush=True)
    else:
        runner.run(args.input, args.output, args.long_edge)


if __name__ == "__main__":
    main()
