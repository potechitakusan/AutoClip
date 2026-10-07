from __future__ import annotations

import copy
import glob
import html
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .common import (ROOT, assert_source, audit, layout_digest, list_sources, natural_key, now,
                     read_json, relative, resolve, sha256, write_json)


def reading_order(panels):
    """Group by top edge, then right-to-left; explicitly only a heuristic."""
    rows = []
    for panel in sorted(panels, key=lambda p: p["outer_px"][1]):
        x1, y1, x2, y2 = panel["outer_px"]
        row = next((r for r in rows if abs(r[0]["outer_px"][1] - y1) < min(
            r[0]["outer_px"][3] - r[0]["outer_px"][1], y2 - y1) * .2), None)
        if row is None:
            rows.append([panel])
        else:
            row.append(panel)
    ordered = [p for row in rows for p in sorted(row, key=lambda p: -p["outer_px"][0])]
    for i, panel in enumerate(ordered, 1):
        panel["reading_order"] = i
    return ordered


def rectangle_metrics(gray, contour, box, settings):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    area = cv2.contourArea(contour)
    approx = cv2.approxPolyDP(contour, cv2.arcLength(contour, True) * .006, True)
    pad = min(9, w // 4, h // 4)
    dark = gray[y1:y2, x1:x2] < 180
    edges = [np.any(dark[:pad], axis=0).mean(), np.any(dark[-pad:], axis=0).mean(),
             np.any(dark[:, :pad], axis=1).mean(), np.any(dark[:, -pad:], axis=1).mean()]
    corners = np.array([[x1, y1], [x2-1, y1], [x2-1, y2-1], [x1, y2-1]])
    deviation = max(min(np.linalg.norm(v - c) for c in corners) for v in approx[:, 0, :])
    rectangularity = area / (w * h)
    score = .45 * rectangularity + .40 * min(edges) + .15 * (len(approx) == 4)
    warnings = []
    if len(approx) != 4 or deviation > settings["max_corner_deviation_px"]:
        warnings.append("non_rectangular_or_skewed")
        score = min(score, .6)
    if min(edges) < .8:
        warnings.append("weak_or_open_border")
        score = min(score, .65)
    return {"rectangularity": round(rectangularity, 4),
            "border_support_top_bottom_left_right": [round(float(v), 4) for v in edges],
            "polygon_vertices": len(approx), "corner_deviation_px": round(float(deviation), 3),
            "method": "foreground_contour_and_four_edge_support"}, round(float(score), 4), warnings


def validate_layout(layout):
    w, h = layout["source"]["size_px"]
    ids, orders = set(), set()
    if not layout["panels"]:
        raise ValueError("No panels; correction required")
    for p in layout["panels"]:
        if not isinstance(p["id"], str) or not p["id"].isascii() or not p["id"].replace("_", "").isalnum():
            raise ValueError("Panel IDs must be ASCII letters/numbers/underscores")
        if p["id"] in ids or p["reading_order"] in orders:
            raise ValueError("Duplicate panel ID or reading order")
        ids.add(p["id"]); orders.add(p["reading_order"])
        if not isinstance(p["reading_order"], int) or p["reading_order"] < 1:
            raise ValueError("Reading order must be a positive integer")
        for key in ("outer_px", "inner_px", "crop_px"):
            x1, y1, x2, y2 = p[key]
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in p[key]):
                raise ValueError("Pixel coordinates must be integers")
            if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
                raise ValueError(f"Invalid {key}: {p['id']}")
        a, b = p["outer_px"], p["inner_px"]
        if not (a[0] <= b[0] < b[2] <= a[2] and a[1] <= b[1] < b[3] <= a[3]):
            raise ValueError("Inner rectangle must be inside outer rectangle")
        c = p["crop_px"]
        if not (c[0] <= b[0] < b[2] <= c[2] and c[1] <= b[1] < b[3] <= c[3]):
            raise ValueError("Crop must cover the frame interior")
        if not math.isfinite(p["score"]) or not 0 <= p["score"] <= 1:
            raise ValueError("Invalid score")
    if orders != set(range(1, len(ids)+1)):
        raise ValueError("Reading orders must be consecutive from 1")
    for i, a in enumerate(layout["panels"]):
        for b in layout["panels"][i+1:]:
            aa, bb = a["outer_px"], b["outer_px"]
            if min(aa[2], bb[2]) > max(aa[0], bb[0]) and min(aa[3], bb[3]) > max(aa[1], bb[1]):
                raise ValueError("Overlapping rectangles are unsupported; resolve manually")
    if layout["mapping"].get("frame_workflow") == "template_split":
        from .split_frames import subdivision
        subdivision(layout)


def derive_geometry(layout):
    w, h = layout["source"]["size_px"]
    for panel in layout["panels"]:
        panel["normalized"] = {k.removesuffix("_px"): [round(v / (w if i % 2 == 0 else h), 8)
                             for i, v in enumerate(panel[k])] for k in ("outer_px", "inner_px", "crop_px")}
    layout["updated_at"] = now()


def analyze_image(path, number, config):
    with Image.open(path) as source:
        if source.getexif().get(274, 1) != 1:
            raise ValueError("EXIF rotation is unsupported; normalize explicitly before analysis")
        rgb = np.array(source.convert("RGB"))
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    settings = config["detection"]
    mask = (gray < settings["foreground_threshold"]).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    panels, warnings = [], []
    covered = np.zeros((h, w), dtype=bool)
    for contour in contours:
        if cv2.contourArea(contour) < w * h * settings["min_area_ratio"]:
            continue
        x, y, cw, ch = cv2.boundingRect(contour)
        box = [x, y, x + cw, y + ch]
        evidence, score, pw = rectangle_metrics(gray, contour, box, settings)
        if min(x, y, w-x-cw, h-y-ch) < settings["edge_margin_px"]:
            pw.append("near_page_edge_check_bleed")
        inset = settings["border_inset_px"]
        panels.append({"outer_px": box, "inner_px": [x+inset, y+inset, x+cw-inset, y+ch-inset],
                       "crop_px": box.copy(), "border_inset_px": inset,
                       "score": score, "score_is_probability": False,
                       "evidence": evidence, "warnings": pw, "manual_geometry": False})
        covered[y:y+ch, x:x+cw] = True
    for i, panel in enumerate(reading_order(panels), 1):
        panel["id"] = f"p{i:03d}"
    residual = int(((gray < 225) & ~covered).sum())
    if residual / (w*h) > settings["unassigned_ink_ratio"]:
        warnings.append("content_outside_panels_preserved_on_separate_layer")
    if not panels:
        warnings.append("no_panels_detected")
    if any(p["score"] < settings["min_score"] for p in panels):
        warnings.append("low_confidence_panels")
    layout = {"schema_version": 1, "page_id": f"page{number:04d}", "page_number": number,
              "source": {"path": relative(path), "size_px": [w, h], "sha256": sha256(path),
                         "rotation_degrees": 0},
              "coordinate_convention": "xyxy; origin=top-left; right/bottom exclusive; integer pixels",
              "reading_direction": "right-to-left, top-to-bottom; estimated; manually editable",
              "canvas": copy.deepcopy(config["canvas"]), "mapping": copy.deepcopy(config["mapping"]),
              "detection_settings": copy.deepcopy(settings), "panels": reading_order(panels),
              "unassigned_foreground_pixels": residual, "warnings": warnings, "approval": None}
    derive_geometry(layout)
    return layout


def font(size):
    for path in ("C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_review(layout, path):
    assert_source(layout)
    image = Image.open(resolve(layout["source"]["path"])).convert("RGB")
    w, h = image.size
    right = image.convert("RGBA")
    overlay = Image.new("RGBA", image.size)
    d = ImageDraw.Draw(overlay)
    palette = [(0, 105, 255), (220, 45, 90), (0, 155, 110), (150, 55, 210), (195, 115, 0)]
    for i, p in enumerate(layout["panels"]):
        c = palette[i % len(palette)]
        d.rectangle(p["outer_px"], fill=(*c, 20), outline=(*c, 255), width=4)
        x1, y1, x2, y2 = p["crop_px"]
        # Dashed orange line just outside the crop boundary so coincident outer/crop remains legible.
        for x in range(x1, x2, 16):
            d.line([(x, y1-3), (min(x+8, x2), y1-3)], fill=(255, 130, 0), width=2)
            d.line([(x, y2+3), (min(x+8, x2), y2+3)], fill=(255, 130, 0), width=2)
        for y in range(y1, y2, 16):
            d.line([(x1-3, y), (x1-3, min(y+8, y2))], fill=(255, 130, 0), width=2)
            d.line([(x2+3, y), (x2+3, min(y+8, y2))], fill=(255, 130, 0), width=2)
        d.rectangle(p["inner_px"], outline=(0, 230, 235, 255), width=2)
        warning = " / 要確認" if p["warnings"] or p["score"] < layout["detection_settings"]["min_score"] else ""
        label = f'{p["id"]} / 順序{p["reading_order"]} / 指標{p["score"]:.2f}{warning}'
        pos = (x1+9, y1+12)
        bounds = d.textbbox(pos, label, font=font(23))
        d.rectangle((bounds[0]-4, bounds[1]-5, bounds[2]+4, bounds[3]+5), fill=(255,255,255,235))
        d.text(pos, label, fill=(*c,255), font=font(23))
    right = Image.alpha_composite(right, overlay).convert("RGB")
    sheet = Image.new("RGB", (w*2+24, h+190), "#f1f3f6")
    sheet.paste(image, (0, 56)); sheet.paste(right, (w+24, 56))
    d = ImageDraw.Draw(sheet)
    d.text((12, 8), f'{layout["page_id"]} 原画像', fill="#1b2434", font=font(28))
    d.text((w+36, 8), "検出結果・人間の確認待ち" if not is_approved(layout) else "検出結果・確認済み", fill="#1b2434", font=font(28))
    d.text((12, h+69), "色実線: 外枠 / 橙破線: 切出し範囲 / 水色線: 推定内側（内側余白は設定・補正可）", fill="#1b2434", font=font(25))
    d.text((12, h+106), "読み順は推定。指標は確率ではありません。タイトルなどコマ外の内容も確認してください。", fill="#1b2434", font=font(25))
    d.text((12, h+146), "警告: " + (", ".join(layout["warnings"]) or "なし"), fill="#a34222", font=font(20))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    if layout["mapping"].get("frame_workflow") == "template_split":
        render_placement(layout, path.with_name(layout["page_id"] + ".placement.png"))


def render_placement(layout, path):
    from .operations import mapping
    from .split_frames import subdivision
    transform, split = mapping(layout), subdivision(layout)
    original = Image.open(resolve(layout["source"]["path"])).convert("RGBA")
    context = original.copy()
    draw = ImageDraw.Draw(context)
    for panel in layout["panels"]:
        x1,y1,x2,y2 = panel["outer_px"]
        draw.rectangle([x1,y1,x2-1,y2-1], fill=(0,0,0,0))
    size = tuple(layout["canvas"]["pixel_size"])
    page = Image.new("RGBA", size, "white")
    x1,y1,x2,y2 = [round(v) for v in transform["image_rect_px"]]
    page.alpha_composite(context.resize((x2-x1,y2-y1), Image.Resampling.LANCZOS), (x1,y1))
    for panel in layout["panels"]:
        rect = split["leaves"][panel["id"]]["rect_px"]
        bleed = split["image_overscan_px"]
        x1,y1,x2,y2 = [round(v) for v in rect]
        art = original.crop(panel["inner_px"]).resize((x2-x1+2*bleed,y2-y1+2*bleed),Image.Resampling.LANCZOS)
        page.alpha_composite(art.crop((bleed,bleed,art.width-bleed,art.height-bleed)),(x1,y1))
        ImageDraw.Draw(page).rectangle((x1,y1,x2,y2),outline="black",width=round(split["frame_line_width_px"]))
    # Guides and paper remain visible so equal placement between pages is reviewable.
    d = ImageDraw.Draw(page)
    factor = layout["canvas"]["dpi"]/25.4
    d.rectangle([round(v*factor) for v in layout["canvas"]["trim_mm"]],outline="#93b5df",width=2)
    sheet = Image.new("RGB", (size[0],size[1]+165), "#f1f3f6")
    sheet.paste(page.convert("RGB"),(0,70))
    d = ImageDraw.Draw(sheet)
    sx,sy = transform["scale_xy"]
    d.text((20,12),f'{layout["page_id"]} 基本枠へ分割配置 / 横 {sx:.4f}倍・縦 {sy:.4f}倍',font=font(30),fill="#1b2434")
    d.text((20,size[1]+78),f'配置見本（原画像使用）。塗り足し{split["image_overscan_px"]}px。実機の分割位置・間隔は画面と整数pxへ丸めます。',font=font(23),fill="#1b2434")
    adjustments = " / ".join(f'{a["panel_id"]}: {a["edge_delta_source_px"]}' for a in split["edge_adjustments"]) or "なし"
    d.text((20,size[1]+116),f'辺をそろえる補正（元画像px・左/上/右/下）: {adjustments}',font=font(23),fill="#1b2434")
    sheet.save(path)


def is_approved(layout):
    a = layout.get("approval")
    return bool(a and a.get("layout_sha256") == layout_digest(layout))


def require_approval(layout):
    validate_layout(layout)
    assert_source(layout)
    if not is_approved(layout):
        raise ValueError(f'{layout["page_id"]}: human review is missing or stale')
    if any(p["score"] < layout["detection_settings"]["min_score"] for p in layout["panels"]):
        raise ValueError("Low-confidence geometry must be corrected before import")


def write_index(output):
    rows = []
    for path in sorted(output.glob("*.layout.json")):
        l = read_json(path)
        warning = l["warnings"] + [f'{p["id"]}:{v}' for p in l["panels"] for v in p["warnings"]]
        status = "確認済み" if is_approved(l) else ("要確認" if warning else "OK候補・確認待ち")
        name = html.escape(l["page_id"])
        placement = f' / <a href="{name}.placement.png">基本枠への配置見本</a>' if l["mapping"].get("frame_workflow") == "template_split" else ""
        rows.append(f'<tr><td>{name}</td><td>{status}</td><td>{len(l["panels"])}</td>'
                    f'<td>{html.escape(", ".join(warning) or "なし")}</td><td>'
                    f'<a href="{name}.review.png">左右比較を開く</a>{placement} / <a href="{name}.layout.json">JSON</a></td></tr>')
    page = '''<!doctype html><html lang="ja"><meta charset="utf-8"><title>漫画コマ解析・確認一覧</title>
<style>body{font:16px sans-serif;margin:40px;background:#f5f6f9;color:#202838}table{border-collapse:collapse;background:white;width:100%}td,th{padding:16px;text-align:left;border-bottom:1px solid #dde1e8}a{color:#164cc0}p{line-height:1.8}</style>
<h1>漫画コマ解析・確認一覧</h1><p>各ページの左右比較で、境界・切出し・読み順・コマ外のタイトルを確認してください。<br>解析上のOKは人間の確認済みを意味しません。確認後にapproveコマンドを実行します。</p>
<table><tr><th>ページ</th><th>状態</th><th>コマ数</th><th>警告</th><th>確認資料</th></tr>'''+"".join(rows)+"</table></html>"
    (output / "index.html").write_text(page, encoding="utf-8")


def analyze(config, quiet=False):
    """Analyze every page. Per-page lines are optional; the summary has a fixed size."""
    output = resolve(config["output_dir"])
    files = list_sources(config)
    if not files:
        raise ValueError("No source images")
    flagged, total_panels = [], 0
    for number, path in enumerate(files, config["page_start"]):
        layout = analyze_image(Path(path), number, config)
        target = output / f'{layout["page_id"]}.layout.json'
        if target.exists():
            previous = read_json(target)
            if previous.get("corrections"):
                raise ValueError(f"{target.name} has manual corrections; use a different output directory to reanalyze")
            if layout_digest(previous) == layout_digest(layout):
                layout["approval"] = previous.get("approval")
        write_json(target, layout)
        render_review(layout, output / f'{layout["page_id"]}.review.png')
        audit("analyze", page=layout["page_id"], panels=len(layout["panels"]))
        total_panels += len(layout["panels"])
        if layout["warnings"] or any(p["warnings"] for p in layout["panels"]):
            flagged.append(layout["page_id"])
        if not quiet:
            print(f'{layout["page_id"]}: {len(layout["panels"])} panels; {layout["warnings"]}', flush=True)
    write_index(output)
    shown = ", ".join(flagged[:10]) + (f" +{len(flagged)-10} more" if len(flagged) > 10 else "")
    print(f"analyzed {len(files)} pages, {total_panels} panels, {len(flagged)} with warnings: {shown or 'none'}")
    return {"pages": len(files), "panels": total_panels, "flagged": flagged}


def correct(path, patch_path):
    layout, patch = read_json(path), read_json(patch_path)
    panels = {p["id"]: p for p in layout["panels"]}
    for ident in patch.get("delete", []):
        del panels[ident]
    for entry in patch.get("upsert", []):
        ident = entry["id"]
        item = panels.get(ident, {"score": 0., "warnings": ["manual_geometry_requires_review"],
                                  "evidence": {"method": "manual"}, "score_is_probability": False})
        for key in ("id", "outer_px", "inner_px", "crop_px", "reading_order"):
            if key in entry:
                item[key] = entry[key]
        if any(key in entry for key in ("outer_px", "inner_px", "crop_px")):
            item["manual_geometry"] = True
            # Explicit, human-supplied correction can replace uncertain automatic geometry.
            if patch.get("geometry_checked_by"):
                item["score"] = 1.0
                item["evidence"] = {"method": "human_corrected", "checked_by": patch["geometry_checked_by"]}
                item["warnings"] = ["human_corrected_geometry"]
        panels[ident] = item
    layout["panels"] = sorted(panels.values(), key=lambda p: p["reading_order"])
    layout["approval"] = None
    layout.setdefault("corrections", []).append({"patch_sha256": sha256(patch_path), "at": now()})
    derive_geometry(layout); validate_layout(layout)
    write_json(path, layout)
    render_review(layout, path.with_name(layout["page_id"] + ".review.png"))
    write_index(path.parent)
    audit("correct", page=layout["page_id"])


def approve(path, reviewer, acknowledge, index=True):
    layout = read_json(path)
    derive_geometry(layout); validate_layout(layout); assert_source(layout)
    if any(p["score"] < layout["detection_settings"]["min_score"] for p in layout["panels"]):
        raise ValueError("Low-confidence panels require explicit geometry corrections")
    warnings = layout["warnings"] + [w for p in layout["panels"] for w in p["warnings"]]
    if warnings and not acknowledge:
        raise ValueError("Warnings require --ack-warnings after visual review")
    layout["approval"] = {"reviewer": reviewer, "at": now(), "layout_sha256": layout_digest(layout),
                          "acknowledged_warnings": warnings}
    write_json(path, layout)
    render_review(layout, path.with_name(layout["page_id"] + ".review.png"))
    if index:
        write_index(path.parent)
    audit("approve", page=layout["page_id"], reviewer=reviewer)
