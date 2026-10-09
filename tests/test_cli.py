import contextlib
import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image, ImageDraw
from autoclip.__main__ import main
from autoclip.config import read_json, write_json, sha256
from autoclip.assets import prepare_assets
from autoclip import calibrate
from autoclip.ui import UI, EnvironmentUnavailable
from tests.support import synthetic
from tests.test_m2 import saved_fixture


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cmc = synthetic.make_project(self.root/'input/project', pages=3)
        self.images = self.root/'input/images'
        synthetic.make_pages(self.images, 3)
        self.profile = self.root/'profile/ui'
        write_json(self.profile/'profile.json', {'verified': True, 'parts': {}})
        self.desktop = Mock()
        self.desktop.document = None
        self.desktop.identity.return_value = {'pid': 1, 'hwnd': 2, 'screen': [300, 420], 'dpi': 96}
        self.desktop.capture.return_value = Image.new('RGB', (300, 420), 'white')
        self.desktop.title.return_value = 'CLIP STUDIO PAINT'
        self.desktop.discard_dialog.return_value = False
        self.last_text = ''
        self.current = None
        self.names = []
        self.opens = []
        self.saves = Counter()
        self.failed_pages = set()
        self.desktop.text.side_effect = self.text
        self.desktop.file_name_text.side_effect = lambda: self.last_text
        self.desktop.press.side_effect = self.press
        self.desktop.open_practice.side_effect = lambda path: setattr(self.desktop, 'document', Path(path).stem)
        for target in ('autoclip.__main__.Desktop', 'autoclip.setup.Desktop'):
            self.addCleanup(patch.stopall)
            patch(target, return_value=self.desktop).start()
        patch.object(UI, 'locate', side_effect=lambda name, threshold=.85: {
            'view_top_left': (0, 0), 'view_bottom_right': (300, 420)}.get(name, (10, 10))).start()
        patch.object(UI, 'ensure_off').start()

    def text(self, text, **kwargs):
        self.last_text = text
        if text.endswith('.clip'):
            self.current = Path(text)
            self.names = []
            self.opens.append(self.current.name)
            # Every retry/rerun starts with a byte-for-byte restored backup.
            backup = self.current.parent.parent/'backups'/self.current.name
            self.assertEqual(self.current.read_bytes(), backup.read_bytes())
        elif text.startswith('AC_page'):
            self.names.append(text)

    def press(self, *keys, **kwargs):
        if keys == ('CTRL', 'S'):
            self.saves[self.current.name] += 1
            if self.current.name in self.failed_pages:
                self.current.write_bytes(b'failed working save')
            else:
                saved_fixture(self.current, self.names, children=True)

    def invoke(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(argv, self.root)
        return code, output.getvalue()

    def argv(self, *extra):
        # --all skips the trial page; the trial tests below call trial_argv instead.
        return ['run', str(self.images), '--cmc', str(self.cmc), '--job', 'job', '--all', *extra]

    def trial_argv(self, *extra):
        return ['run', str(self.images), '--cmc', str(self.cmc), '--job', 'job', *extra]

    def test_trial_page_first_then_rest_without_second_trial(self):
        code, output = self.invoke(self.trial_argv())
        self.assertEqual(code, 0, output)
        self.assertEqual(self.opens, ['page0001.clip'])
        self.assertIn('試運転', output)
        self.assertTrue((self.root/'output/job/trial.png').is_file())
        self.assertTrue((self.root/'output/job/trial_screen.png').is_file())
        self.assertIn('完了 1 / 3ページ', self.invoke(['status', '--job', 'job'])[1])
        (self.root/'output/job/trial.png').unlink()
        self.opens.clear()
        code, output = self.invoke(['run', '--job', 'job'])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.opens, ['page0002.clip', 'page0003.clip'])
        self.assertNotIn('試運転', output)
        self.assertFalse((self.root/'output/job/trial.png').exists())
        self.assertIn('成功 3 / スキップ 0', output)

    def test_trial_is_skipped_by_all_page_and_dry_run(self):
        self.assertEqual(self.invoke(self.trial_argv('--dry-run'))[0], 0)
        self.assertEqual(self.opens, [])
        self.assertEqual(self.invoke(self.trial_argv('--page', '2'))[0], 0)
        self.assertEqual(self.opens, ['page0002.clip'])
        self.assertNotIn('試運転', self.invoke(self.argv('--job', 'other'))[1])
        self.assertEqual(self.opens[1:], ['page0001.clip', 'page0002.clip', 'page0003.clip'])

    def test_fit_defaults_to_cover_is_saved_and_locked_per_job(self):
        self.assertEqual(self.invoke(self.argv('--dry-run'))[0], 0)
        self.assertEqual(read_json(self.root/'work/job/job.json')['fit'], 'cover')
        plan = read_json(self.root/'work/job/pages/page0001.plan.json')
        self.assertEqual(plan['panels'][0]['scale'][0], plan['panels'][0]['scale'][1])
        code, output = self.invoke(self.argv('--dry-run', '--fit', 'stretch'))
        self.assertEqual(code, 1)
        self.assertIn('新しい作業名', output)
        self.assertEqual(self.invoke(self.argv('--dry-run', '--job', 'other', '--fit', 'stretch'))[0], 0)
        self.assertEqual(read_json(self.root/'work/other/job.json')['fit'], 'stretch')
        self.assertNotIn('frame_polygon', read_json(self.root/'work/other/pages/page0001.plan.json')['panels'][0])

    def wrong_view(self):
        # A look-alike far from the real bottom-right: the paper area comes out wide and short.
        return patch.object(UI, 'locate', side_effect=lambda name, threshold=.85: {
            'view_top_left': (0, 0), 'view_bottom_right': (300, 150)}.get(name, (10, 10)))

    def test_wrong_paper_area_stops_before_drawing_and_leaves_no_done(self):
        with self.wrong_view():
            code, output = self.invoke(self.argv())
        self.assertEqual(code, 1)
        self.assertIn('用紙領域の縦横比が合いません', output)
        self.desktop.drag.assert_not_called()
        self.desktop.minimize.assert_not_called()
        work = self.root/'work/job'
        self.assertEqual(list((work/'pages').glob('*.done')), [])
        self.assertEqual((work/'project/page0001.clip').read_bytes(), (work/'backups/page0001.clip').read_bytes())
        self.assertEqual(self.opens, ['page0001.clip'])
        self.assertIn('失敗', read_json(self.root/'output/job/summary.json')['pages'][0]['status'])

    def test_tolerance_option_is_saved_and_ignore_accepts_wrong_shape(self):
        with self.wrong_view():
            code, output = self.invoke(self.argv('--job', 'loose', '--tolerance', 'ignore', '--page', '1'))
        self.assertEqual(code, 0, output)
        self.assertEqual(read_json(self.root/'work/loose/job.json')['tolerance'], 'ignore')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(self.argv('--tolerance', 'huge'), self.root)

    def test_multi_page_failure_restore_retry_skip_rerun_and_status(self):
        original = {p: sha256(p) for p in self.cmc.parent.iterdir()}
        self.failed_pages = {'page0002.clip'}
        code, output = self.invoke(self.argv())
        self.assertEqual(code, 0, output)
        self.assertIn('成功 2 / スキップ 1', output)
        self.assertEqual(self.saves, {'page0001.clip': 1, 'page0002.clip': 2, 'page0003.clip': 1})
        work = self.root/'work/job'
        self.assertEqual((work/'project/page0002.clip').read_bytes(), (work/'backups/page0002.clip').read_bytes())
        self.assertFalse((work/'pages/page0002.done').exists())
        self.assertEqual(original, {p: sha256(p) for p in self.cmc.parent.iterdir()})
        code, output = self.invoke(['status', '--job', 'job'])
        self.assertEqual(code, 0)
        self.assertIn('完了 2 / 3ページ', output)
        self.assertIn('ページ2：失敗・スキップ', output)
        self.assertIn('次の1コマンド：autoclip run --job job', output)
        self.failed_pages.clear()
        self.opens.clear()
        # Done pages must skip before image analysis or reading modified page metadata.
        (self.images/'page-001.png').write_bytes(b'unreadable source')
        code, output = self.invoke(['run', '--job', 'job'])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.opens, ['page0002.clip'])
        self.assertIn('成功 3 / スキップ 0', output)
        self.assertEqual(self.desktop.minimize.call_count, 2)
        self.opens.clear()
        self.invoke(['run', '--job', 'job'])
        self.assertEqual(self.opens, [])
        self.assertIn('完了 3 / 3ページ', self.invoke(['status', '--job', 'job'])[1])

    def test_retry_success_and_single_page_frames_only(self):
        original_press = self.press
        def once(*keys, **kwargs):
            if keys == ('CTRL', 'S') and not self.saves:
                self.current.write_bytes(b'failed')
                self.saves[self.current.name] += 1
            else:
                original_press(*keys, **kwargs)
        self.desktop.press.side_effect = once
        code, output = self.invoke(self.argv('--page', '2', '--frames-only'))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.opens, ['page0002.clip', 'page0002.clip'])
        self.assertFalse((self.root/'work/job/assets').exists())
        self.assertFalse((self.root/'work/job/pages/page0001.done').exists())
        self.assertTrue((self.root/'work/job/pages/page0002.done').is_file())
        self.assertIn('完了 1 / 3ページ', self.invoke(['status', '--job', 'job'])[1])

    def test_partial_rerun_retains_other_failed_reasons(self):
        self.failed_pages = {'page0001.clip', 'page0002.clip'}
        self.invoke(self.argv())
        self.failed_pages.clear()
        self.invoke(['run', '--job', 'job', '--page', '2'])
        output = self.invoke(['status', '--job', 'job'])[1]
        self.assertIn('ページ1：失敗・スキップ', output)
        self.assertNotIn('ページ2：失敗・スキップ', output)

    def test_review_cancel_never_inputs(self):
        with patch('autoclip.__main__.review_result', return_value=False):
            self.assertEqual(self.invoke(self.argv('--review'))[0], 1)
        self.desktop.press.assert_not_called()

    def test_review_only_when_requested_and_before_import(self):
        def confirm(output):
            self.assertTrue((output/'index.html').is_file())
            self.desktop.press.assert_not_called()
            return True
        with patch('autoclip.__main__.review_result', side_effect=confirm) as review:
            code, output = self.invoke(self.argv('--review', '--frames-only'))
            self.assertEqual(code, 0, output)
            review.assert_called_once()
        with patch('autoclip.__main__.review_result') as review:
            self.assertEqual(self.invoke(self.argv('--job', 'default', '--frames-only'))[0], 0)
            review.assert_not_called()

    def test_setup_click_multiple_coordinates(self):
        self.desktop.capture.return_value = Image.fromarray(np.random.default_rng(8).integers(
            0, 255, (420, 300, 3), dtype=np.uint8))
        self.assertEqual(self.invoke(['setup', 'prepare', '--cmc', str(self.cmc), '--recalibrate'])[0], 0)
        self.assertEqual(self.invoke(['setup', 'click', '20', '30', '40', '50', '60', '70'])[0], 0)
        self.assertEqual([c.args[0] for c in self.desktop.click.call_args_list], [(20, 30), (40, 50), (60, 70)])
        self.desktop.click.reset_mock()
        self.assertEqual(self.invoke(['setup', 'click', '20', '30', '40'])[0], 1)
        self.desktop.click.assert_not_called()

    def test_upscale_failure_skips_page_and_none_warning_continues(self):
        from autoclip.upscale import UpscaleFailed
        from autoclip.__main__ import prepare_assets
        def assets(plan, grain, policy, root):
            if plan['page_number'] == 2:
                raise UpscaleFailed('コマ画像の拡大に失敗しました。')
            return prepare_assets(plan, grain)  # no real model/desktop in tests
        with patch('autoclip.__main__.prepare_assets', side_effect=assets) as preparer:
            code, output = self.invoke(self.argv('--upscale', 'external', '--python', str(self.root/'python.exe'),
                                                   '--model', str(self.root/'model.safetensors'), '--long-edge', 'auto'))
        self.assertEqual(code, 0, output)
        self.assertEqual(preparer.call_count, 3)
        self.assertIn('成功 2 / スキップ 1', output)
        self.assertEqual(self.saves, {'page0001.clip': 1, 'page0003.clip': 1})
        work = self.root/'work/job'
        self.assertEqual((work/'project/page0002.clip').read_bytes(), (work/'backups/page0002.clip').read_bytes())
        self.assertFalse((work/'pages/page0002.done').exists())
        self.assertEqual(read_json(work/'job.json')['long_edge'], 'auto')
        for source in self.images.glob('*.png'):
            with Image.open(source) as image:
                image.resize((image.width//4, image.height//4)).save(source)
        code, output = self.invoke(self.argv('--job', 'none', '--upscale', 'none'))
        self.assertEqual(code, 0, output)
        self.assertIn('画質が低下します', output)
        none_work = self.root/'work/none'
        for number in range(1, 4):
            plan = read_json(none_work/'pages'/f'page{number:04d}.plan.json')
            warnings = [w for w in plan['warnings'] if '画質が低下します' in w]
            self.assertEqual(len(warnings), 1)
            self.assertIn(f"{len(plan['panels'])}コマとも必要倍率", warnings[0])
            self.assertNotIn('AC_page', warnings[0])
            self.assertEqual(read_json(none_work/'pages'/f'page{number:04d}.done')['warnings'], plan['warnings'])
        self.assertIn('成功 3 / スキップ 0 / 警告あり 3ページ', output)
        self.opens.clear()
        code, output = self.invoke(['run', '--job', 'none'])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.opens, [])
        self.assertIn('画質が低下します', output)
        self.assertEqual(output.count('画質が低下します'), 3)

    def test_setup_prepare_shot_click_guard_and_sqlite_check(self):
        rng = np.random.default_rng(5)
        self.desktop.capture.return_value = Image.fromarray(rng.integers(0, 255, (420, 300, 3), dtype=np.uint8))
        code, output = self.invoke(['setup', 'prepare', '--cmc', str(self.cmc), '--recalibrate'])
        self.assertEqual(code, 0, output)
        profile = read_json(self.profile/'profile.json')
        practice = Path(profile['practice'])
        self.assertTrue(practice.is_relative_to(self.root/'work/v2-setup/project'))
        self.assertFalse(profile['verified'])
        self.assertEqual(self.invoke(['setup', 'shot', 'start'])[0], 0)
        self.assertEqual(self.invoke(['setup', 'crop', '--name', 'frame_tool', '--rect', '0', '0', '22', '14'])[0], 0)
        self.assertEqual(self.invoke(['setup', 'click', '20', '30'])[0], 0)
        self.desktop.click.assert_called_with((20, 30), modal=True)
        clicks = self.desktop.click.call_count
        self.assertEqual(self.invoke(['setup', 'click', '-1', '20'])[0], 1)
        self.assertEqual(self.desktop.click.call_count, clicks)
        self.desktop.identity.return_value = {'pid': 99, 'hwnd': 2, 'screen': [300, 420], 'dpi': 96}
        self.assertEqual(self.invoke(['setup', 'click', '20', '30'])[0], 1)
        self.assertEqual(self.desktop.click.call_count, clicks)
        self.desktop.identity.return_value['pid'] = 1
        self.assertEqual(self.invoke(['setup', 'check'])[0], 1)  # missing parts cannot verify
        for name in calibrate.PARTS:
            self.assertEqual(self.invoke(['setup', 'crop', '--name', name, '--rect', '0', '0', '22', '14'])[0], 0)
        self.desktop.capture.return_value = Image.new('RGB', (300, 420), 'white')
        code, output = self.invoke(['setup', 'check'])
        self.assertEqual(code, 0, output)
        self.assertTrue(read_json(self.profile/'profile.json')['verified'])
        self.assertTrue((self.root/'work/v2-setup/pages/page0001.done').is_file())
        # Recalibration keeps prior done evidence and uses a fresh copy/page number.
        self.assertEqual(self.invoke(['setup', 'prepare', '--cmc', str(self.cmc), '--recalibrate'])[0], 0)
        self.assertEqual(read_json(self.profile/'profile.json')['selftest_page'], 2)
        self.assertTrue((self.root/'work/v2-setup/pages/page0001.done').exists())

    def test_setup_argument_validation(self):
        invalid = [['setup'], ['setup', 'prepare'], ['setup', 'shot'], ['setup', 'click', '1'],
                   ['setup', 'click', 'x', '2'], ['setup', 'crop', '--name', 'frame_tool'],
                   ['setup', 'crop', '--name', 'unknown', '--rect', '0', '0', '10', '10'],
                   ['setup', 'check', '--rect', '0', '0', '10', '10'], ['status']]
        for argv in invalid:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(argv, self.root)
                self.assertEqual(error.exception.code, 2)
        self.desktop.press.assert_not_called()
        self.assertEqual(self.invoke(self.argv('--page', '0'))[0], 1)

    def test_environment_failure_stops_without_restore_or_minimize(self):
        def stop(rect):
            self.current.write_bytes(b'unsafe open working page')
            raise EnvironmentUnavailable('作業ページを確認できません。')
        self.desktop.drag.side_effect = stop
        code, output = self.invoke(self.argv('--frames-only'))
        self.assertEqual(code, 1)
        self.assertIn('作業ページを確認できません。', output)
        self.assertEqual(self.opens, ['page0001.clip'])
        self.desktop.minimize.assert_not_called()
        self.assertEqual((self.root/'work/job/project/page0001.clip').read_bytes(), b'unsafe open working page')
        summary = read_json(self.root/'output/job/summary.json')
        self.assertEqual(summary['pages'][0]['status'], '失敗')
        self.assertIn('作業ページを確認できません。', self.invoke(['status', '--job', 'job'])[1])

    def test_status_next_setup_command_without_calibration(self):
        self.invoke(self.argv('--dry-run'))
        (self.profile/'profile.json').unlink()
        self.assertIn('setup prepare --cmc', self.invoke(['status', '--job', 'job'])[1])
        write_json(self.profile/'profile.json', {'verified': False})
        self.assertIn('次の1コマンド：autoclip setup check', self.invoke(['status', '--job', 'job'])[1])

    def test_known_open_target_blocks_restore(self):
        self.invoke(self.argv('--dry-run'))
        target = self.root/'work/job/project/page0001.clip'
        target.write_bytes(b'open working file')
        self.desktop.title.return_value = 'page0001 - CLIP STUDIO PAINT'
        code, output = self.invoke(['run', '--job', 'job'])
        self.assertEqual(code, 1)
        self.assertIn('同名のタブ', output)
        self.assertEqual(target.read_bytes(), b'open working file')
        self.assertEqual(self.opens, [])

    def test_calibration_screen_drift_and_correlation_failure(self):
        directory = self.profile
        rng = np.random.default_rng(6)
        parts = {}
        for name in calibrate.PARTS:
            image = Image.fromarray(rng.integers(0, 255, (14, 22, 3), dtype=np.uint8))
            image.save(directory/f'{name}.png')
            image.save(directory/f'{name}.observed.png')
            parts[name] = {'image': f'{name}.png', 'observed': f'{name}.observed.png'}
        write_json(directory/'profile.json', {'verified': True, 'parts': parts,
                                             'session': self.desktop.identity()})
        self.desktop.identity.return_value['dpi'] = 144
        with self.assertRaises(EnvironmentUnavailable):
            UI(self.desktop, directory)
        self.desktop.identity.return_value['dpi'] = 96
        Image.new('RGB', (22, 14)).save(directory/'frame_tool.observed.png')
        plan = {'panels': [{}]}
        with self.assertRaises(EnvironmentUnavailable):
            calibrate.check(self.desktop, directory, plan, self.root/'work/v2-setup')
        self.assertFalse(read_json(directory/'profile.json')['verified'])


class AssetTests(unittest.TestCase):
    def test_axes_position_overscan_dpi_no_polygon_mask(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'source.png'
            image = Image.new('RGB', (100, 100), 'red')
            ImageDraw.Draw(image).rectangle((30, 20, 60, 50), fill='blue')
            image.save(source)
            plan = {'source': str(source), 'canvas_size': [400, 400], 'dpi': 350,
                    'panels': [{'source_polygon': [[30, 20], [60, 20], [55, 50], [35, 50]],
                                'canvas_polygon': [[100, 100], [160, 100], [150, 190], [110, 190]],
                                'scale': [2, 3], 'image_path': str(root/'asset.png')}]}
            self.assertEqual(prepare_assets(plan, 8), 20)
            with Image.open(root/'asset.png') as result:
                self.assertEqual(result.mode, 'RGBA')
                self.assertEqual(result.size, (400, 400))
                self.assertEqual(result.getbbox(), (80, 80, 180, 210))
                self.assertEqual(result.getpixel((120, 130)), (0, 0, 255, 255))
                self.assertEqual(result.getpixel((81, 81)), (255, 0, 0, 255))
                self.assertEqual(result.getpixel((0, 0))[3], 0)
                self.assertEqual(result.getpixel((158, 187))[3], 255)  # polygon exterior is unmasked
                self.assertAlmostEqual(result.info['dpi'][0], 350, delta=.02)

    def test_page_edge_padding_and_fractional_scale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new('RGB', (40, 50), 'green').save(root/'source.png')
            plan = {'source': str(root/'source.png'), 'canvas_size': [120, 140], 'dpi': 600,
                    'panels': [{'source_polygon': [[0, 0], [40, 0], [40, 50], [0, 50]],
                                'canvas_polygon': [[10, 10], [70, 10], [70, 110], [10, 110]],
                                'scale': [1.5, 2], 'image_path': str(root/'asset.png')}]}
            prepare_assets(plan, 2.2)
            with Image.open(root/'asset.png') as result:
                self.assertEqual(result.getbbox(), (0, 0, 85, 125))
                self.assertEqual(result.getpixel((0, 0)), (0, 128, 0, 255))
                self.assertEqual(result.getpixel((84, 124)), (0, 128, 0, 255))


if __name__ == '__main__':
    unittest.main()
