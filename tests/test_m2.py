import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image
from autoclip import calibrate
from autoclip.config import write_json, read_json, sha256
from autoclip.clip import copy_project, verify_saved
from autoclip.importer import import_page, import_pages
from autoclip.ui import (UI, EnvironmentUnavailable, CanvasMismatch, match, matches, topmost_match, detect_canvas,
                         screen_points, check_canvas)
from autoclip.desktop import Desktop, Input
import ctypes
from tests.support import synthetic


def saved_fixture(path, names, children=True, extra=False, nested=False):
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE Canvas(CanvasWidth, CanvasHeight)')
    con.execute('INSERT INTO Canvas VALUES(1000, 1400)')
    con.execute('CREATE TABLE Layer(MainId, LayerName, LayerFolder, VectorNormalType, ComicFrameLineMipmap, LayerFirstChildIndex, LayerNextIndex)')
    con.execute('INSERT INTO Layer VALUES(1, "", 1, NULL, NULL, 10, 0)')
    for i, name in enumerate(names+(['extra'] if extra else [])):
        ident = 10+i*2
        nxt = ident+2 if i+1 < len(names)+int(extra) else 0
        con.execute('INSERT INTO Layer VALUES(?, ?, 1, 3, ?, ?, ?)', (ident, name, b'native-frame', ident+1 if children else 0, nxt))
        if children:
            con.execute('INSERT INTO Layer VALUES(?, "image", 0, NULL, NULL, 0, 0)', (ident+1,))
    if nested:
        con.execute('UPDATE Layer SET LayerFirstChildIndex=0 WHERE MainId=1')
    con.commit()
    path.write_bytes(con.serialize())
    con.close()


class M2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root/'profile'
        self.profile.mkdir()
        rng = np.random.default_rng(4)
        self.images = {}
        parts = {}
        for i, name in enumerate(calibrate.PARTS):
            image = Image.fromarray(rng.integers(0, 256, (14, 22, 3), dtype=np.uint8))
            image.save(self.profile/f'{name}.png')
            image.save(self.profile/f'{name}.observed.png')
            self.images[name] = image
            parts[name] = {'image': f'{name}.png', 'click_offset': [0, 0], 'observed': f'{name}.observed.png'}
        write_json(self.profile/'profile.json', {'verified': True, 'parts': parts})
        self.desktop = Mock()
        self.desktop.document = None
        self.ui = UI(self.desktop, self.profile, self.root)

    def screen(self, name):
        image = Image.new('RGB', (400, 300), (40, 40, 40))
        image.paste(self.images[name], (110, 80))
        return image

    def test_match_moved_and_one_recapture(self):
        self.desktop.capture.side_effect = [Image.new('RGB', (400, 300)), self.screen('frame_tool')]
        self.assertEqual(self.ui.locate('frame_tool'), (121, 87))
        self.assertEqual(self.desktop.capture.call_count, 2)
        self.desktop.capture.side_effect = [Image.new('RGB', (400, 300))]*4
        with self.assertRaises(EnvironmentUnavailable):
            self.ui.locate('frame_tool')

    def test_canvas_threshold_close_largest_and_offset(self):
        array = np.zeros((300, 400, 3), np.uint8)+60
        array[40:270, 80:280] = [200, 190, 240]
        array[80:82, 80:280] = 60
        self.assertEqual(detect_canvas(Image.fromarray(array), [30, 20, 350, 290]), [80, 40, 280, 270])
        with self.assertRaises(ValueError):
            detect_canvas(Image.new('RGB', (400, 300)), [0, 0, 400, 300])

    def test_canvas_aspect_tolerance_levels(self):
        size = [1764, 2508]
        check_canvas([594, 119, 1190, 966], size, 'small')   # the real page, 596x847 on screen
        for level in ('small', 'medium', 'large'):
            with self.subTest(level=level), self.assertRaisesRegex(CanvasMismatch, '縦横比'):
                check_canvas([594, 119, 1190, 327], size, level)   # the 596x208 area that was accepted by mistake
        check_canvas([594, 119, 1190, 327], size, 'ignore')
        off_3 = [0, 0, 600, 828]   # about 3% off
        off_1 = [0, 0, 600, 845]   # about 1% off
        for rect, passing in ((off_1, {'medium', 'large', 'ignore'}), (off_3, {'large', 'ignore'})):
            for level in ('small', 'medium', 'large', 'ignore'):
                with self.subTest(rect=rect, level=level):
                    if level in passing:
                        check_canvas(rect, size, level)
                    else:
                        with self.assertRaises(CanvasMismatch):
                            check_canvas(rect, size, level)
        with self.assertRaises(CanvasMismatch):
            check_canvas([10, 10, 10, 50], size, 'ignore')   # an empty area is rejected whatever the tolerance
        with self.assertRaises(ValueError):
            check_canvas([0, 0, 600, 845], size, 'huge')

    def test_canvas_checks_view_order_and_aspect_before_returning(self):
        self.ui.click = Mock()
        self.desktop.capture.return_value = Image.new('RGB', (300, 420), 'white')
        points = {'view_top_left': (0, 0), 'view_bottom_right': (300, 420)}
        self.ui.locate = Mock(side_effect=lambda name, threshold=.85: points[name])
        self.assertEqual(self.ui.canvas([1000, 1400], 'medium'), [0, 0, 300, 420])
        self.assertEqual(self.ui.canvas(), [0, 0, 300, 420])
        points['view_bottom_right'] = (300, 150)
        with self.assertRaises(CanvasMismatch):
            self.ui.canvas([1000, 1400], 'medium')
        self.assertEqual(self.ui.canvas([1000, 1400], 'ignore'), [0, 0, 300, 150])
        points['view_bottom_right'] = (50, 60)   # not to the lower right of the top-left corner
        with self.assertRaises(CanvasMismatch):
            self.ui.canvas([1000, 1400], 'ignore')

    def test_view_parts_only_match_inside_their_region(self):
        wrong = Image.new('RGB', (400, 300), (40, 40, 40))
        wrong.paste(self.images['view_bottom_right'], (110, 80))   # a look-alike in the upper left
        self.desktop.capture.return_value = wrong
        with self.assertRaisesRegex(EnvironmentUnavailable, 'view_bottom_right'):
            self.ui.locate('view_bottom_right')
        wrong.paste(self.images['view_bottom_right'], (300, 230))
        self.assertEqual(self.ui.locate('view_bottom_right'), (311, 237))
        corner = Image.new('RGB', (400, 300), (40, 40, 40))
        corner.paste(self.images['view_top_left'], (20, 10))
        self.desktop.capture.return_value = corner
        self.assertEqual(self.ui.locate('view_top_left'), (31, 17))

    def test_matches_counts_distinct_places_not_neighbouring_pixels(self):
        template = self.images['view_bottom_right']
        screen = Image.new('RGB', (400, 300), (40, 40, 40))
        screen.paste(template, (50, 50))
        self.assertEqual(len(matches(screen, template, .9)), 1)
        screen.paste(template, (250, 200))
        self.assertEqual(len(matches(screen, template, .9)), 2)
        self.assertEqual(matches(Image.new('RGB', (10, 10)), template, .9), [])

    def test_check_rejects_part_matching_several_places(self):
        template = self.images['view_bottom_right']
        twice = Image.new('RGB', (400, 300), (40, 40, 40))
        twice.paste(template, (50, 50))
        twice.paste(template, (250, 200))
        twice.save(self.profile/'view_bottom_right.observed.png')
        with self.assertRaisesRegex(EnvironmentUnavailable, 'view_bottom_right.*複数箇所'):
            calibrate.check(self.desktop, self.profile, {'panels': [{}]}, self.root/'work')
        self.assertFalse(read_json(self.profile/'profile.json')['verified'])
        # A part that repeats by design (one folder icon per frame row) is not an error.
        once = Image.new('RGB', (400, 300), (40, 40, 40))
        once.paste(template, (50, 50))
        once.save(self.profile/'view_bottom_right.observed.png')
        repeated = Image.new('RGB', (400, 300), (40, 40, 40))
        repeated.paste(self.images['top_folder'], (50, 50))
        repeated.paste(self.images['top_folder'], (250, 200))
        repeated.save(self.profile/'top_folder.observed.png')
        with self.assertRaises(ValueError) as raised:   # passes the part checks; the one-panel rule then stops it
            calibrate.check(self.desktop, self.profile, {'panels': [{}, {}]}, self.root/'work')
        self.assertIn('1コマ', str(raised.exception))

    def test_small_part_gets_a_warning_on_crop(self):
        self.screen('frame_tool').save(self.profile/'screen.png')
        self.assertTrue(calibrate.crop(self.profile, 'frame_tool', [110, 80, 132, 94]))   # 22x14 is small
        self.assertEqual(calibrate.crop(self.profile, 'frame_tool', [100, 70, 140, 110]), [])

    def test_canvas_mismatch_closes_restores_and_stops(self):
        plan = self.plan()
        backup = (self.work/'backups'/Path(plan['clip_path']).name).read_bytes()
        ui = self.mock_ui(plan)
        ui.canvas.side_effect = CanvasMismatch('用紙領域の縦横比が合いません。')
        Path(plan['clip_path']).write_bytes(b'opened and touched')
        with self.assertRaises(CanvasMismatch):
            import_page(plan, ui, self.work, tolerance='small')
        ui.canvas.assert_called_once_with(plan['canvas_size'], 'small')
        self.assertEqual(ui.open_page.call_count, 1)   # no retry: every attempt would repeat it
        ui.close.assert_called_once()
        ui.frame.assert_not_called()
        self.assertEqual(Path(plan['clip_path']).read_bytes(), backup)
        self.assertFalse((self.work/'pages/page0001.done').exists())

    def test_snapshot_is_taken_after_frames_before_save(self):
        plan = self.plan()
        ui = self.mock_ui(plan)
        order = []
        ui.frame.side_effect = lambda *a: order.append('frame') or 1.2
        ui.snapshot.side_effect = lambda rect, path: order.append(('snapshot', rect, path))
        original_save = ui.save.side_effect
        ui.save.side_effect = lambda path: (order.append('save'), original_save(path))[1]
        import_page(plan, ui, self.work, snapshot=self.root/'shot.png')
        self.assertEqual(order, ['frame', ('snapshot', [0, 0, 300, 420], self.root/'shot.png'), 'save'])

    def test_topmost_and_selected_frame_tool(self):
        screen = self.screen('top_folder')
        screen.paste(self.images['top_folder'], (180, 20))
        self.assertEqual(topmost_match(screen, self.images['top_folder'], .9), (191, 27))
        self.desktop.capture.return_value = screen
        self.assertEqual(self.ui.locate('top_folder'), (191, 27))
        self.desktop.capture.return_value = self.screen('frame_tool_on')
        self.assertEqual(self.ui.locate('frame_tool'), (121, 87))
        self.assertEqual(self.desktop.capture.call_count, 4)
        self.assertIsNone(topmost_match(Image.new('RGB', (400, 300)), self.images['top_folder'], .9))

    def test_locate_error_contains_part(self):
        self.desktop.capture.return_value = Image.new('RGB', (400, 300))
        with self.assertRaisesRegex(EnvironmentUnavailable, 'rectangle'):
            self.ui.locate('rectangle')

    def test_save_waits_for_disk_change_and_times_out(self):
        from types import SimpleNamespace
        before = SimpleNamespace(st_mtime_ns=1, st_size=20)
        for after in (SimpleNamespace(st_mtime_ns=2, st_size=20),
                      SimpleNamespace(st_mtime_ns=1, st_size=21)):
            with patch('autoclip.ui.Path.stat', side_effect=[before, before, after]), \
                 patch('autoclip.ui.time.monotonic', side_effect=[0, 0, 1]), \
                 patch('autoclip.ui.time.sleep') as sleep:
                self.ui.save(self.root/'page.clip')
                self.assertEqual([c.args for c in sleep.call_args_list], [(.2,), (1.0,)])
        self.desktop.title.assert_not_called()
        with patch('autoclip.ui.Path.stat', return_value=before), \
             patch('autoclip.ui.time.monotonic', side_effect=[0, 0, 30]), patch('autoclip.ui.time.sleep'):
            with self.assertRaisesRegex(ValueError, '保存完了'):
                self.ui.save(self.root/'page.clip')

    def test_close_matches_delayed_dialog_and_no_dialog(self):
        self.ui.locate = Mock(side_effect=[EnvironmentUnavailable('待機'), (100, 100)])
        self.ui.click = Mock()
        with patch('autoclip.ui.time.monotonic', side_effect=[0, 0, .2, .4]), patch('autoclip.ui.time.sleep'):
            self.ui.close()
        self.assertEqual([c.args for c in self.ui.locate.call_args_list], [('save_dialog',)]*2)
        self.ui.click.assert_called_once_with('discard_button')
        self.desktop.discard_dialog.assert_not_called()
        self.ui.click.reset_mock()
        self.ui.locate.side_effect = EnvironmentUnavailable('待機')
        with patch('autoclip.ui.time.monotonic', side_effect=[0, 0, 3]), patch('autoclip.ui.time.sleep'):
            self.ui.close()
        self.ui.click.assert_not_called()
        self.assertEqual(self.desktop.wait_main.call_count, 2)

    def test_picker_waits_for_dialog_up_to_six_seconds(self):
        path = self.root/'asset.png'
        path.write_bytes(b'fixture')
        self.desktop.file_name_text.return_value = str(path)
        self.ui.locate = Mock(side_effect=[EnvironmentUnavailable('待機'), (100, 100)])
        with patch('autoclip.ui.time.monotonic', side_effect=[0, 1]), patch('autoclip.ui.time.sleep') as sleep:
            self.ui.picker(path, 'image_dialog')
        sleep.assert_called_once_with(.3)
        self.desktop.reset_mock()
        self.ui.locate.side_effect = EnvironmentUnavailable('待機')
        with patch('autoclip.ui.time.monotonic', side_effect=[0, 7]), patch('autoclip.ui.time.sleep'):
            with self.assertRaises(EnvironmentUnavailable):
                self.ui.picker(path)
        self.desktop.text.assert_not_called()

    def test_focus_preserves_maximization_and_restores_minimized(self):
        desktop = object.__new__(Desktop)
        desktop.u, desktop.hwnd, desktop.pid = Mock(), 12, 42
        desktop.process = Mock(return_value=42)
        with patch('autoclip.desktop.time.sleep'):
            desktop.u.IsIconic.return_value = False
            desktop.focus()
            desktop.u.ShowWindow.assert_not_called()
            desktop.u.IsIconic.return_value = True
            desktop.focus()
            desktop.u.ShowWindow.assert_called_once_with(12, 9)

    def test_minimized_window_detection_and_generic_title_guard(self):
        user, kernel = Mock(), Mock()
        user.IsWindowVisible.return_value = True
        user.IsIconic.return_value = True
        user.EnumWindows.side_effect = lambda callback, _: callback(12, 0)
        with patch('autoclip.desktop.C.WinDLL', side_effect=[user, kernel, user, kernel]), \
             patch.object(Desktop, 'title', return_value='CLIP STUDIO PAINT'), \
             patch.object(Desktop, 'rect', return_value=[-32000, -32000, -31900, -31970]), \
             patch.object(Desktop, 'process', return_value=42):
            desktop = Desktop()
            self.assertEqual(desktop.hwnd, 12)
            desktop.document = 'practice'
            desktop.guard()  # generic title is allowed
            with patch.object(Desktop, 'title', return_value='other - CLIP STUDIO PAINT'):
                with self.assertRaises(EnvironmentUnavailable):
                    desktop.guard()
            user.IsIconic.return_value = False
            with self.assertRaises(EnvironmentUnavailable):
                Desktop()  # a tiny non-minimized dialog isn't the main window

    def test_root_layer_detected_by_name_and_folder(self):
        path = self.root/'saved.clip'
        panels = [{'folder_name': 'AC_page0001_p001'}]
        saved_fixture(path, ['AC_page0001_p001'])
        con = sqlite3.connect(':memory:')
        con.deserialize(path.read_bytes())
        con.execute('UPDATE Layer SET MainId=999 WHERE MainId=1')
        con.execute('INSERT INTO Layer VALUES(1, "", 0, NULL, NULL, 0, 0)')
        con.commit()
        path.write_bytes(con.serialize())
        verify_saved(path, panels)
        con.execute('UPDATE Layer SET LayerName="not-root" WHERE MainId=999')
        con.commit()
        path.write_bytes(con.serialize())
        with self.assertRaisesRegex(ValueError, 'ルート'):
            verify_saved(path, panels)
        con.close()

    def test_check_threshold_only_discard_is_point_eight(self):
        plan = self.plan()
        def exercise(plan, ui, *args, **kwargs):
            ui.locate('discard_button')
            ui.locate('frame_tool', .85)
            ui.locate('rectangle', .97)
            return {'status': '成功'}
        with patch.object(UI, '_locate', return_value=(10, 20)) as locate, \
             patch('autoclip.calibrate.import_page', side_effect=exercise):
            calibrate.check(self.desktop, self.profile, plan, self.work)
        self.assertEqual([c.args for c in locate.call_args_list],
                         [('discard_button', .8), ('frame_tool', .9), ('rectangle', .97)])

    def test_coordinate_rounding_and_rectangle_drag(self):
        poly = [[0, 0], [1000, 0], [1000, 1400], [0, 1400]]
        points, error = screen_points(poly, [1000, 1400], [101, 41, 401, 461])
        self.assertEqual(points, [(101, 41), (401, 41), (401, 461), (101, 461)])
        self.assertEqual(error, 0)
        _, error = screen_points([[1, 1], [99, 199]], [1000, 1400], [101, 41, 401, 461])
        self.assertGreater(error, 0)
        self.assertLessEqual(error, .5/.3)
        self.ui.select_tool = Mock()
        self.ui.click = Mock()
        panel = {'rectangle': True, 'canvas_polygon': poly, 'folder_name': 'AC_page0001_p001'}
        self.ui.frame(panel, [1000, 1400], [101, 41, 401, 461], 5, True)
        self.desktop.drag.assert_called_once_with([101, 41, 401, 461])
        self.desktop.text.assert_called_once_with(panel['folder_name'])
        self.ui.click.assert_any_call('top_folder', double=True)
        self.ui.click.assert_any_call('top_folder')

    def test_polyline_enter_rename_then_import(self):
        calls = []
        self.ui.select_tool = lambda *a: calls.append('tool')
        self.ui.click = lambda name, **kw: calls.append(name)
        self.ui.picker = lambda path, *args: calls.append('picker')
        self.desktop.click.side_effect = lambda point: calls.append(('point', point))
        self.desktop.press.side_effect = lambda *keys: calls.append(keys)
        self.desktop.text.side_effect = lambda value: calls.append(('text', value))
        panel = {'rectangle': False, 'canvas_polygon': [[100, 100], [900, 100], [950, 1000], [100, 900]],
                 'folder_name': 'AC_page0001_p001', 'image_path': 'image.png'}
        self.ui.frame(panel, [1000, 1400], [0, 0, 300, 420], 5, False)
        self.assertEqual(calls[:6], ['tool', ('point', (30, 30)), ('point', (270, 30)),
                                   ('point', (285, 300)), ('point', (30, 270)), ('ENTER',)])
        self.assertLess(calls.index(('text', panel['folder_name'])), calls.index('file_menu'))
        self.assertEqual(calls[-5:], ['file_menu', 'import_menu', 'image_menu', 'picker', 'top_folder'])

    def test_check_off_idempotent_and_on_toggle(self):
        self.desktop.capture.return_value = self.screen('raster_off')
        self.ui.ensure_off('raster')
        self.desktop.click.assert_not_called()
        self.desktop.capture.side_effect = [self.screen('raster_on'), self.screen('raster_on'), self.screen('raster_off')]
        self.ui.ensure_off('raster')
        self.desktop.click.assert_called_once()

    def test_tool_properties_set_for_each_tool(self):
        self.ui.click, self.ui.ensure_off = Mock(), Mock()
        self.ui.select_tool(False, 5.5)
        self.assertEqual([c.args[0] for c in self.ui.click.call_args_list], ['frame_tool', 'frame_create', 'polyline', 'brush_size'])
        self.assertEqual([c.args[0] for c in self.ui.ensure_off.call_args_list], ['raster', 'fill'])

    def test_file_name_retries_no_enter_on_mismatch(self):
        path = self.root/'asset.png'
        path.write_bytes(b'fixture')
        self.ui.locate = Mock()
        self.desktop.file_name_text.side_effect = ['wrong', 'wrong', str(path)]
        self.ui.picker(path)
        self.assertEqual(self.desktop.text.call_count, 3)
        self.desktop.wait_main.assert_called_once()
        self.desktop.reset_mock()
        self.desktop.file_name_text.side_effect = ['wrong']*3
        with self.assertRaises(ValueError):
            self.ui.picker(path)
        self.assertNotIn(('ENTER',), [c.args for c in self.desktop.press.call_args_list])

    def test_import_picker_failure_cancels_before_recovery(self):
        self.ui.select_tool, self.ui.click = Mock(), Mock()
        self.ui.picker = Mock(side_effect=ValueError('ファイル名を確認できません。'))
        panel = {'rectangle': True, 'canvas_polygon': [[0, 0], [1000, 0], [1000, 1400], [0, 1400]],
                 'folder_name': 'AC_page0001_p001', 'image_path': 'fixture.png'}
        with self.assertRaises(ValueError):
            self.ui.frame(panel, [1000, 1400], [0, 0, 300, 420], 5, False)
        self.desktop.press.assert_any_call('ESC', modal=True)
        self.desktop.wait_main.assert_called_once()

    def test_desktop_owned_dialog_focus_point_guard_and_handoff(self):
        desktop = object.__new__(Desktop)
        desktop.u = Mock()
        desktop.hwnd, desktop.pid, desktop.return_window = 1, 4, 3
        desktop.u.GetForegroundWindow.return_value = 2  # same-process modal dialog
        desktop.process = Mock(return_value=4)
        with patch('autoclip.desktop.time.sleep'):
            desktop.focus()
        desktop.u.SetForegroundWindow.assert_called_once_with(1)
        desktop.guard_point((20, 30))
        desktop.process.return_value = 9
        with self.assertRaises(EnvironmentUnavailable):
            desktop.guard_point((20, 30))
        desktop.u.IsWindow.return_value = True
        desktop.minimize()
        desktop.u.ShowWindow.assert_called_with(1, 6)
        desktop.u.SetForegroundWindow.assert_called_with(3)

    def plan(self):
        cmc = synthetic.make_project(self.root/'input', pages=2)
        self.work = self.root/'work/job'
        info = copy_project(cmc, self.work)
        return {'page_number': 1, 'clip_path': info['pages'][0]['path'], 'canvas_size': [1000, 1400],
                'frame_line_px': 5, 'warnings': [], 'panels': [{'folder_name': 'AC_page0001_p001'}]}

    def mock_ui(self, plan):
        ui = Mock()
        ui.frame.return_value = 1.2
        ui.canvas.return_value = [0, 0, 300, 420]
        ui.save.side_effect = lambda path: saved_fixture(Path(path), [p['folder_name'] for p in plan['panels']])
        return ui

    def test_save_sqlite_done_and_rerun_skip(self):
        plan = self.plan()
        ui = self.mock_ui(plan)
        result = import_page(plan, ui, self.work)
        self.assertEqual(result['status'], '成功')
        marker = self.work/'pages/page0001.done'
        self.assertEqual(read_json(marker)['sha256'], sha256(plan['clip_path']))
        ui.reset_mock()
        self.assertEqual(import_page(plan, ui, self.work)['status'], '完了済み')
        ui.open_page.assert_not_called()
        Path(plan['clip_path']).write_bytes(b'changed')
        import_page(plan, ui, self.work)
        ui.open_page.assert_called_once()

    def test_retry_restore_once_and_skip_next_page(self):
        plan = self.plan()
        backup = (self.work/'backups'/Path(plan['clip_path']).name).read_bytes()
        ui = self.mock_ui(plan)
        attempts = []
        def retry_save(path):
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError('保存失敗')
            saved_fixture(Path(plan['clip_path']), ['AC_page0001_p001'])
        ui.save.side_effect = retry_save
        self.assertEqual(import_page(plan, ui, self.work)['status'], '成功')
        self.assertEqual(ui.open_page.call_count, 2)
        # New job: twice fails, both times restores byte-for-byte, next page proceeds.
        (self.work/'pages/page0001.done').unlink()
        ui = self.mock_ui(plan)
        ui.save.side_effect = lambda path: Path(path).write_bytes(b'invalid')
        result = import_page(plan, ui, self.work)
        self.assertEqual(result['status'], '失敗')
        self.assertEqual(ui.open_page.call_count, 2)
        self.assertEqual(Path(plan['clip_path']).read_bytes(), backup)
        self.assertFalse((self.work/'pages/page0001.done').exists())
        next_plan = {**plan, 'page_number': 2, 'clip_path': str(self.work/'project/page0002.clip'),
                     'panels': [{'folder_name': 'AC_page0002_p001'}]}
        def save_current(path):
            current = Path(path)
            if current.name == 'page0001.clip':
                current.write_bytes(b'invalid')
            else:
                saved_fixture(current, ['AC_page0002_p001'])
        ui.save.side_effect = save_current
        results = import_pages([plan, next_plan], ui, self.work)
        self.assertEqual([r['status'] for r in results], ['失敗', '成功'])

    def test_sqlite_counts_names_children_and_root(self):
        plan = self.plan()
        path = Path(plan['clip_path'])
        names = [p['folder_name'] for p in plan['panels']]
        for kwargs in ({'extra': True}, {'children': False}, {'nested': True}):
            saved_fixture(path, names, **kwargs)
            with self.assertRaises(ValueError):
                verify_saved(path, plan['panels'])
        saved_fixture(path, names, children=False)
        verify_saved(path, plan['panels'], True)
        saved_fixture(path, ['wrong'])
        with self.assertRaises(ValueError):
            verify_saved(path, plan['panels'])

    def test_unsafe_tab_close_stops_without_restore(self):
        plan = self.plan()
        ui = self.mock_ui(plan)
        ui.save.side_effect = ValueError('failure')
        ui.close.side_effect = ValueError('cannot close')
        with self.assertRaises(EnvironmentUnavailable):
            import_page(plan, ui, self.work)
        self.assertEqual(ui.open_page.call_count, 1)

    def test_calibration_crop_saves_no_absolute_coordinates(self):
        self.screen('frame_tool').save(self.profile/'screen.png')
        calibrate.crop(self.profile, 'frame_tool', [110, 80, 132, 94], [2, -3])
        item = read_json(self.profile/'profile.json')['parts']['frame_tool']
        self.assertEqual(set(item), {'image', 'click_offset', 'observed'})
        self.assertFalse(read_json(self.profile/'profile.json')['verified'])

    def test_calibration_verified_only_after_self_test(self):
        plan = self.plan()
        self.desktop.document = None
        with patch('autoclip.calibrate.import_page', return_value={'status': '失敗'}):
            with self.assertRaises(EnvironmentUnavailable):
                calibrate.check(self.desktop, self.profile, plan, self.work)
        self.assertFalse(read_json(self.profile/'profile.json')['verified'])
        with patch('autoclip.calibrate.import_page', return_value={'status': '成功'}) as importer:
            calibrate.check(self.desktop, self.profile, plan, self.work)
            self.assertTrue(read_json(self.profile/'profile.json')['verified'])
            self.assertFalse(importer.call_args.args[3])

    def test_desktop_sendinput_keyboard_mouse_and_rejection(self):
        desktop = object.__new__(Desktop)
        desktop.u = Mock()
        events = []
        def send(count, pointer, size):
            event = ctypes.cast(pointer, ctypes.POINTER(Input)).contents
            if event.type == 1:
                events.append((1, event.payload.keyboard.vk, event.payload.keyboard.flags))
            else:
                events.append((0, event.payload.mouse.flags))
            self.assertEqual(size, ctypes.sizeof(Input))
            return 1
        desktop.u.SendInput.side_effect = send
        desktop.guard = Mock()
        with patch('autoclip.desktop.time.sleep'):
            desktop.press('CTRL', 'S')
            desktop.click((20, 30))
        self.assertEqual(events, [(1, 17, 0), (1, 83, 0), (1, 83, 2), (1, 17, 2), (0, 2), (0, 4)])
        desktop.u.SendInput.side_effect = None
        desktop.u.SendInput.return_value = 0
        with self.assertRaises(EnvironmentUnavailable):
            desktop.send(1, 0, 13)

    def test_complete_ui_and_sqlite_lifecycle_with_mock_desktop(self):
        plan = self.plan()
        plan['panels'][0].update(rectangle=True, canvas_polygon=[[100, 100], [900, 100], [900, 1300], [100, 1300]],
                                image_path=str(self.root/'asset.png'))
        Path(plan['panels'][0]['image_path']).write_bytes(b'fixture')
        self.ui.locate = Mock(side_effect=lambda name, threshold=.85: {'view_top_left': (0, 0), 'view_bottom_right': (300, 420)}.get(name, (10, 10)))
        self.ui.ensure_off = Mock()
        self.desktop.capture.return_value = Image.new('RGB', (300, 420), 'white')
        last_text = []
        self.desktop.text.side_effect = lambda text, **kw: last_text.append(text)
        self.desktop.file_name_text.side_effect = lambda: last_text[-1]
        self.desktop.discard_dialog.return_value = False
        self.desktop.title.side_effect = lambda *args: (self.desktop.document or '')+' - CLIP STUDIO PAINT'
        def press(*keys, **kw):
            if keys == ('CTRL', 'S'):
                saved_fixture(Path(plan['clip_path']), ['AC_page0001_p001'])
        self.desktop.press.side_effect = press
        result = import_page(plan, self.ui, self.work)
        self.assertEqual(result['status'], '成功')
        self.assertTrue((self.work/'pages/page0001.done').exists())
        calls = [c.args for c in self.desktop.press.call_args_list]
        self.assertLess(calls.index(('CTRL', 'S')), calls.index(('CTRL', 'W')))
        self.assertEqual(self.desktop.document, None)

    def test_calibration_prepare_click_and_reject_original(self):
        plan = self.plan()
        self.desktop.capture.return_value = self.screen('frame_tool')
        calibrate.prepare(self.desktop, self.profile, plan['clip_path'], self.work)
        self.desktop.open_practice.assert_called_once()
        self.assertFalse(read_json(self.profile/'profile.json')['verified'])
        calibrate.click(self.desktop, self.profile, (10, 20))
        self.desktop.click.assert_called_with((10, 20), modal=True)
        with self.assertRaises(ValueError):
            calibrate.prepare(self.desktop, self.profile, self.root/'input/page0001.clip', self.work)

    def test_focus_environment_failure_is_not_retried(self):
        plan = self.plan()
        ui = self.mock_ui(plan)
        ui.frame.side_effect = EnvironmentUnavailable('別のタブです。')
        with self.assertRaises(EnvironmentUnavailable):
            import_page(plan, ui, self.work)
        self.assertEqual(ui.open_page.call_count, 1)
        ui.close.assert_not_called()

    def test_empty_frames_only_saved_without_image_import(self):
        plan = self.plan()
        ui = self.mock_ui(plan)
        ui.save.side_effect = lambda path: saved_fixture(Path(path), ['AC_page0001_p001'], children=False)
        self.assertEqual(import_page(plan, ui, self.work, True)['status'], '成功')
        self.assertTrue(ui.frame.call_args.args[-1])
