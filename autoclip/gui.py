"""Calibrated Windows-only CLIP STUDIO adapter; all edits use its public GUI."""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import shutil
import time

import cv2
import numpy as np
from PIL import Image, ImageGrab

from .common import ROOT, audit, now, read_json, relative, resolve, sha256, work_directory, write_json
from .operations import calibrated_placement, prepare_assets, verify_operations
from .project import database, inspect_clip


class Desktop:
    def __init__(self, calibration=None):
        if os.name != "nt":
            raise ValueError("Windows desktop required")
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.u.SetProcessDPIAware()
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.u.IsWindowVisible.argtypes = [W.HWND]
        self.u.IsWindowEnabled.argtypes = [W.HWND]
        self.u.GetDpiForWindow.argtypes = [W.HWND]
        self.u.SetForegroundWindow.argtypes = [W.HWND]
        self.u.OpenClipboard.argtypes = [W.HWND]
        self.u.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
        self.u.SetClipboardData.restype = W.HANDLE
        self.k.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
        self.k.GlobalAlloc.restype = W.HANDLE
        self.k.GlobalLock.argtypes = [W.HANDLE]
        self.k.GlobalLock.restype = C.c_void_p
        self.k.GlobalUnlock.argtypes = [W.HANDLE]
        windows = []
        callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def collect(hwnd, _):
            if self.u.IsWindowVisible(hwnd) and "CLIP STUDIO PAINT" in self.title(hwnd):
                r = self.rect(hwnd)
                if r[2]-r[0] > 1000 and r[3]-r[1] > 700:
                    windows.append(hwnd)
            return True
        self.u.EnumWindows(callback(collect), 0)
        if len(windows) != 1:
            raise ValueError("Expected one visible CLIP STUDIO PAINT window")
        self.hwnd = windows[0]
        self.pid = self.process(self.hwnd)
        self.calibration = calibration

    def process(self, hwnd):
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return pid.value

    def title(self, hwnd=None):
        buffer = C.create_unicode_buffer(2048)
        self.u.GetWindowTextW(hwnd or self.u.GetForegroundWindow(), buffer, len(buffer))
        return buffer.value

    def rect(self, hwnd):
        r = W.RECT()
        self.u.GetWindowRect(hwnd,C.byref(r))
        return [r.left,r.top,r.right,r.bottom]

    def environment(self):
        return {"screen_size": [self.u.GetSystemMetrics(0),self.u.GetSystemMetrics(1)],
                "window_rect":self.rect(self.hwnd),"dpi":self.u.GetDpiForWindow(self.hwnd)}

    def guard(self, modal=False):
        if self.process(self.u.GetForegroundWindow()) != self.pid:
            raise ValueError("CLIP STUDIO lost foreground; input stopped")
        if not modal and not self.u.IsWindowEnabled(self.hwnd):
            raise ValueError("Unexpected modal dialog; input stopped")
        if self.calibration and self.environment() != self.calibration["environment"]:
            raise ValueError("Screen/window/DPI changed; recalibrate. メイン画面1920x1080・拡大縮小100%・クリスタ最大化を確認してください。手順: docs/GUI.html")

    def press(self, *keys, modal=False):
        self.guard(modal)
        codes = {"CTRL":17,"ALT":18,"SHIFT":16,"ENTER":13,"ESC":27,"TAB":9,
                 "HOME":36,"LEFT":37,"UP":38,"RIGHT":39,"DOWN":40}
        values = [codes[k] if k in codes else ord(k.upper()) for k in keys]
        for key in values:
            self.u.keybd_event(key,0,0,0)
        time.sleep(.035)
        for key in reversed(values):
            self.u.keybd_event(key,0,2,0)
        time.sleep(.18)

    def text(self, value, modal=False):
        self.guard(modal)
        payload = (value+"\0").encode("utf-16le")
        memory = self.k.GlobalAlloc(2,len(payload))
        pointer = self.k.GlobalLock(memory)
        if not pointer:
            raise OSError("Clipboard allocation failed")
        C.memmove(pointer,payload,len(payload))
        self.k.GlobalUnlock(memory)
        for _ in range(20):
            if self.u.OpenClipboard(self.hwnd):
                break
            time.sleep(.05)
        else:
            raise OSError("Clipboard busy")
        try:
            self.u.EmptyClipboard()
            if not self.u.SetClipboardData(13,memory):
                raise OSError("Clipboard write failed")
        finally:
            self.u.CloseClipboard()
        self.press("CTRL","V",modal=modal)

    def click(self, point, double=False, modal=False):
        self.guard(modal)
        self.u.SetCursorPos(*point)
        for _ in range(2 if double else 1):
            self.u.mouse_event(2,0,0,0,0); self.u.mouse_event(4,0,0,0,0)
            time.sleep(.075)
        time.sleep(.15)

    def drag(self, rect):
        self.guard()
        x1,y1,x2,y2 = rect
        self.u.SetCursorPos(x1,y1); self.u.mouse_event(2,0,0,0,0)
        try:
            for t in np.linspace(0,1,25):
                self.guard()
                self.u.SetCursorPos(round(x1+(x2-x1)*t),round(y1+(y2-y1)*t))
                time.sleep(.012)
        finally:
            self.u.mouse_event(4,0,0,0,0)
        time.sleep(.25)

    def capture(self, path=None):
        image = ImageGrab.grab()
        if path:
            Path(path).parent.mkdir(parents=True,exist_ok=True)
            image.save(path)
        return image

    def wait_main(self, timeout=15):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.guard(modal=True)
            if self.u.IsWindowEnabled(self.hwnd) and self.u.GetForegroundWindow() == self.hwnd:
                time.sleep(.6)
                return
            time.sleep(.15)
        raise ValueError("CLIP STUDIO did not return to its canvas")

    def file_picker(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(ROOT/"work") or not path.is_file():
            raise ValueError("GUI file input must be an existing work/ copy")
        deadline=time.monotonic()+5
        while "開く" not in self.title() and time.monotonic()<deadline:
            time.sleep(.1)
        if "開く" not in self.title():
            raise ValueError("Expected Open file picker")
        self.press("ALT","N",modal=True)
        self.press("CTRL","A",modal=True)
        self.text(str(path),modal=True)
        self.press("ENTER",modal=True)
        self.wait_main()

    def open(self, path):
        self.press("CTRL","O"); self.file_picker(path)

    def import_image(self, path):
        self.press("ALT","F"); self.press("I")
        # Access key I is duplicated; a calibrated click selects Image, not File Object.
        self.click(self.calibration["points"]["import_image_menu"])
        self.file_picker(path)

    def frame_tool(self):
        self.click(self.calibration["points"]["frame_tool"])
        image = np.array(self.capture())
        reference = np.array(Image.open(resolve(self.calibration["reference"])).convert("RGB"))
        for rect in self.calibration["guard_regions"]:
            x1,y1,x2,y2 = rect
            error=np.abs(image[y1:y2,x1:x2].astype(float)-reference[y1:y2,x1:x2].astype(float)).mean()
            if error > 3:
                raise ValueError(f"Workspace/tool settings differ (region {rect}, error {error:.1f})")
        self.click([197,710]); self.click([1229,700])
        snap=np.array(self.capture())
        snap_reference=np.array(Image.open(resolve(self.calibration["snap_reference"])).convert("RGB"))
        rect=self.calibration["snap_guard_region"]
        x1,y1,x2,y2=rect
        error=np.abs(snap[y1:y2,x1:x2].astype(float)-snap_reference[y1:y2,x1:x2].astype(float)).mean()
        self.click([1542,309])
        if error>3:
            raise ValueError("Snap configuration differs; frame placement would be unsafe")

    def screen_frame(self, op, canvas_size):
        x1,y1,x2,y2 = self.calibration["canvas_screen_rect"]
        sx,sy=(x2-x1)/canvas_size[0],(y2-y1)/canvas_size[1]
        center = [(a+b)/2 for a,b in zip(op["outer_rect_px"],op["frame_inner_rect_px"])]
        result = [round(v*(sx if i%2==0 else sy)+(x1 if i%2==0 else y1)) for i,v in enumerate(center)]
        if not (x1 < result[0] < result[2] < x2 and y1 < result[1] < result[3] < y2):
            raise ValueError("Frame lies outside calibrated canvas")
        return result

    def panel(self, op, asset, canvas_size, evidence_dir):
        self.frame_tool()
        if abs(op["frame_line_width_px"]-self.calibration["frame_line_width_px"])>.05:
            raise ValueError("Frame width differs from calibrated tool")
        rect=self.screen_frame(op,canvas_size)
        self.drag(rect)
        points=self.calibration["points"]
        self.click(points["top_folder_name"],double=True)
        self.press("CTRL","A"); self.text(op["folder_name"]); self.press("ENTER")
        self.click(points["top_folder_child"])
        self.import_image(resolve(asset["path"]))
        self.click(points["top_folder_toggle"])
        self.capture(Path(evidence_dir)/(op["panel_id"]+".png"))
        audit("gui_panel",panel=op["panel_id"],screen_rect=rect)
        return {"panel_id":op["panel_id"],"screen_rect":rect,"state":"created_not_saved"}

    def assert_blank_canvas(self):
        screenshot=np.array(self.capture())
        x1,y1,x2,y2=self.calibration["canvas_screen_rect"]
        # Blank test pages have blue trim marks but virtually no dark content.
        inside=screenshot[y1+4:y2-4,x1+4:x2-4]
        if np.mean(np.min(inside,axis=2)>200)<.98:
            raise ValueError("Canvas is not blank or viewport changed")
        # Blue trim/cursor guides can extend beyond the white sheet at its midpoint.
        # Find the connected paper rectangle instead of testing four single pixels.
        left,top=max(0,x1-25),max(0,y1-9)
        area=screenshot[top:y2+9,left:x2+25]
        mask=(np.max(area,axis=2)>=100).astype(np.uint8)
        _,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
        largest=stats[1:][np.argmax(stats[1:,cv2.CC_STAT_AREA])]
        found=[left+int(largest[0]),top+int(largest[1]),left+int(largest[0]+largest[2]),top+int(largest[1]+largest[3])]
        if max(abs(a-b) for a,b in zip(found,(x1,y1,x2,y2)))>1:
            raise ValueError(f"Canvas boundary or zoom differs: {found}; recalibrate")

    def save(self, target):
        before=sha256(target)
        self.press("CTRL","S")
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            self.guard(modal=True)
            if self.u.IsWindowEnabled(self.hwnd):
                try:
                    value=inspect_clip(target)
                    time.sleep(1)
                    if value["file_sha256"]!=before and value["file_sha256"]==sha256(target):
                        return value
                except (OSError,ValueError):
                    pass
            time.sleep(.25)
        raise ValueError("Save did not complete within 30 seconds")


def verify_saved(plan, expected_frame_type=17):
    info=inspect_clip(resolve(plan["target_clip"]))
    layers=info["layers"]
    names=[r["LayerName"] for r in layers]
    by_id={r["MainId"]:r for r in layers}
    roots=[r for r in layers if r["LayerName"]=="" and r["LayerFolder"]]
    if len(roots)!=1:
        raise ValueError("Unexpected layer root")
    root_children=[]; index=roots[0]["LayerFirstChildIndex"]
    while index:
        if index in root_children:
            raise ValueError("Layer sibling cycle")
        root_children.append(index); index=by_id[index]["LayerNextIndex"]
    for op in plan["operations"]:
        if names.count(op["folder_name"])!=1 or names.count(op["image_layer_name"])!=1:
            raise ValueError("Missing or duplicate imported layer")
        folder=next(r for r in layers if r["LayerName"]==op["folder_name"])
        if not folder["LayerFolder"] or folder["MainId"] not in root_children:
            raise ValueError("Panel folder is not an independent root child")
        if expected_frame_type is not None and (folder["LayerFolder"]!=expected_frame_type or not folder.get("ComicFrameLineMipmap") or folder.get("VectorNormalType")!=3):
            raise ValueError("Saved layer type is not the GUI-verified native frame type")
        children=[]; index=folder["LayerFirstChildIndex"]
        while index:
            if index in children:
                raise ValueError("Layer child cycle")
            children.append(index); index=by_id[index]["LayerNextIndex"]
        image=next(r for r in layers if r["LayerName"]==op["image_layer_name"])
        if image["MainId"] not in children:
            raise ValueError("Panel image is not inside its frame folder")
    if names.count(f'AC_{plan["page_id"]}_outside')!=1:
        raise ValueError("Outside-panel content layer is missing")
    if plan.get("frame_workflow") == "template_split":
        from .split_frames import native_frames
        if len(native_frames(info)) != len(plan["operations"]):
            raise ValueError("Unexpected leftover or missing native subdivided frame")
    return info


def checked_plan(operations_path):
    initial=read_json(operations_path)
    state_path=work_directory(initial)/"state"/(initial["page_id"]+".json")
    if state_path.exists():
        old=read_json(state_path)
        if old["state"]=="complete" and old["plan_sha256"]==initial["plan_sha256"]:
            if old["saved_sha256"]!=sha256(resolve(initial["target_clip"])):
                raise ValueError("Previously completed page changed; inspect before rerun")
            verify_operations(operations_path,target_sha256=old["saved_sha256"])
            verify_saved(initial)
            return initial, True
        raise ValueError("Incomplete/changed run exists; use the documented recovery process before rerunning")
    return verify_operations(operations_path), False


def validate_calibration(plan, calib):
    """Reject an incompatible recipe before assets, state, or desktop edits."""
    if not 1 <= len(plan["operations"]) <= 10:
        raise ValueError("This layer-palette calibration supports 1..10 panels per page")
    if calib.get("canvas_size_px") != plan["canvas"]["pixel_size"]:
        raise ValueError("Canvas size differs from GUI calibration; recalibrate for this project")
    if not calib.get("validated_native_frame_folder_flag"):
        raise ValueError("Native frame calibration has not been verified")
    if any(abs(op["frame_line_width_px"]-calib["frame_line_width_px"]) > .05 for op in plan["operations"]):
        raise ValueError("Frame width differs from calibrated tool; recalibrate for this project")
    if plan.get("frame_workflow") == "template_split":
        from .split_gui import validate
        validate(plan,calib)
        return
    if plan["transform"]["rotation_degrees"] != 0 or plan["transform"]["fit"] != "contain":
        raise ValueError("GUI recipe is validated for unrotated contain placement only")


def check(operations_path, calibration_path):
    plan, complete = checked_plan(operations_path)
    if not complete:
        calib = read_json(resolve(calibration_path))
        validate_calibration(calibrated_placement(plan, calib), calib)
    print(plan["page_id"], "already complete; verified" if complete else "files/calibration verified; desktop not checked")


def run(operations_path, calibration_path):
    plan, complete = checked_plan(operations_path)
    if complete:
        print(plan["page_id"],"already complete; skipped")
        return
    work = work_directory(plan)
    state_path = work / "state" / (plan["page_id"] + ".json")
    calib=read_json(resolve(calibration_path))
    plan=calibrated_placement(plan, calib)
    validate_calibration(plan, calib)
    assets=read_json(prepare_assets(operations_path, calib))
    for asset in assets["assets"]:
        if sha256(resolve(asset["path"]))!=asset["sha256"]:
            raise ValueError("Prepared asset changed")
    desktop=Desktop(calib); desktop.guard()
    target=resolve(plan["target_clip"])
    if not target.is_relative_to(ROOT/"work"):
        raise ValueError("GUI runner only edits work copies")
    backup=work/"backups/gui"/plan["page_id"]/plan["target_clip_before_sha256"]
    backup.mkdir(parents=True,exist_ok=True)
    for file in (target,resolve(plan["project"])):
        dest=backup/file.name
        if not dest.exists():
            shutil.copy2(file,dest)
        if sha256(file)!=sha256(dest):
            raise ValueError("Backup verification failed")
    state={"state":"started","plan_sha256":plan["plan_sha256"],"backup":relative(backup),
           "page_id":plan["page_id"],"panels":[],"started_at":now(),"stage":"preflight"}
    state["placement_rects_px"] = assets["placement_rects_px"]
    write_json(state_path,state)
    evidence=work/"evidence/import"/plan["page_id"]
    try:
        desktop.open(target)
        if "*" in desktop.title(desktop.hwnd):
            raise ValueError("Target has unsaved changes")
        if plan.get("frame_workflow") == "template_split":
            from .split_gui import populate
            populate(desktop,plan,assets,evidence,state,state_path)
        else:
            desktop.assert_blank_canvas()
            state["stage"]="importing"
            write_json(state_path,state)
            desktop.import_image(resolve(assets["assets"][0]["path"]))
            for op,asset in zip(plan["operations"],assets["assets"][1:]):
                state["panels"].append(desktop.panel(op,asset,plan["canvas"]["pixel_size"],evidence))
                write_json(state_path,state)
                print(plan["page_id"],op["panel_id"],"imported",flush=True)
        # Select the outside layer after collapsing all panel folders: avoids selection overlays.
        if plan.get("frame_workflow") == "template_split":
            from .split_gui import row
            desktop.click(row(calib,len(plan["operations"])))
            desktop.click(calib["points"]["selection_tool"])
        else:
            desktop.click([1730,450+40*len(plan["operations"])])
            desktop.click([20,205])
        desktop.save(target)
        info=verify_saved(plan,calib["validated_native_frame_folder_flag"])
        desktop.capture(evidence/"saved.png")
        state.update(state="complete",saved_sha256=info["file_sha256"],completed_at=now())
        write_json(state_path,state)
        audit("gui_complete",page=plan["page_id"],sha256=info["file_sha256"])
        print(plan["page_id"],"saved and layer hierarchy verified",flush=True)
    except Exception as exc:
        state.update(state="interrupted",error=str(exc),interrupted_at=now())
        write_json(state_path,state)
        desktop.capture(evidence/"interrupted.png")
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["status","check","run"])
    parser.add_argument("operations",type=Path,nargs="?")
    parser.add_argument("--calibration",help="GUI profile JSON (profile/<paper>.json)")
    args=parser.parse_args()
    if args.command=="status":
        d=Desktop(); print(d.environment()); print(d.title(d.hwnd))
    elif args.operations:
        (check if args.command == "check" else run)(args.operations,args.calibration)
    else:
        parser.error("operations JSON is required")


if __name__=="__main__":
    main()
