import unittest
from pathlib import Path
from unittest.mock import Mock
from autoclip.plan import make_plan, clip_polygon, polygon_area


def rect(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def detection(size, boxes):
    return {'source': 'page.png', 'source_size': size, 'warnings': [],
            'panels': [{'source_polygon': rect(*box)} for box in boxes]}


def geometry(width, height):
    return {'basic_frame_px': [0, 0, width, height], 'size_px': [width, height], 'dpi': 350, 'warnings': []}


class FitTests(unittest.TestCase):
    def setUp(self):
        # A 2:3 page onto a 128:182 paper: the source is relatively narrower, so cover overflows vertically.
        self.found = detection([200, 300], [(0, 0, 200, 140), (0, 160, 200, 300)])
        self.paper = geometry(1280, 1820)

    def test_cover_keeps_aspect_ratio_and_fills_the_frame(self):
        plan = make_plan(self.found, self.paper, 1, 'p.clip', 'assets')
        first, second = plan['panels']
        self.assertEqual(first['scale'], [6.4, 6.4])
        # Bounds are centred: 1920px of content on a 1820px frame hides 50px at each end.
        self.assertEqual([first['canvas_polygon'][0], first['canvas_polygon'][2]], [[0, -50], [1280, 846]])
        self.assertEqual(second['canvas_polygon'][2], [1280, 1870])
        box = lambda points: [min(x for x, _ in points), min(y for _, y in points),
                              max(x for x, _ in points), max(y for _, y in points)]
        self.assertEqual(box(first['frame_polygon']), [0, 0, 1280, 846])
        self.assertEqual(box(second['frame_polygon']), [0, 974, 1280, 1820])
        for panel in plan['panels']:
            for x, y in panel['frame_polygon']:
                self.assertTrue(0 <= x <= 1280 and 0 <= y <= 1820)
        self.assertEqual(plan['warnings'], [])   # 5% hidden is accepted without a warning

    def test_stretch_is_the_previous_per_axis_fit(self):
        plan = make_plan(self.found, self.paper, 1, 'p.clip', 'assets', 'stretch')
        first = plan['panels'][0]
        self.assertAlmostEqual(first['scale'][0], 6.4)
        self.assertAlmostEqual(first['scale'][1], 1820/300)
        self.assertNotIn('frame_polygon', first)
        self.assertEqual(plan['warnings'], [])

    def test_small_overflow_is_not_worth_a_warning(self):
        plan = make_plan(detection([200, 290], [(0, 0, 200, 290)]), self.paper, 1, 'p.clip', 'assets')
        self.assertEqual(plan['panels'][0]['scale'][0], plan['panels'][0]['scale'][1])
        self.assertEqual(plan['warnings'], [])

    def test_wide_overflow_warns_for_left_and_right(self):
        plan = make_plan(detection([300, 200], [(0, 0, 300, 200)]), geometry(1000, 1000), 1, 'p.clip', 'assets')
        self.assertTrue(any('左右の約33%が隠れます' in w for w in plan['warnings']))

    def test_panels_wholly_outside_the_frame_are_dropped_by_name(self):
        found = detection([100, 300], [(0, 0, 100, 20), (0, 140, 100, 160), (0, 280, 100, 300)])
        plan = make_plan(found, geometry(1000, 1000), 3, 'p.clip', 'assets')
        self.assertEqual([p['folder_name'] for p in plan['panels']], ['AC_page0003_p002'])
        self.assertTrue(any('AC_page0003_p001 は基本枠の外' in w for w in plan['warnings']))

    def test_nothing_left_inside_the_frame_is_an_error(self):
        # Two panels at the far ends: the covered centre is empty and both ends fall outside the frame.
        found = detection([100, 300], [(0, 0, 100, 20), (0, 280, 100, 300)])
        with self.assertRaisesRegex(ValueError, '基本枠'):
            make_plan(found, geometry(1000, 1000), 1, 'p.clip', 'assets')

    def test_rectangle_flag_and_picture_mapping_are_unchanged_by_clipping(self):
        plan = make_plan(self.found, self.paper, 1, 'p.clip', 'assets')
        panel = plan['panels'][0]
        self.assertTrue(panel['rectangle'])
        # The picture still maps the whole source polygon (clipping only shapes the frame).
        sx, sy = panel['scale']
        (cx, cy), (px, py) = panel['canvas_polygon'][0], panel['source_polygon'][0]
        self.assertEqual((cx-px*sx, cy-py*sy), (0, -50))

    def test_unknown_fit_is_rejected(self):
        with self.assertRaises(ValueError):
            make_plan(self.found, self.paper, 1, 'p.clip', 'assets', 'contain')


class ClipTests(unittest.TestCase):
    def test_inside_outside_and_diagonal(self):
        self.assertEqual(clip_polygon(rect(10, 10, 20, 20), [0, 0, 100, 100]), rect(10, 10, 20, 20))
        self.assertEqual(clip_polygon(rect(200, 200, 300, 300), [0, 0, 100, 100]), [])
        triangle = [[-50, 0], [50, 0], [-50, 100]]
        clipped = clip_polygon(triangle, [0, 0, 100, 100])
        self.assertTrue(all(0 <= x <= 100 and 0 <= y <= 100 for x, y in clipped))
        self.assertAlmostEqual(polygon_area(clipped), 1250)

    def test_ui_draws_the_clipped_frame(self):
        from autoclip.ui import UI
        ui = object.__new__(UI)
        ui.desktop = Mock()
        ui.select_tool, ui.click, ui.picker = Mock(), Mock(), Mock()
        panel = {'rectangle': True, 'canvas_polygon': rect(-10, -10, 110, 110), 'frame_polygon': rect(0, 0, 100, 100),
                 'folder_name': 'x'}
        ui.frame(panel, [100, 100], [0, 0, 100, 100], 1, True)
        ui.desktop.drag.assert_called_once_with([0, 0, 100, 100])


if __name__ == '__main__':
    unittest.main()
