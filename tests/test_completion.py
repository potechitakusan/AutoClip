from __future__ import annotations

import shutil
import unittest
import uuid
from unittest.mock import Mock, patch

from autoclip.common import ROOT, read_json
from autoclip.completion import run_calibration
from autoclip.window_handoff import Handoff, Windows, choose_codex_window


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.path = (ROOT/'work/test-runs'/uuid.uuid4().hex).resolve()
        self.path.mkdir(parents=True)
        self.config = {'work_dir':str(self.path),'project':'work/fixture/project.clip'}

    def tearDown(self):
        self.assertTrue(self.path.is_relative_to((ROOT/'work/test-runs').resolve()))
        shutil.rmtree(self.path)

    def windows(self):
        return [dict(hwnd=10,pid=100,exe='CLIPStudioPaint.exe',title='CLIP STUDIO PAINT'),
                dict(hwnd=20,pid=200,exe='Code.exe',title='Preview file - AutoClip - Visual Studio Code'),
                dict(hwnd=30,pid=300,exe='Code.exe',title='OtherProject - Visual Studio Code')]

    def api(self):
        api = Mock()
        api.windows.return_value = self.windows()
        api.foreground.return_value = 10
        api.identity.return_value = self.windows()[1]
        api.minimize.return_value = True
        api.activate.return_value = True
        return api

    def test_only_project_editor_or_codex_is_selected(self):
        self.assertEqual(choose_codex_window(self.windows(),10,'AutoClip')['hwnd'],20)
        self.assertIsNone(choose_codex_window(self.windows()[2:],30,'AutoClip'))
        false_match = dict(hwnd=40,pid=400,exe='Code.exe',title='AutoClip - OtherProject - Visual Studio Code')
        # Explicit workspace segment is preferred to a mere substring match.
        false_match['title'] = 'AutoClip-notes.txt - OtherProject - Visual Studio Code'
        self.assertIsNone(choose_codex_window([false_match],40,'AutoClip'))

    def test_ambiguous_windows_are_not_guessed(self):
        other = dict(self.windows()[1],hwnd=21)
        self.assertIsNone(choose_codex_window(self.windows()+[other],10,'AutoClip'))
        self.assertEqual(choose_codex_window(self.windows()+[other],21,'AutoClip')['hwnd'],21)

    def test_minimize_only_clip_then_activate_expected_window(self):
        api = self.api();handoff = Handoff('AutoClip',api)
        self.assertEqual(handoff.finish()['codex_foreground'],True)
        api.minimize.assert_called_once_with(10)
        api.activate.assert_called_once_with(20)

    def test_modal_or_recycled_handle_stops_handoff(self):
        api = self.api();handoff = Handoff('AutoClip',api)
        api.minimize.return_value = False
        self.assertFalse(handoff.finish()['clip_minimized']);api.activate.assert_not_called()
        api.minimize.return_value = True
        api.identity.return_value = dict(self.windows()[1],pid=999)
        self.assertFalse(handoff.finish()['codex_foreground']);api.activate.assert_not_called()

    def test_success_handoff_runs_after_operation_and_generates_html(self):
        order = []
        def operation():
            order.append('verified')
            return self.path/'seed.json'
        with patch('autoclip.completion.Handoff') as handoff,patch('autoclip.completion.notify',return_value=True),patch('builtins.print'):
            handoff.return_value.finish.side_effect = lambda: (order.append('handoff') or {'clip_minimized':True,'codex_foreground':True})
            self.assertEqual(run_calibration(self.config,operation),self.path/'seed.json')
        self.assertEqual(order,['verified','handoff'])
        status = read_json(self.path/'calibration-status.json')
        self.assertEqual(status['state'],'complete')
        self.assertTrue(status['handoff']['codex_foreground'])
        self.assertIn('基本枠の作成とページ配布が完了しました',(self.path/'calibration-status.html').read_text(encoding='utf-8'))

    def test_failed_calibration_keeps_desktop_visible_and_escapes_error(self):
        def fail():
            raise ValueError('<script>fixture</script>')
        with patch('autoclip.completion.Handoff') as handoff,patch('autoclip.completion.notify') as notify,patch('builtins.print'):
            with self.assertRaisesRegex(ValueError,'fixture'):
                run_calibration(self.config,fail)
            handoff.return_value.finish.assert_not_called();notify.assert_not_called()
        self.assertEqual(read_json(self.path/'calibration-status.json')['state'],'interrupted')
        html = (self.path/'calibration-status.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)

    def test_foreground_denied_is_not_misreported_as_calibration_failure(self):
        with patch('autoclip.completion.Handoff') as handoff,patch('autoclip.completion.notify',return_value=False),patch('builtins.print'):
            handoff.return_value.finish.return_value = {'clip_minimized':True,'codex_foreground':False}
            run_calibration(self.config,lambda:self.path/'seed.json')
        status = read_json(self.path/'calibration-status.json')
        self.assertEqual(status['state'],'complete')
        self.assertFalse(status['handoff']['codex_foreground'])
        self.assertIn('画面の復帰を確認できません',status['return_message'])

    def test_temporary_input_attachments_are_released_on_focus_error(self):
        api = object.__new__(Windows)
        api.u,api.k = Mock(),Mock()
        api.u.IsIconic.return_value = False
        api.foreground = Mock(return_value=10)
        api.k.GetCurrentThreadId.return_value = 1
        api.u.GetWindowThreadProcessId.side_effect = [2,3]
        api.u.AttachThreadInput.return_value = True
        api.u.BringWindowToTop.side_effect = OSError('focus fixture')
        with patch('autoclip.window_handoff.time.sleep'),self.assertRaisesRegex(OSError,'focus fixture'):
            api.activate(20)
        calls = [c.args for c in api.u.AttachThreadInput.call_args_list]
        self.assertEqual(calls,[(1,2,True),(1,3,True),(1,3,False),(1,2,False)])


if __name__ == '__main__':
    unittest.main()
