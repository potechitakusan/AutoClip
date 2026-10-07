"""Calibration focus recovery, using mocks without real desktop input."""
import unittest
from unittest.mock import Mock, patch

from autoclip import agent_profile as agent


class AgentFocusTests(unittest.TestCase):
    def setUp(self):
        self.session = {'environment': {'screen_size': [1920, 1080]},
                        'scratch': 'work/fixture/record-agent.clip', 'phase': 'layers'}
        self.desktop = Mock(hwnd=10, pid=100)
        self.desktop.environment.return_value = self.session['environment']
        self.desktop.u.GetForegroundWindow.return_value = 20
        self.desktop.u.GetLastActivePopup.return_value = 11
        self.desktop.process.side_effect = lambda hwnd: 100 if hwnd in (10, 11) else 200
        self.desktop.title.return_value = 'CLIP STUDIO PAINT'
        self.api = Mock()
        self.windows = patch('autoclip.window_handoff.Windows', return_value=self.api)
        self.windows.start()
        self.addCleanup(self.windows.stop)
        self.sleep = patch.object(agent.time, 'sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def activated(self, hwnd):
        self.assertEqual(hwnd, 11)
        self.desktop.u.GetForegroundWindow.return_value = 11
        self.desktop.title.return_value = 'record-agent.clip - CLIP STUDIO PAINT'
        return True

    def test_activation_precedes_scratch_title_check(self):
        def title(hwnd):
            self.assertEqual(self.desktop.u.GetForegroundWindow(), 11)
            return 'record-agent.clip - CLIP STUDIO PAINT'
        self.desktop.title.side_effect = title
        self.api.activate.side_effect = self.activated
        self.assertTrue(agent.restore_scratch(self.desktop, self.session))
        self.api.activate.assert_called_once_with(11)
        self.desktop.press.assert_not_called()
        self.desktop.click.assert_not_called()

    def test_transient_activation_failure_is_retried(self):
        count = 0
        def activate(hwnd):
            nonlocal count
            count += 1
            return False if count == 1 else self.activated(hwnd)
        self.api.activate.side_effect = activate
        agent.restore_scratch(self.desktop, self.session)
        self.assertEqual(self.api.activate.call_count, 2)
        self.desktop.click.assert_not_called()

    def test_delayed_title_is_retried_after_focus_is_restored(self):
        self.api.activate.side_effect = self.activated
        self.desktop.title.side_effect = ['CLIP STUDIO PAINT', 'record-agent.clip - CLIP STUDIO PAINT']
        agent.restore_scratch(self.desktop, self.session)
        self.api.activate.assert_called_once()
        self.assertEqual(self.desktop.title.call_count, 2)

    def test_persistent_failure_has_resume_instruction_and_no_input(self):
        self.api.activate.return_value = False
        before = dict(self.session)
        with self.assertRaisesRegex(ValueError, 'profile agent --step capture'):
            agent.restore_scratch(self.desktop, self.session)
        self.assertEqual(self.api.activate.call_count, 5)
        self.assertEqual(self.session, before)
        self.desktop.title.assert_not_called()
        self.desktop.press.assert_not_called()
        self.desktop.click.assert_not_called()

    def test_focus_taken_between_title_and_guard_is_retried(self):
        self.api.activate.side_effect = self.activated
        calls = 0
        def guard(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.desktop.u.GetForegroundWindow.return_value = 20
                raise ValueError('CLIP STUDIO lost foreground')
        self.desktop.guard.side_effect = guard
        agent.restore_scratch(self.desktop, self.session)
        self.assertEqual(self.api.activate.call_count, 2)
        self.desktop.press.assert_not_called()

    def test_wrong_document_is_never_operated_on(self):
        self.desktop.u.GetForegroundWindow.return_value = 10
        self.desktop.title.return_value = 'other-work.clip - CLIP STUDIO PAINT'
        with self.assertRaisesRegex(ValueError, 'CALIBRATION_FOCUS_RETRY'):
            agent.restore_scratch(self.desktop, self.session)
        self.api.activate.assert_not_called()
        self.desktop.press.assert_not_called()
        self.desktop.click.assert_not_called()

    def test_generic_title_can_use_same_process_live_tab_proof(self):
        from PIL import Image
        image = Image.new('RGB', (80, 20), 'white')
        self.desktop.u.GetForegroundWindow.return_value = 10
        self.desktop.capture.return_value = image
        self.session['document_tab'] = {'hwnd': 10, 'pid': 100, 'rect': [0, 0, 80, 20],
                                       'pixels': [[[255, 255, 255]] * 80] * 20}
        agent.restore_scratch(self.desktop, self.session)
        self.desktop.click.assert_not_called()

    def test_changed_tab_or_process_rejects_generic_title(self):
        from PIL import Image
        self.desktop.u.GetForegroundWindow.return_value = 10
        self.desktop.capture.return_value = Image.new('RGB', (80, 20), 'black')
        self.session['document_tab'] = {'hwnd': 10, 'pid': 100, 'rect': [0, 0, 80, 20],
                                       'pixels': [[[255, 255, 255]] * 80] * 20}
        with self.assertRaisesRegex(ValueError, 'CALIBRATION_FOCUS_RETRY'):
            agent.restore_scratch(self.desktop, self.session)
        self.session['document_tab']['pid'] = 999
        self.assertFalse(agent.matches_document_tab(self.desktop, self.session))
        self.desktop.click.assert_not_called()

    def test_explicit_other_document_cannot_use_tab_fallback(self):
        self.desktop.u.GetForegroundWindow.return_value = 10
        self.desktop.title.return_value = 'other.clip - CLIP STUDIO PAINT'
        with patch.object(agent, 'matches_document_tab', return_value=True) as match:
            with self.assertRaisesRegex(ValueError, 'CALIBRATION_FOCUS_RETRY'):
                agent.restore_scratch(self.desktop, self.session)
            match.assert_not_called()

    def test_foreign_popup_is_rejected(self):
        self.desktop.u.GetLastActivePopup.return_value = 30
        with self.assertRaisesRegex(ValueError, 'different process'):
            agent.restore_scratch(self.desktop, self.session)
        self.api.activate.assert_not_called()

    def test_environment_change_is_not_retried(self):
        self.desktop.environment.return_value = {'screen_size': [1280, 720]}
        with self.assertRaisesRegex(ValueError, 'Screen/window/DPI changed'):
            agent.restore_scratch(self.desktop, self.session)
        self.api.activate.assert_not_called()

    def test_menu_is_reopened_after_ownership_and_modal_check(self):
        self.session['phase'] = 'import-menu'
        self.api.activate.side_effect = self.activated
        self.desktop.u.IsWindowEnabled.return_value = True
        events = []
        self.desktop.title.side_effect = lambda hwnd: (events.append('title') or 'record-agent.clip')
        self.desktop.u.IsWindowEnabled.side_effect = lambda hwnd: (events.append('modal-check') or True)
        self.desktop.press.side_effect = lambda *keys: events.append(keys)
        with patch('autoclip.gui.Desktop', return_value=self.desktop):
            agent.owned_desktop({}, self.session)
        self.assertLess(events.index('title'), events.index(('ESC',)))
        self.assertLess(events.index('modal-check'), events.index(('ESC',)))

    def test_unexpected_modal_prevents_menu_keys(self):
        self.session['phase'] = 'import-menu'
        self.api.activate.side_effect = self.activated
        self.desktop.u.IsWindowEnabled.return_value = False
        with patch('autoclip.gui.Desktop', return_value=self.desktop):
            with self.assertRaisesRegex(ValueError, 'Unexpected modal dialog'):
                agent.owned_desktop({}, self.session)
        self.desktop.press.assert_not_called()

    def test_owned_modeless_frame_dialog_is_supported(self):
        self.session['phase'] = 'frame-dialog'
        self.api.activate.side_effect = self.activated
        self.desktop.title.side_effect = lambda hwnd=None: ('record-agent.clip' if hwnd == 10 else '新規コマ枠フォルダー')
        self.desktop.u.IsWindowEnabled.return_value = True
        with patch('autoclip.gui.Desktop', return_value=self.desktop):
            agent.owned_desktop({}, self.session)
        self.desktop.click.assert_not_called()


class TabRegistrationTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image, ImageDraw
        self.image = Image.new('RGB', (1920, 1080), 'white')
        ImageDraw.Draw(self.image).text((5, 45), 'record-agent.clip', fill='black')
        self.session = {'screenshot_sha256': 'current', 'screenshot': 'work/fixture/screen.png',
                        'environment': {'window_rect': [0, 0, 1920, 1080]},
                        'document_tab': {'existing': True}}
        self.desktop = Mock(hwnd=10, pid=100)

    def test_current_tab_is_committed_after_live_verification(self):
        events = []
        with patch.object(agent, 'sha256', return_value='current'), \
                patch('PIL.Image.open', return_value=self.image), \
                patch('autoclip.gui.Desktop', return_value=self.desktop), \
                patch.object(agent, 'restore_scratch') as restore, \
                patch.object(agent, 'session_path'), patch.object(agent, 'write_json') as write:
            def verify(desktop, candidate):
                self.assertEqual(self.session['document_tab'], {'existing': True})
                self.assertEqual(candidate['document_tab']['pid'], 100)
                events.append('verify')
            restore.side_effect = verify
            write.side_effect = lambda *args: events.append('write')
            agent.register_document_tab({}, self.session, [0, 40, 180, 70], 'current')
            self.assertEqual(events, ['verify', 'write'])
            self.assertEqual(self.session['document_tab']['hwnd'], 10)
            self.desktop.click.assert_not_called()

    def test_failed_live_verification_preserves_existing_proof(self):
        with patch.object(agent, 'sha256', return_value='current'), \
                patch('PIL.Image.open', return_value=self.image), \
                patch('autoclip.gui.Desktop', return_value=self.desktop), \
                patch.object(agent, 'restore_scratch', side_effect=ValueError('mismatched tab')), \
                patch.object(agent, 'write_json') as write:
            with self.assertRaisesRegex(ValueError, 'mismatched tab'):
                agent.register_document_tab({}, self.session, [0, 40, 180, 70], 'current')
            self.assertEqual(self.session['document_tab'], {'existing': True})
            write.assert_not_called()

    def test_stale_token_or_changed_file_is_rejected_before_capture(self):
        for token, actual in [('stale', 'current'), ('current', 'changed')]:
            with self.subTest(token=token, actual=actual), \
                    patch.object(agent, 'sha256', return_value=actual), \
                    patch('autoclip.gui.Desktop') as desktop:
                with self.assertRaisesRegex(ValueError, 'Stale screenshot'):
                    agent.register_document_tab({}, self.session, [0, 40, 180, 70], token)
                desktop.assert_not_called()

    def test_invalid_tab_region_is_rejected(self):
        with patch.object(agent, 'sha256', return_value='current'), patch('PIL.Image.open') as image:
            with self.assertRaisesRegex(ValueError, 'small active document tab'):
                agent.register_document_tab({}, self.session, [0, 400, 180, 430], 'current')
            image.assert_not_called()

