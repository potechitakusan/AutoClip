"""GUI profile: where CLIP STUDIO's controls are on *this* screen for *this* paper size.

`agent` locates controls from screenshots without F8. `record` is the optional manual teach-in;
`verify` performs a
synthetic four-panel split on another scratch copy and checks the saved layer structure. Only a profile
that passed `verify` is accepted by the pipeline. Nothing here touches the user's own documents.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .common import ROOT, now, read_json, relative, resolve, work_directory, write_json

STEPS = [
    ("dialog", None, "メニュー『レイヤー→新規レイヤー→コマ枠フォルダー』のダイアログを開く（自動で開きます。開かない場合は手動で開く）"),
    ("new_frame_width", "point", "ダイアログの『線の幅』入力欄の上"),
    ("new_frame_ok", "point", "ダイアログの『OK』ボタンの上（押さないでください）"),
    ("top_folder_name", "point", "レイヤーパレット1行目（コマ枠フォルダー）の名前の上"),
    ("row2_name", "point", "レイヤーパレット2行目（用紙）の名前の上"),
    ("top_folder_toggle", "point", "レイヤーパレット1行目の展開／折りたたみ矢印の上"),
    ("frame_tool", "point", "ツールパレットの『コマ枠』ツールのアイコンの上"),
    ("cut_group", "point", "サブツールパレットの『コマ枠の分割（カット）』グループの上"),
    ("divide_folder", "point", "サブツールパレットの『コマフォルダー分割』の上"),
    ("gap_vertical", "point", "ツールプロパティの『上下の間隔』入力欄の上"),
    ("gap_horizontal", "point", "ツールプロパティの『左右の間隔』入力欄の上"),
    ("selection_tool", "point", "ツールパレットで、コマ枠を選ぶ『操作』ツールのアイコンの上"),
    ("import_image_menu", "point", "メニュー『ファイル→読み込み』の中の『画像』の上（自動でメニューを開きます）"),
]


def profile_path(config):
    return resolve(config["gui_calibration"])


def detect_canvas_rect(image, size_px):
    """Bounding box of the white paper on screen. The whole page must be visible."""
    pixels = np.asarray(image.convert("RGB"))
    mask = (pixels.min(axis=2) >= 235).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))  # trim guides split the paper
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 4)
    aspect = size_px[0] / size_px[1]
    best = None
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if area < 40000 or area / (w * h) < 0.9:
            continue
        error = abs(w / h / aspect - 1)
        if error < 0.02 and (best is None or area > best[4]):
            best = (int(x), int(y), int(w), int(h), int(area))
    if best is None:
        raise ValueError("Cannot see the whole blank page on screen. Show the entire page (View > Fit to screen), "
                         "keep it unscrolled, and use a blank page.")
    x, y, w, h, _ = best
    return [x, y, x + w, y + h]


def refine_rect(image, coarse):
    """The exact boundary rule `assert_blank_canvas` applies at run time."""
    screenshot = np.asarray(image.convert("RGB"))
    x1, y1, x2, y2 = coarse
    left, top = max(0, x1 - 25), max(0, y1 - 9)
    area = screenshot[top:y2 + 9, left:x2 + 25]
    mask = (np.max(area, axis=2) >= 100).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    largest = stats[1:][np.argmax(stats[1:, cv2.CC_STAT_AREA])]
    return [left + int(largest[0]), top + int(largest[1]), left + int(largest[0] + largest[2]), top + int(largest[1] + largest[3])]


def padded(points, margin, limit):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [max(0, min(xs) - margin), max(0, min(ys) - margin), min(limit[0], max(xs) + margin), min(limit[1], max(ys) + margin)]


def scratch_page(config, name):
    """A throw-away copy of the original first page, inside work/ where the GUI runner may open files."""
    from .project import inspect_project
    first = resolve(inspect_project(resolve(config["source_project"]))["pages"][0]["path"])
    target = work_directory(config) / "profile" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(first, target)
    return target


def record(config, line_px=None, teacher_factory=None):
    from .gui import Desktop
    from .split_gui import row  # noqa: F401  (imported to fail early if the GUI module is broken)
    from .stages import focus_clip
    from .teach import Teacher
    line = float(line_px or config["mapping"]["frame_line_width_px"])
    scratch = scratch_page(config, "record.clip")
    focus_clip(1.0)
    desktop = Desktop(None)
    desktop.guard()
    desktop.open(scratch)
    if "*" in desktop.title(desktop.hwnd):
        raise ValueError("Scratch page unexpectedly modified")
    time.sleep(1.0)
    size = config["canvas"]["pixel_size"]
    shot = desktop.capture()
    rect = refine_rect(shot, detect_canvas_rect(shot, size))
    teacher = (teacher_factory or Teacher)(work_directory(config) / "profile/teach.txt",
                      ((rect[0] + rect[2]) // 2 - 390, min(rect[3] + 6, desktop.u.GetSystemMetrics(1) - 130)))
    teacher.total = 12
    points = {}
    try:
        desktop.press("ALT", "L"); desktop.press("E"); desktop.press("C")
        if "コマ枠" not in desktop.title():
            teacher.ask(STEPS[0][2])
        for key, kind, text in STEPS[1:3]:
            points[key] = teacher.ask(text)
        # Fill the dialog with the profile's line width and create the frame in the scratch page.
        desktop.press("CTRL", "A", modal=True); desktop.text("AC_base_frame", modal=True)
        desktop.click(points["new_frame_width"], modal=True)
        desktop.press("CTRL", "A", modal=True); desktop.text(str(line), modal=True)
        desktop.click(points["new_frame_ok"], modal=True)
        desktop.wait_main()
        for key, kind, text in STEPS[3:6]:
            points[key] = teacher.ask(text)
        height = points.pop("row2_name")[1] - points["top_folder_name"][1]
        if not 20 <= height <= 80:
            raise ValueError(f"Layer row height {height}px looks wrong; point at the names of rows 1 and 2")
        for key in ("frame_tool", "cut_group", "divide_folder"):
            points[key] = teacher.ask(dict((k, t) for k, _, t in STEPS)[key])
            desktop.click(points[key])
        for key in ("gap_vertical", "gap_horizontal", "selection_tool"):
            points[key] = teacher.ask(dict((k, t) for k, _, t in STEPS)[key])
        desktop.press("ALT", "F"); desktop.press("I")
        points["import_image_menu"] = teacher.ask(STEPS[-1][2])
        desktop.press("ESC"); desktop.press("ESC")
        teacher.show("記録を保存しています。操作しないでください。")
        screen = [desktop.u.GetSystemMetrics(0), desktop.u.GetSystemMetrics(1)]
        points["hover_away"] = [min(screen[0] - 2, rect[2] + 10), min(screen[1] - 2, rect[3] + 4)]
        # Reference image of the split tool with its options, cursor parked away so no hover highlight remains.
        for key in ("frame_tool", "cut_group", "divide_folder"):
            desktop.click(points[key])
        desktop.u.SetCursorPos(*points["hover_away"])
        time.sleep(0.4)
        stem = Path(config["gui_calibration"]).stem
        reference = resolve(f"profile/{stem}-split.png")
        desktop.capture(reference)
        guards = [padded([points["cut_group"], points["divide_folder"]], 60, screen),
                  padded([points["gap_vertical"], points["gap_horizontal"]], 50, screen)]
        calib = {"schema_version": 1, "environment": desktop.environment(), "canvas_screen_rect": rect,
                 "canvas_size_px": size, "frame_line_width_px": line, "points": points,
                 "validated_at": None, "validated_native_frame_folder_flag": None,
                 "notes": [f"Recorded {now()} by `profile record`; not valid until `profile verify` passes."],
                 "split": {"workflow": "template_split", "verified": False,
                           "basic_frame_mm": config["canvas"]["basic_frame_mm"], "reference": relative(reference),
                           "guard_regions": guards, "geometry_tolerance_screen_px": 2.5, "layer_row_height": height,
                           "gap_unit": "integer_canvas_pixels", "folder_order": "upper before lower; right before left"}}
        write_json(profile_path(config), calib)
        config["mapping"]["frame_line_width_px"] = line
        write_json(work_directory(config) / "job.json", {k: v for k, v in config.items() if not k.startswith("_")})
        desktop.calibration = calib
        # The scratch page now holds an unsaved frame; save and close it so no dialog is left behind.
        desktop.save(scratch)
        desktop.press("CTRL", "W")
    finally:
        teacher.close()
    return profile_path(config)


def synthetic_plan(config, target):
    """Four panels in a 2x2 grid: exercises both gap fields and three native splits."""
    from .split_frames import axis_mapping, subdivision
    gutter = 40
    layout = {"canvas": config["canvas"], "mapping": {**config["mapping"], "target_region": "basic_frame"},
              "source": {"size_px": [1000, 1400]},
              "panels": [{"id": f"p{i + 1:03d}", "outer_px": box, "reading_order": i + 1}
                         for i, box in enumerate([[0, 0, 480, 680], [520, 0, 1000, 680],
                                                  [0, 720, 480, 1400], [520, 720, 1000, 1400]])]}
    split = subdivision(layout)
    page = "profile"
    plan = {"page_id": page, "target_clip": relative(target), "canvas": config["canvas"], "frame_workflow": "template_split",
            "transform": axis_mapping(layout), "subdivision": split, "operations": []}
    assets_dir = work_directory(config) / "profile/assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    size = tuple(config["canvas"]["pixel_size"])
    dpi = config["canvas"]["dpi"]
    context = assets_dir / f"AC_{page}_outside.png"
    Image.new("RGBA", size, (0, 0, 0, 0)).save(context, dpi=(dpi, dpi))
    assets = {"assets": [{"path": relative(context)}]}
    overscan = config["mapping"]["image_overscan_px"]
    for panel, color in zip(layout["panels"], ["#ef6755", "#47aaf0", "#63c884", "#f2c64d"]):
        ident = panel["id"]
        leaf = split["leaves"][ident]
        rect = leaf["rect_px"]
        op = {"panel_id": ident, "palette_index": leaf["palette_index"], "folder_name": f"AC_{page}_{ident}",
              "image_layer_name": f"AC_{page}_{ident}_image", "frame_path_rect_px": rect}
        plan["operations"].append(op)
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle([round(v + (-overscan if i < 2 else overscan)) for i, v in enumerate(rect)], fill=color)
        path = assets_dir / (op["image_layer_name"] + ".png")
        image.save(path, dpi=(dpi, dpi))
        assets["assets"].append({"panel_id": ident, "path": relative(path)})
    return plan, assets


def measured_line_width(screenshot, calib, basic_left_mm, dpi):
    """Dark run length across the basic frame's left edge at mid height, in screen pixels."""
    x1, y1, x2, y2 = calib["canvas_screen_rect"]
    pixels = np.asarray(screenshot.convert("RGB")).astype(int)
    scale = (x2 - x1) / calib["canvas_size_px"][0]
    edge = x1 + basic_left_mm * dpi / 25.4 * scale
    row_y = (y1 + y2) // 2
    line = pixels[row_y, max(0, int(edge - 12)):int(edge + 12)].max(axis=1) < 110
    runs = [len(r) for r in "".join("1" if v else "0" for v in line).split("0") if r]
    return (max(runs) if runs else 0), scale


def verify(config):
    from .gui import Desktop, verify_saved
    from .split_gui import create_base_frame, populate, row
    from .stages import focus_clip
    path = profile_path(config)
    calib = read_json(path)
    for key in ("new_frame_width", "new_frame_ok", "top_folder_name", "top_folder_toggle", "frame_tool", "cut_group",
                "divide_folder", "gap_vertical", "gap_horizontal", "selection_tool", "import_image_menu", "hover_away"):
        if key not in calib["points"]:
            raise ValueError(f"Profile lacks point {key}; run `profile record`")
    config = {**config, "mapping": {**config["mapping"], "frame_line_width_px": calib["frame_line_width_px"]}}
    target = scratch_page(config, "verify.clip")
    plan, assets = synthetic_plan(config, target)
    focus_clip(1.0)
    desktop = Desktop(calib)
    desktop.guard()
    work = work_directory(config) / "profile"
    state_path = work / "verify-state.json"
    state = {"purpose": "synthetic GUI profile verification; no manga content", "panels": []}
    write_json(state_path, state)
    desktop.open(target)
    if "*" in desktop.title(desktop.hwnd):
        raise ValueError("Scratch page unexpectedly modified")
    desktop.assert_blank_canvas()
    create_base_frame(desktop, calib)
    desktop.click(row(calib, 1)); desktop.click(calib["points"]["selection_tool"])
    shot = desktop.capture(work / "evidence/base-frame.png")
    width, scale = measured_line_width(shot, calib, config["canvas"]["basic_frame_mm"][0], config["canvas"]["dpi"])
    expected = calib["frame_line_width_px"] * scale
    if abs(width - expected) > max(1.5, expected * 0.35):
        raise ValueError(f"Frame line is {width}px wide on screen, expected about {expected:.1f}px. "
                         "Set CLIP STUDIO's length unit to px (Preferences > Ruler/Unit) and record again.")
    populate(desktop, plan, assets, work / "evidence", state, state_path)
    desktop.click(row(calib, len(plan["operations"]))); desktop.click(calib["points"]["selection_tool"])
    desktop.save(target)
    verify_saved(plan, 17)
    desktop.capture(work / "evidence/saved.png")
    desktop.press("CTRL", "W")
    calib["split"]["verified"] = True
    calib["validated_native_frame_folder_flag"] = 17
    calib["validated_at"] = now()
    calib["notes"] = [f"Verified {calib['validated_at']}: synthetic 2x2 split saved and layer structure checked."]
    write_json(path, calib)
    return path


def show(config):
    path = profile_path(config)
    if not path.is_file():
        print(f"profile: none ({relative(path)})")
        return
    calib = read_json(path)
    env = calib["environment"]
    print(f"profile {relative(path)}: screen {env['screen_size']} dpi {env['dpi']} window {env['window_rect']}; "
          f"paper {calib['canvas_size_px']}; verified={calib['split']['verified']} at {calib.get('validated_at')}")


def main(config, action, line_px=None):
    from .completion import run_calibration
    if action == "show":
        return show(config)
    if action == 'auto':
        raise ValueError('Computer Use is the default calibration workflow. Confirm a local desktop connection, '
                         'then use `profile computer --step prepare` and --step finish. See docs/PROFILE.html.')
    from .stages import stage_init
    stage_init(config)
    if action == "record":
        def both():
            record(config, line_px)
            return verify(config)
        print(run_calibration(config, both, "profile"))
    else:
        print(run_calibration(config, lambda: verify(config), "profile"))
