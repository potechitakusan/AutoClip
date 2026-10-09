import argparse
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from PIL import Image, ImageDraw
from autoclip.detect import detect, is_rectangle
from autoclip.plan import make_plan
from autoclip.clip import inspect_project, copy_project, canvas_geometry
from autoclip.config import sha256, save_job, read_json
from autoclip.report import render_review, report
from tests.support import synthetic
from autoclip.__main__ import run


class M1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_synthetic_counts_and_order(self):
        for variant, count in enumerate((4, 3, 3, 3, 5)):
            source = synthetic.make_page(self.root/f'{variant}.png', variant=variant)
            result = detect(source)
            self.assertEqual(len(result['panels']), count)
            row = result['panels'][:2]
            if variant == 0:
                self.assertGreater(row[0]['outer_px'][0], row[1]['outer_px'][0])

    def test_transparent_and_blank(self):
        source = synthetic.make_transparent_page(self.root/'alpha.png')
        self.assertEqual(len(detect(source)['panels']), 4)
        self.assertIn('透明', detect(source)['warnings'][0])
        Image.new('RGBA', (300, 400)).save(self.root/'blank.png')
        result = detect(self.root/'blank.png')
        self.assertEqual(result['panels'][0]['outer_px'], [0, 0, 300, 400])
        self.assertTrue(any('1コマ' in w for w in result['warnings']))

    def test_diagonal_and_gaps(self):
        for variant, count in (('horizontal', 2), ('nested', 3), ('windmill', 4)):
            source = synthetic.make_diagonal_page(self.root/f'{variant}.png', variant=variant)
            result = detect(source)
            self.assertEqual(len(result['panels']), count)
            self.assertTrue(any(not is_rectangle(p['source_polygon']) for p in result['panels']))

    def test_non_quad_fallback(self):
        image = Image.new('RGB', (300, 400), 'white')
        ImageDraw.Draw(image).ellipse((30, 40, 270, 350), fill='black')
        image.save(self.root/'ellipse.png')
        result = detect(self.root/'ellipse.png')
        self.assertTrue(is_rectangle(result['panels'][0]['source_polygon']))
        self.assertTrue(result['warnings'])

    def test_project_copy_plan_report_and_immutability(self):
        cmc = synthetic.make_project(self.root/'input/project', pages=3)
        original = {p.name: sha256(p) for p in cmc.parent.iterdir()}
        work = self.root/'work/job'
        info = copy_project(cmc, work)
        source = synthetic.make_diagonal_page(self.root/'page.png')
        geometry = canvas_geometry(info['pages'][0]['path'])
        plan = make_plan(detect(source), geometry, 1, info['pages'][0]['path'], work/'assets')
        self.assertEqual(plan['panels'][0]['folder_name'], 'AC_page0001_p001')
        xs = [x for p in plan['panels'] for x, y in p['canvas_polygon']]
        self.assertAlmostEqual(min(xs), geometry['basic_frame_px'][0])
        self.assertAlmostEqual(max(xs), geometry['basic_frame_px'][2])
        output = self.root/'output'
        render_review(plan, output)
        job = {'name': 'job', 'work': str(work), 'project': info['cmc']}
        summary = report(job, [{'page_number': 1, 'panels': 2, 'status': '計画済み', 'warnings': ['<注意>']}], output)
        self.assertIn('成功 1', summary)
        self.assertIn('&lt;注意&gt;', (output/'index.html').read_text(encoding='utf-8'))
        self.assertTrue((output/'page0001.review.png').is_file())
        target = Path(info['pages'][0]['path'])
        target.write_bytes(b'changed working page')
        # No recopy or overwritten backup on subsequent runs.
        target.write_bytes((work/'backups'/target.name).read_bytes())
        copy_project(cmc, work)
        self.assertEqual(original, {p.name: sha256(p) for p in cmc.parent.iterdir()})

    def test_saved_config_reuse_and_path_rejection(self):
        args = argparse.Namespace(images=str(self.root), cmc=str(self.root/'x.cmc'), job='job',
                                  upscale=None, frames_only=None, review=None, python=None, model=None)
        first = save_job(args, self.root)
        args.images = args.cmc = None
        self.assertEqual(save_job(args, self.root), first)
        args.job = '../unsafe'
        with self.assertRaises(ValueError):
            save_job(args, self.root)

    def test_cli_continues_after_bad_image_or_page_metadata(self):
        cmc = synthetic.make_project(self.root/'input/project', pages=4)
        images = self.root/'input/images'
        synthetic.make_pages(images, 4)
        (images/'page-002.png').write_bytes(b'bad image')
        (cmc.parent/'page0003.clip').write_bytes(b'bad page metadata')
        args = argparse.Namespace(images=str(images), cmc=str(cmc), job='job',
                                  upscale=None, frames_only=None, review=None, python=None, model=None,
                                  dry_run=True, page=None)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run(args, self.root), 0)
        summary = read_json(self.root/'output/job/summary.json')
        self.assertEqual([r['status'] for r in summary['pages']], ['計画済み', '失敗', '失敗', '計画済み'])
        self.assertIn('成功 2 / スキップ 2', output.getvalue())
        self.assertTrue((self.root/'work/job/pages/page0004.plan.json').is_file())


if __name__ == '__main__':
    unittest.main()
