"""Measured Japanese CSP basic-frame creation and native folder subdivision."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .common import now, read_json, relative, resolve, sha256, work_directory, write_json
from .project import inspect_project
from .split_frames import basic_frame, seed_pages, verify_base, verify_template


def screen_rect(calib, rect):
    x1,y1,x2,y2 = calib["canvas_screen_rect"]
    w,h = calib["canvas_size_px"]
    return [v*((x2-x1)/w if i%2 == 0 else (y2-y1)/h)+(x1 if i%2 == 0 else y1)
            for i,v in enumerate(rect)]


def measure_frames(screenshot, calib, rectangles):
    """Check the rendered, empty native frames before any artwork can hide errors."""
    image = np.asarray(screenshot.convert("RGB"))
    x1,y1,x2,y2 = calib["canvas_screen_rect"]
    area = image[y1+2:y2-2,x1+2:x2-2].astype(np.int16)
    mask = ((area.max(axis=2) < 180) & ((area.max(axis=2)-area.min(axis=2)) < 30)).astype(np.uint8)*255
    contours,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for contour in contours:
        x,y,w,h = cv2.boundingRect(contour)
        if w > 15 and h > 15:
            if cv2.contourArea(contour)/(w*h) < .90:
                raise ValueError("Rendered frame is open, skewed, or contains unexpected content")
            found.append([x+x1+2,y+y1+2,x+x1+2+w,y+y1+2+h])
    if len(found) != len(rectangles):
        raise ValueError(f"Native frame count differs: rendered {len(found)}, expected {len(rectangles)}")
    measured = []
    remaining = found.copy()
    tolerance = calib["split"]["geometry_tolerance_screen_px"]
    if not 0 < tolerance <= 2.5:
        raise ValueError("Unsafe frame geometry tolerance")
    for rect in rectangles:
        expected = screen_rect(calib, rect)
        best = min(remaining,key=lambda b: max(abs(a-v) for a,v in zip(b,expected)))
        error = max(abs(a-v) for a,v in zip(best,expected))
        if error > tolerance:
            raise ValueError(f"Rendered native frame differs by {error:.2f} screen pixels; expected {expected}, found {best}")
        remaining.remove(best)
        measured.append({"expected_screen_rect": expected, "found_screen_rect": best, "max_error": error})
    return measured


def split_tool(desktop):
    calib = desktop.calibration
    for key in ("frame_tool", "cut_group", "divide_folder"):
        desktop.click(calib["points"][key])
    desktop.u.SetCursorPos(*calib["points"]["hover_away"])
    time.sleep(.3) # Let the subtool hover highlight disappear before comparison.
    actual = np.asarray(desktop.capture().convert("RGB")).astype(float)
    reference = np.asarray(Image.open(resolve(calib["split"]["reference"])).convert("RGB")).astype(float)
    for x1,y1,x2,y2 in calib["split"]["guard_regions"]:
        error = np.abs(actual[y1:y2,x1:x2]-reference[y1:y2,x1:x2]).mean()
        if error > 3:
            raise ValueError(f"Native split tool settings changed ({error:.1f}); recalibrate")


def row(calib, index, name=True):
    x,y = calib["points"]["top_folder_name" if name else "top_folder_toggle"]
    return [x,y+calib["split"]["layer_row_height"]*index]


def populate(desktop, plan, assets, evidence, state, state_path):
    calib = desktop.calibration
    # Template has one empty folder and the original root raster beneath it.
    desktop.click(row(calib,1)); desktop.click(calib["points"]["selection_tool"])
    desktop.assert_blank_canvas()
    base_measurement = measure_frames(desktop.capture(),calib,[plan["transform"]["target_rect_px"]])
    state.update(stage="splitting", base_geometry=base_measurement, cuts=[])
    write_json(state_path,state)
    for index,cut in enumerate(plan["subdivision"]["cuts"]):
        desktop.click(row(calib,cut["palette_index"]))
        split_tool(desktop)
        point = calib["points"]["gap_vertical" if cut["axis"] == "y" else "gap_horizontal"]
        desktop.click(point,double=True); desktop.press("CTRL","A")
        desktop.text(str(cut["tool_gap_px"])); desktop.press("ENTER")
        x1,y1,x2,y2 = screen_rect(calib,cut["rect_px"])
        if cut["axis"] == "y":
            pos = screen_rect(calib,[0,cut["position_px"],0,cut["position_px"]])[1]
            drag = [round(x1+8),round(pos),round(x2-8),round(pos)]
            length = x2-x1
        else:
            pos = screen_rect(calib,[cut["position_px"],0,cut["position_px"],0])[0]
            drag = [round(pos),round(y1+8),round(pos),round(y2-8)]
            length = y2-y1
        if length < 40:
            raise ValueError("Frame is too small for the calibrated subdivision drag")
        desktop.drag(drag)
        state["cuts"].append({"node":cut["node"],"drag":drag,"gap_px":cut["tool_gap_px"]})
        write_json(state_path,state)
    count = len(plan["operations"])
    desktop.click(row(calib,count)); desktop.click(calib["points"]["selection_tool"])
    screenshot = desktop.capture(evidence/"empty-divided-frames.png")
    measured = measure_frames(screenshot,calib,[o["frame_path_rect_px"] for o in plan["operations"]])
    state.update(stage="importing", divided_geometry=measured); write_json(state_path,state)
    # Root raster is selected, so context is a root sibling below all frames.
    desktop.import_image(resolve(assets["assets"][0]["path"]))
    by_id = {a["panel_id"]:a for a in assets["assets"][1:]}
    for op in sorted(plan["operations"],key=lambda o:o["palette_index"]):
        index = op["palette_index"]
        desktop.click(row(calib,index),double=True)
        desktop.press("CTRL","A"); desktop.text(op["folder_name"]); desktop.press("ENTER")
        # Importing while the empty frame is selected creates its image child.
        desktop.import_image(resolve(by_id[op["panel_id"]]["path"]))
        desktop.click(row(calib,index,name=False))
        desktop.capture(evidence/(op["panel_id"]+".png"))
        state["panels"].append({"panel_id":op["panel_id"],"state":"created_not_saved"})
        write_json(state_path,state)
        print(plan["page_id"],op["panel_id"],"filled native subdivision",flush=True)


def validate(plan, calib):
    split = calib.get("split",{})
    if not split.get("verified") or split.get("workflow") != "template_split":
        raise ValueError("Native subdivision calibration has not been verified")
    if plan["transform"]["fit"] != "independent_axes" or plan["transform"]["rotation_degrees"]:
        raise ValueError("Split placement must use independent axes with no rotation")
    if plan["canvas"]["basic_frame_mm"] != split["basic_frame_mm"]:
        raise ValueError("Basic frame differs from subdivision calibration")
    x1,y1,x2,y2 = calib["canvas_screen_rect"]
    w,h = calib["canvas_size_px"]
    # Cover the maximum accepted GUI error plus half the native stroke.
    minimum = split["geometry_tolerance_screen_px"]*max(w/(x2-x1),h/(y2-y1))
    for op in plan["operations"]:
        r,p = op["frame_path_rect_px"],op["placement_rect_px"]
        if min(r[0]-p[0],r[1]-p[1],p[2]-r[2],p[3]-r[3]) < minimum:
            raise ValueError("Image overscan is too small for the calibrated GUI tolerance")


def record_template(config, calib, evidence, measured):
    work = work_directory(config)
    first = resolve(inspect_project(resolve(config["project"]))["pages"][0]["path"])
    verify_base(first)
    if basic_frame(first)["rect_mm"] != config["canvas"]["basic_frame_mm"]:
        raise ValueError("Native basic frame differs from configuration")
    base = work/"template/base.clip"
    record = work/"template/template.json"
    if record.exists():
        return verify_template(config)
    if base.exists() and sha256(base) != sha256(first):
        raise ValueError("Existing template differs; refusing to overwrite")
    base.parent.mkdir(parents=True,exist_ok=True)
    if not base.exists():
        shutil.copy2(first,base)
    data = {"path":relative(base),"sha256":sha256(base),"basic_frame":basic_frame(base),
            "environment":calib["environment"],"evidence":relative(evidence),"evidence_sha256":sha256(evidence),
            "screen_geometry_verified":True,"measurement":measured,"at":now(),
            "method":"CLIP STUDIO: Layer > New Layer > Frame Border Folder; GUI save"}
    write_json(record,data)
    return data


def calibrate(config, calibration):
    from .completion import run_calibration
    return run_calibration(config,lambda: _calibrate(config,calibration))


def create_base_frame(desktop, calib, name="AC_base_frame"):
    """Menu command creates the path exactly at the document's native basic frame."""
    desktop.press("ALT","L"); desktop.press("E"); desktop.press("C")
    if "コマ枠" not in desktop.title():
        raise ValueError("Expected the native frame-border-folder dialog")
    desktop.press("CTRL","A",modal=True); desktop.text(name,modal=True)
    desktop.click(calib["points"]["new_frame_width"],modal=True)
    desktop.press("CTRL","A",modal=True); desktop.text(str(calib["frame_line_width_px"]),modal=True)
    desktop.click(calib["points"]["new_frame_ok"],modal=True)
    desktop.wait_main()


def _calibrate(config, calibration):
    from .gui import Desktop
    work = work_directory(config)
    if (work/"template/template.json").exists():
        verify_template(config)
        return seed_pages(config)
    if list((work/"state").glob("*.json")) or list(resolve(config["output_dir"]).glob("*.operations*.json")):
        raise ValueError("Operations/state exist; use a new work directory for calibration")
    pages = inspect_project(resolve(config["project"]))["pages"]
    first = resolve(pages[0]["path"])
    for page in pages:
        if any(r.get("ComicFrameLineMipmap") or r["LayerName"].startswith("AC_") for r in page["clip"]["layers"]):
            raise ValueError("Calibration requires an untouched project copy")
        if basic_frame(resolve(page["path"]))["rect_mm"] != config["canvas"]["basic_frame_mm"]:
            raise ValueError("Page basic frame differs from configuration")
    calib = read_json(resolve(calibration))
    desktop = Desktop(calib); desktop.guard()
    desktop.open(first)
    if "*" in desktop.title(desktop.hwnd):
        raise ValueError("Target has unsaved changes")
    desktop.assert_blank_canvas()
    create_base_frame(desktop,calib)
    desktop.click(row(calib,1)); desktop.click(calib["points"]["selection_tool"])
    expected = [v*config["canvas"]["dpi"]/25.4 for v in config["canvas"]["basic_frame_mm"]]
    evidence = work/"evidence/calibration/basic-frame.png"
    measured = measure_frames(desktop.capture(evidence),calib,[expected])
    desktop.save(first); verify_base(first)
    desktop.press("CTRL","W") # Only the document opened and saved by this command.
    record_template(config,calib,evidence,measured)
    return seed_pages(config)
