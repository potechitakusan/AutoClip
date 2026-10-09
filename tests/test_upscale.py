import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image
from autoclip.upscale import (prepare_assets, required_magnification, target_long_edge,
                         output_size, runner_command, UpscaleFailed)
from autoclip.__main__ import review_result


class UpscaleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.python = self.root/'external/python.exe'
        self.python.parent.mkdir()
        self.python.write_bytes(b'python fixture')
        self.model = self.root/'external/model.safetensors'
        self.model.write_bytes(b'model fixture')
        builtin = self.root/'.venv/Scripts/python.exe'
        builtin.parent.mkdir(parents=True)
        builtin.write_bytes(b'builtin fixture')
        self.policy = {'upscale': 'external', 'python': str(self.python), 'model': str(self.model)}

    def plan(self, sx=2, sy=3):
        source = self.root/'source.png'
        Image.new('RGB', (40, 50), 'green').save(source)
        return {'source': str(source), 'canvas_size': [200, 240], 'dpi': 350,
                'panels': [{'source_polygon': [[0, 0], [40, 0], [40, 50], [0, 50]],
                            'canvas_polygon': [[30, 40], [30+40*sx, 40],
                                               [30+40*sx, 40+50*sy], [30, 40+50*sy]],
                            'scale': [sx, sy], 'image_path': str(self.root/'assets/panel.png')}]}

    def infer(self, command, **kwargs):
        self.assertEqual(kwargs['cwd'], str(self.root))
        source = Path(command[command.index('--input')+1])
        target = Path(command[command.index('--output')+1])
        edge = int(command[command.index('--long-edge')+1])
        with Image.open(source) as im:
            self.assertEqual(im.mode, 'RGB')
            im.resize(output_size(im.size, edge)).save(target)
        return SimpleNamespace(returncode=0, stdout='mock inference', stderr='')

    def test_auto_threshold_native_limit_and_anisotropic_scale(self):
        panel = self.plan()['panels'][0]
        self.assertEqual(required_magnification(panel), 3)
        self.assertIsNone(target_long_edge((100, 200), 1.25, self.policy))
        self.assertEqual(target_long_edge((100, 200), 1.251, self.policy), 252)
        self.assertEqual(target_long_edge((100, 200), 7, self.policy), 800)
        self.assertEqual(target_long_edge((8000, 10000), 3, self.policy), 16384)
        self.assertIsNone(target_long_edge((100, 200), 8, {'upscale': 'none'}))

    def test_external_and_builtin_commands_explicit_paths_only(self):
        command = runner_command(self.policy, 'in.png', 'out.png', 100, self.root)
        self.assertEqual(command[:4], [str(self.python), '-B', '-s', str(self.root/'scripts/upscale_runner.py')])
        self.assertIn(str(self.model), command)
        command = runner_command({**self.policy, 'upscale': 'builtin'}, 'in.png', 'out.png', 100, self.root)
        self.assertEqual(command[:4], [str(self.root/'.venv/Scripts/python.exe'), '-B', '-s', str(self.root/'scripts/upscale_runner.py')])
        with self.assertRaises(UpscaleFailed):
            runner_command({**self.policy, 'python': str(self.root/'missing.exe')}, 'in', 'out', 100, self.root)

    def test_both_modes_prepare_transparent_canvas_with_dpi_and_overscan(self):
        original_external = {p: p.read_bytes() for p in self.python.parent.iterdir()}
        for mode in ('builtin', 'external'):
            plan = self.plan(1.5, 2.2)
            with patch('autoclip.upscale.subprocess.run', side_effect=self.infer) as runner:
                self.assertEqual(prepare_assets(plan, 2.2, {**self.policy, 'upscale': mode}, self.root), 15)
                runner.assert_called_once()
            with Image.open(plan['panels'][0]['image_path']) as image:
                self.assertEqual(image.size, (200, 240))
                self.assertEqual(image.mode, 'RGBA')
                self.assertEqual(image.getbbox(), (15, 25, 105, 165))
                self.assertEqual(image.getpixel((15, 25)), (0, 128, 0, 255))
                self.assertAlmostEqual(image.info['dpi'][0], 350, delta=.02)
        self.assertEqual(original_external, {p: p.read_bytes() for p in self.python.parent.iterdir()})

    def test_low_scale_and_none_do_not_launch_or_validate_model(self):
        for policy, scales in ((self.policy, (1.25, 1)), ({'upscale': 'none'}, (3, 2))):
            self.python.unlink(missing_ok=True)
            self.model.unlink(missing_ok=True)
            plan = self.plan(*scales)
            with patch('autoclip.upscale.subprocess.run') as runner:
                prepare_assets(plan, policy=policy, root=self.root)
            runner.assert_not_called()
            self.assertFalse((self.root/'assets/crops').exists())

    def test_failure_exit_missing_output_wrong_size(self):
        for result in (SimpleNamespace(returncode=1, stdout='', stderr='failure'),
                       SimpleNamespace(returncode=0, stdout='', stderr='')):
            with patch('autoclip.upscale.subprocess.run', return_value=result):
                with self.assertRaises(UpscaleFailed):
                    prepare_assets(self.plan(), policy=self.policy, root=self.root)
        def wrong(command, **kwargs):
            target = command[command.index('--output')+1]
            Image.new('RGB', (10, 10)).save(target)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch('autoclip.upscale.subprocess.run', side_effect=wrong):
            with self.assertRaisesRegex(UpscaleFailed, '寸法'):
                prepare_assets(self.plan(), policy=self.policy, root=self.root)

    def test_review_opens_html_then_yes_no_default_no_messagebox(self):
        import ctypes
        events = []
        dialog = Mock()
        def answer(*args):
            events.append('dialog')
            self.assertEqual(args[3], 0x4 | 0x100 | 0x40000)
            return 6
        dialog.user32.MessageBoxW.side_effect = answer
        with patch('webbrowser.open', side_effect=lambda uri: events.append(uri)), \
             patch.object(ctypes, 'windll', dialog):
            self.assertTrue(review_result(self.root))
            dialog.user32.MessageBoxW.side_effect = None
            dialog.user32.MessageBoxW.return_value = 7
            self.assertFalse(review_result(self.root))
        self.assertEqual(events[:2], [(self.root/'index.html').as_uri(), 'dialog'])
