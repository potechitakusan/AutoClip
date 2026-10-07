"""Model-file inspection that needs neither torch nor a third-party package."""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path


def safetensors_header(path):
    """Read only the JSON header of a .safetensors file (tensor names and shapes)."""
    with Path(path).open("rb") as stream:
        raw = stream.read(8)
        if len(raw) != 8:
            raise ValueError("Not a safetensors file")
        size = struct.unpack("<Q", raw)[0]
        if not 2 <= size <= 64 * 1024 * 1024:
            raise ValueError("Unreasonable safetensors header size")
        header = json.loads(stream.read(size).decode("utf-8"))
    header.pop("__metadata__", None)
    return header


def inspect_model(path):
    """Report whether the built-in x4 RRDBNet can load this file, without loading weights.

    Only .safetensors is accepted: .pth/.pt are pickles and are never opened.
    """
    path = Path(path)
    if path.suffix.lower() != ".safetensors":
        return {"compatible": False, "reason": "only .safetensors models are supported (pickle files are never loaded)"}
    try:
        header = safetensors_header(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"compatible": False, "reason": f"unreadable header: {exc}"}
    shapes = {k: v["shape"] for k, v in header.items()}
    needed = ("conv_first.weight", "conv_body.weight", "conv_up1.weight", "conv_up2.weight",
              "conv_hr.weight", "conv_last.weight", "body.0.rdb1.conv1.weight")
    if any(k not in shapes for k in needed):
        return {"compatible": False, "reason": "not a BasicSR RRDBNet x4 layout"}
    blocks = {int(m.group(1)) for k in shapes if (m := re.match(r"body\.(\d+)\.", k))}
    if blocks != set(range(len(blocks))):
        return {"compatible": False, "reason": "non-contiguous RRDB blocks"}
    if (shapes["conv_first.weight"] != [64, 3, 3, 3] or shapes["conv_last.weight"] != [3, 64, 3, 3]
            or shapes["body.0.rdb1.conv1.weight"] != [32, 64, 3, 3]):
        return {"compatible": False, "reason": "feature width/channels differ from the built-in network"}
    return {"compatible": True, "num_block": len(blocks), "reason": "RRDBNet x4, 64 features, 32 growth"}
