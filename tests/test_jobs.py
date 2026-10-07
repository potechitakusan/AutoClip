"""Job workflow tests on synthetic pages in a throw-away workspace (AUTOCLIP_ROOT)."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import synthetic  # noqa: E402

from autoclip.common import CODE_ROOT, IMAGE_EXTENSIONS, list_sources, natural_key  # noqa: E402
from autoclip.models import inspect_model  # noqa: E402
from autoclip.operations import size_for_edge, target_long_edge  # noqa: E402


def cli(workspace, *args, expect=0):
    env = {**os.environ, "AUTOCLIP_ROOT": str(workspace), "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-m", "autoclip", *args], cwd=str(CODE_ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == expect, f"{args}: rc {done.returncode}\n{done.stdout}\n{done.stderr}"
    return done.stdout + done.stderr


def safetensors(path, shapes):
    header = {k: {"dtype": "F32", "shape": s, "data_offsets": [0, 0]} for k, s in shapes.items()}
    raw = json.dumps(header).encode()
    Path(path).write_bytes(struct.pack("<Q", len(raw)) + raw)


def rrdb_shapes(blocks=23):
    shapes = {"conv_first.weight": [64, 3, 3, 3], "conv_body.weight": [64, 64, 3, 3], "conv_up1.weight": [64, 64, 3, 3],
              "conv_up2.weight": [64, 64, 3, 3], "conv_hr.weight": [64, 64, 3, 3], "conv_last.weight": [3, 64, 3, 3]}
    for i in range(blocks):
        shapes[f"body.{i}.rdb1.conv1.weight"] = [32, 64, 3, 3]
    return shapes


class Workspace(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="autoclip-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def make(self, pages, cmc_pages=None, size=(600, 840), jpeg_every=0):
        synthetic.make_pages(self.root / "input/book", pages, size, jpeg_every)
        return synthetic.make_project(self.root / "input/book/project", cmc_pages or pages)

    def verified_profile(self):
        folder = self.root / "profile"
        folder.mkdir(exist_ok=True)
        (folder / "2508x3541.json").write_text(json.dumps({"split": {"verified": True}}), encoding="utf-8")


class SourceTests(Workspace):
    def test_png_jpg_natural_order(self):
        folder = self.root / "pages"
        for name in ("page-10.png", "page-2.jpg", "page-1.jpeg", "notes.txt", "page-3.PNG"):
            (folder / name).parent.mkdir(exist_ok=True)
            (folder / name).write_bytes(b"x")
        names = [p.name for p in list_sources({"source_dir": str(folder)})]
        self.assertEqual(names, ["page-1.jpeg", "page-2.jpg", "page-3.PNG", "page-10.png"])
        self.assertEqual(sorted(["b10", "b9", "b1"], key=natural_key), ["b1", "b9", "b10"])
        self.assertEqual(set(IMAGE_EXTENSIONS), {".png", ".jpg", ".jpeg"})

    def test_model_inspection_reads_only_the_header(self):
        good, bad, pickle = self.root / "a.safetensors", self.root / "b.safetensors", self.root / "c.pth"
        safetensors(good, rrdb_shapes(6))
        self.assertEqual(inspect_model(good)["num_block"], 6)
        safetensors(bad, {**rrdb_shapes(), "conv_first.weight": [64, 12, 3, 3]})
        self.assertFalse(inspect_model(bad)["compatible"])
        pickle.write_bytes(b"not read")
        self.assertIn("pickle", inspect_model(pickle)["reason"])

    def test_upscale_policy(self):
        auto = {"mode": "builtin", "long_edge": "auto", "skip_below_scale": 1.25}
        self.assertIsNone(target_long_edge((1000, 800), 1.1, auto))      # tiny enlargement: no model
        self.assertEqual(target_long_edge((1000, 800), 2.0, auto), 2000)  # only as much as needed
        self.assertEqual(target_long_edge((1000, 800), 9.0, auto), 4000)  # never beyond the x4 model
        self.assertEqual(target_long_edge((1000, 800), 9.0, {"mode": "builtin", "long_edge": 3000}), 3000)
        self.assertIsNone(target_long_edge((1000, 800), 9.0, {"mode": "none"}))
        self.assertEqual(size_for_edge((400, 300), None), [400, 300])


class JobFlowTests(Workspace):
    def test_scan_configure_status_has_fixed_size_and_asks_questions(self):
        self.make(12, jpeg_every=4)
        scan = cli(self.root, "scan")
        self.assertIn("12 pages", scan)
        self.assertIn("project: input/book/project/project.cmc", scan)
        self.assertLessEqual(len(scan.splitlines()), 8)
        out = cli(self.root, "configure", "--name", "demo", "--images", "input/book", "--project", "input/book/project/project.cmc")
        for key in ("fit_policy", "upscale", "profile"):
            self.assertIn(f"ASK: {key}", out)
        refused = cli(self.root, "configure", "--name", "demo", "--fit", "keep_aspect", expect=2)
        self.assertIn("not implemented", refused)
        cli(self.root, "configure", "--name", "demo", "--fit", "stretch", "--upscale", "none")
        job = json.loads((self.root / "work/demo/job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["options"]["fit_policy"], "stretch")
        self.assertEqual(job["options"]["answers"]["upscale"]["value"], "none")
        self.assertEqual(job["canvas"]["pixel_size"], [2508, 3541])
        self.assertEqual(job["canvas"]["basic_frame_mm"], [28.5, 38.5, 153.5, 218.5])
        status = cli(self.root, "status")
        self.assertIn("profile MISSING", status)
        self.assertIn("next:", status)

    def test_more_images_than_cmc_pages_is_refused_and_fewer_is_noted(self):
        self.make(5, cmc_pages=3)
        refused = cli(self.root, "configure", "--name", "x", "--images", "input/book", "--project",
                      "input/book/project/project.cmc", expect=2)
        self.assertIn("5 images but the CMC has only 3 pages", refused)
        shutil.rmtree(self.root / "input")
        self.make(2, cmc_pages=4)
        cli(self.root, "configure", "--name", "y", "--images", "input/book", "--project", "input/book/project/project.cmc")
        job = json.loads((self.root / "work/y/job.json").read_text(encoding="utf-8"))
        self.assertIn("2 CMC pages beyond", job["job"]["notes"][0])

    def test_hundred_pages_cost_the_same_console_output_as_five(self):
        sizes = {}
        for count in (5, 100):
            workspace = Path(tempfile.mkdtemp(prefix="autoclip-scale-"))
            self.addCleanup(shutil.rmtree, workspace, True)
            self.root = workspace
            self.make(count, jpeg_every=7)
            self.verified_profile()
            cli(workspace, "configure", "--name", "big", "--images", "input/book", "--project", "input/book/project/project.cmc",
                "--fit", "stretch", "--upscale", "none", "--accept-quality-loss")
            outputs = [cli(workspace, "scan"), cli(workspace, "run", "analyze"), cli(workspace, "run", "upscale"),
                       cli(workspace, "status")]
            sizes[count] = [len(o) for o in outputs]
            self.assertLessEqual(sum(len(o.splitlines()) for o in outputs), 22)
        for small, large in zip(sizes[5], sizes[100]):
            self.assertLess(abs(large - small), 160, "console output must not grow with the page count")
        status = cli(workspace, "status")
        self.assertIn("analyzed 100", status)
        self.assertIn("upscaled 100", status)

    def test_quality_loss_needs_the_users_consent(self):
        self.make(3)
        self.verified_profile()
        cli(self.root, "configure", "--name", "q", "--images", "input/book", "--project", "input/book/project/project.cmc",
            "--fit", "stretch", "--upscale", "none")
        cli(self.root, "run", "analyze")
        blocked = cli(self.root, "run", "upscale", expect=2)
        self.assertIn("would be enlarged", blocked)
        cli(self.root, "configure", "--name", "q", "--accept-quality-loss")
        self.assertIn("copied", cli(self.root, "run", "upscale"))

    def test_analysis_needs_a_verified_profile_and_answers(self):
        self.make(2)
        cli(self.root, "configure", "--name", "p", "--images", "input/book", "--project", "input/book/project/project.cmc")
        self.assertIn("Unanswered", cli(self.root, "run", "analyze", expect=2))
        cli(self.root, "configure", "--name", "p", "--fit", "stretch", "--upscale", "none")
        self.assertIn("profile", cli(self.root, "run", "analyze", expect=2))


if __name__ == "__main__":
    unittest.main()
