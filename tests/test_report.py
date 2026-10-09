import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
from autoclip.config import read_json
from autoclip.detect import detect
from autoclip.plan import make_plan
from autoclip.report import report
from autoclip.upscale import quality_warnings
from autoclip.warnings import compact_warnings


class WarningTests(unittest.TestCase):
    def plan(self, scales):
        return {'warnings': [], 'panels': [
            {'folder_name': f'AC_page0001_p{i:03d}',
             'source_polygon': [[0, 0], [100, 0], [100, 100], [0, 100]],
             'canvas_polygon': [[0, 0], [100*s, 0], [100*s, 100*s], [0, 100*s]]}
            for i, s in enumerate(scales, 1)]}

    def test_quality_all_panels_one_warning_and_idempotent(self):
        plan = self.plan([3.72]*4)
        quality_warnings(plan, {'upscale': 'none'})
        expected = ['画質が低下します：4コマとも必要倍率3.72倍'
                    '（拡大すると改善します：--upscale builtin/external）']
        self.assertEqual(plan['warnings'], expected)
        quality_warnings(plan, {'upscale': 'none'})
        self.assertEqual(plan['warnings'], expected)

    def test_quality_range_and_partial_panel_names(self):
        for scales, scope in (([3.5, 3.7], '2コマとも'),
                              ([1, 3.5, 1.25, 3.7], '2コマ（p002・p004）')):
            plan = self.plan(scales)
            quality_warnings(plan, {})
            self.assertEqual(plan['warnings'], [f'画質が低下します：{scope}必要倍率3.5〜3.7倍'
                             '（拡大すると改善します：--upscale builtin/external）'])

    def test_no_quality_warning_for_upscale_frames_only_or_threshold(self):
        for policy, scales in (({'upscale': 'builtin'}, [4]),
                               ({'upscale': 'external'}, [4]),
                               ({'frames_only': True}, [4]), ({}, [1, 1.25])):
            plan = self.plan(scales)
            plan['warnings'] = ['注意', '注意', '別の注意']
            quality_warnings(plan, policy)
            self.assertEqual(plan['warnings'], ['注意（2件）', '別の注意'])

    def test_repeated_detection_and_plan_warnings_keep_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'ellipses.png'
            image = Image.new('RGB', (400, 400), 'white')
            draw = ImageDraw.Draw(image)
            for x in (20, 220):
                draw.ellipse((x, 20, x+150, 180), fill='black')
            image.save(source)
            detection = detect(source)
            text = '四角形でない輪郭を外接矩形に置き換えました。'
            self.assertEqual(detection['warnings'], [text+'（2件）'])
            geometry = {'basic_frame_px': [0, 0, 400, 400], 'size_px': [400, 400],
                        'dpi': 350, 'warnings': [text]}
            plan = make_plan(detection, geometry, 1, 'page.clip', temp, 'stretch')
            self.assertEqual(plan['warnings'], [text+'（3件）'])
            self.assertEqual(compact_warnings(plan['warnings']), plan['warnings'])

    def test_report_counts_html_duplicates_rounding_and_failure_line(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = {'name': 'job', 'work': str(root/'job'), 'project': str(root/'job/project/book.cmc')}
            results = [
                {'page_number': 1, 'panels': 4, 'status': '成功', 'warnings': ['<注意>']*4},
                {'page_number': 2, 'panels': 2, 'status': '完了済み', 'rounding_error_px': 4.03},
                {'page_number': 3, 'panels': 1, 'status': '計画済み', 'rounding_error_px': 4},
                {'page_number': 4, 'panels': 0, 'status': '失敗', 'reason': '画像を読めません。'}]
            summary = report(job, results, root/'report')
            self.assertEqual(summary.splitlines()[0], '成功 3 / スキップ 1 / 警告あり 2ページ')
            self.assertIn('警告：<注意>（4件）', summary)
            rounding = '枠の位置誤差は最大およそ4.0px（画面倍率のため）'
            self.assertEqual(summary.count(rounding), 1)
            self.assertNotIn('丸め誤差推定', summary)
            self.assertEqual(summary.splitlines()[4],
                             'ページ4：0コマ・失敗 / 画像を読めません。 / autoclip run --job job --page 4')
            content = (root/'report/index.html').read_text(encoding='utf-8')
            self.assertEqual(content.count('&lt;注意&gt;（4件）'), 1)
            self.assertEqual(content.count(rounding), 1)
            self.assertIn('警告：なし', content)
            self.assertEqual(read_json(root/'report/summary.json')['pages'][0]['warnings'], ['<注意>（4件）'])
            self.assertEqual(results[0]['warnings'], ['<注意>']*4)  # caller remains reusable

    def test_old_records_and_partial_report_resume_are_compacted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = {'name': 'job', 'work': str(root/'job'), 'project': str(root/'book.cmc')}
            old = [f'AC_page0001_p{i:03d}：必要倍率 3.72倍、画質が低下します。' for i in range(1, 5)]
            expected = '画質が低下します：4コマとも必要倍率3.72倍（拡大すると改善します：--upscale builtin/external）'
            summary = report(job, [{'page_number': 1, 'panels': 4, 'status': '完了済み',
                                    'warnings': old}], root/'report')
            self.assertEqual(summary.count(expected), 1)
            self.assertNotIn('AC_page', summary)
            summary = report(job, [{'page_number': 2, 'panels': 1, 'status': '成功'}], root/'report')
            self.assertEqual(summary.splitlines()[0], '成功 2 / スキップ 0 / 警告あり 1ページ')
            saved = read_json(root/'report/summary.json')['pages']
            self.assertEqual(saved[0]['warnings'], [expected])
            self.assertEqual((root/'report/index.html').read_text(encoding='utf-8').count(expected), 1)
            self.assertEqual(compact_warnings([old[1], old[3]], 4), [
                '画質が低下します：2コマ（p002・p004）必要倍率3.72倍'
                '（拡大すると改善します：--upscale builtin/external）'])


if __name__ == '__main__':
    unittest.main()
