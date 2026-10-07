"""GUI bleed must cover screen tolerance without changing reviewed baselines."""
import unittest
from copy import deepcopy

from autoclip.operations import calibrated_placement
from autoclip.split_gui import validate


class CalibratedPlacementTests(unittest.TestCase):
    def setUp(self):
        self.calib = {"canvas_screen_rect": [600, 119, 1200, 967],
                      "canvas_size_px": [2894, 4093],
                      "split": {"verified": True, "workflow": "template_split",
                                "geometry_tolerance_screen_px": 2.5,
                                "basic_frame_mm": [30.5, 39, 179.5, 258]}}
        self.plan = {"frame_workflow": "template_split", "plan_sha256": "baseline",
                     "canvas": {"basic_frame_mm": [30.5, 39, 179.5, 258]},
                     "transform": {"fit": "independent_axes", "rotation_degrees": 0},
                     "operations": [{"panel_id": "p001", "frame_line_width_px": 5.51,
                                     "frame_path_rect_px": [400, 500, 2400, 3500],
                                     "placement_rect_px": [388, 488, 2412, 3512]}]}

    def test_default_bleed_fails_but_execution_copy_passes(self):
        before = deepcopy(self.plan)
        with self.assertRaisesRegex(ValueError, "overscan"):
            validate(self.plan, self.calib)
        result = calibrated_placement(self.plan, self.calib)
        validate(result, self.calib)
        self.assertEqual(self.plan, before)
        self.assertEqual(result["plan_sha256"], "baseline")
        self.assertEqual(result["operations"][0]["frame_path_rect_px"],
                         before["operations"][0]["frame_path_rect_px"])
        self.assertEqual(result["operations"][0]["placement_rect_px"], [384, 484, 2416, 3516])

    def test_existing_larger_bleed_is_preserved(self):
        self.plan["operations"][0]["placement_rect_px"] = [370, 470, 2430, 3530]
        self.assertEqual(calibrated_placement(self.plan, self.calib), self.plan)

    def test_unverified_or_unsafe_calibration_is_rejected(self):
        self.calib["split"]["verified"] = False
        with self.assertRaisesRegex(ValueError, "not been verified"):
            calibrated_placement(self.plan, self.calib)
        self.calib["split"]["verified"] = True
        self.calib["split"]["geometry_tolerance_screen_px"] = 3
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            calibrated_placement(self.plan, self.calib)

    def test_excessive_bleed_is_rejected(self):
        self.calib["canvas_screen_rect"] = [0, 0, 10, 10]
        with self.assertRaisesRegex(ValueError, "exceeds"):
            calibrated_placement(self.plan, self.calib)


if __name__ == "__main__":
    unittest.main()
