import unittest
from unittest.mock import Mock, patch

from PIL import Image

from autoclip import computer_profile as computer
from autoclip.__main__ import build_parser
from autoclip.common import ROOT


class ComputerProfileTests(unittest.TestCase):
    def points(self):
        return {key: [100, 100 if key != 'row2_name' else 140] for key in computer.REQUIRED}

    def test_cli_supports_prepare_and_finish(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(['profile', 'computer', '--step', 'prepare']).action, 'computer')
        self.assertEqual(parser.parse_args(['profile', 'computer', '--step', 'finish']).step, 'finish')
        self.assertEqual(parser.parse_args(['profile', 'agent']).step, None)

    def test_required_points_and_row_spacing(self):
        points = self.points()
        self.assertEqual(computer.validate_points(points, [1920, 1080]), 40)
        del points['new_frame_ok']
        with self.assertRaisesRegex(ValueError, 'new_frame_ok'):
            computer.validate_points(points, [1920, 1080])

    def test_wrong_coordinate_scale_or_other_window_is_rejected(self):
        points = self.points()
        points['gap_vertical'] = [2000, 100]
        with self.assertRaisesRegex(ValueError, 'outside screen'):
            computer.validate_points(points, [1920, 1080])
        points['gap_vertical'] = [100, 100]
        with self.assertRaisesRegex(ValueError, 'outside CLIP STUDIO'):
            computer.validate_points(points, [1920, 1080], [200, 0, 1920, 1080])

    def test_finish_cannot_mark_verified_without_native_verification(self):
        folder = ROOT/'work/computer-fixture/profile'
        config = {'canvas': {'pixel_size': [2508, 3541], 'basic_frame_mm': [1, 1, 100, 100]},
                  'mapping': {'frame_line_width_px': 5}}
        session = {'token': 'fixture', 'canvas': config['canvas'], 'line_px': 5,
                   'scratch': 'work/computer-fixture/profile/record-computer.clip',
                   'environment': {'screen_size': [1920, 1080], 'window_rect': [0, 0, 1920, 1080]}}
        observations = {'token': 'fixture', 'points': self.points(),
                        'blank_screenshot': str(folder/'blank.png'), 'split_screenshot': str(folder/'split.png'),
                        'split': {'verified': True}}
        desktop = Mock()
        desktop.environment.return_value = session['environment']
        desktop.capture.side_effect = lambda *args: Image.new('RGB', (1920, 1080), 'white')
        with patch.object(computer, 'work_directory', return_value=folder.parent), \
                patch.object(computer, 'read_json', side_effect=[session, observations]), \
                patch('pathlib.Path.is_file', return_value=True), \
                patch('PIL.Image.open', side_effect=lambda *args: Image.new('RGB', (1920, 1080), 'white')), \
                patch.object(computer, 'detect_canvas_rect', return_value=[400, 100, 1000, 950]), \
                patch.object(computer, 'refine_rect', return_value=[400, 100, 1000, 950]), \
                patch('autoclip.stages.focus_clip'), patch('autoclip.gui.Desktop', return_value=desktop), \
                patch('autoclip.split_frames.verify_base') as base, \
                patch.object(computer, 'profile_path', return_value=folder/'profile.json'), \
                patch.object(computer, 'write_json') as write, patch.object(computer.time, 'sleep'), \
                patch('autoclip.completion.run_calibration', side_effect=lambda c, op, key: op()), \
                patch.object(computer, 'verify', side_effect=ValueError('native verification failed')) as verify:
            with self.assertRaisesRegex(ValueError, 'native verification failed'):
                computer.main(config, 'finish')
            base.assert_called_once()
            verify.assert_called_once_with(config)
            self.assertFalse(write.call_args.args[1]['split']['verified'])
