"""正式版移行のモデル取得・推論・独立runnerをモックで検証する。"""
import contextlib
import hashlib
import importlib.util
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from autoclip import fetch_model
from autoclip.config import ROOT, read_json, sha256
from autoclip.__main__ import main


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def builtin(self):
        python = self.root/'.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_bytes(b'fixture')
        return python

    def test_package_root_and_standalone_runner_dispatch(self):
        self.assertEqual(ROOT, Path(__file__).resolve().parents[1])
        entry = Mock()
        bytecode = sys.dont_write_bytecode
        original = sys.path[:]
        try:
            with patch.dict(sys.modules, {'autoclip.inference': SimpleNamespace(main=entry)}):
                runpy.run_path(str(ROOT/'scripts/upscale_runner.py'), run_name='__main__')
            entry.assert_called_once_with()
            self.assertEqual(sys.path[0], str(ROOT))
            self.assertTrue(sys.dont_write_bytecode)
        finally:
            sys.path[:] = original
            sys.dont_write_bytecode = bytecode

    def test_fetch_model_cli_dispatch_without_desktop(self):
        with patch('autoclip.fetch_model.fetch', return_value='saved fixture') as fetch, \
             patch('autoclip.__main__.Desktop') as desktop, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['fetch-model', '--dest', 'models-fixture'], self.root), 0)
        fetch.assert_called_once_with('models-fixture')
        desktop.assert_not_called()

    def test_fetch_rejects_hash_before_conversion(self):
        self.builtin()
        with patch.object(fetch_model, 'ROOT', self.root), \
             patch.object(fetch_model.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad weights')), \
             patch.object(fetch_model.subprocess, 'run') as convert:
            with self.assertRaisesRegex(ValueError, 'pinned'):
                fetch_model.fetch()
        convert.assert_not_called()
        self.assertEqual(list((self.root/'models').iterdir()), [])

    def test_fetch_verified_weights_only_conversion_and_cleanup(self):
        python = self.builtin()
        weights = b'official weights fixture'
        def convert(command, **kwargs):
            self.assertEqual(command[:3], [str(python), '-B', '-c'])
            self.assertIn('weights_only=True', command[3])
            self.assertEqual(Path(command[4]).read_bytes(), weights)
            Path(command[5]).write_bytes(b'plain tensor fixture')
            return SimpleNamespace(returncode=0)
        with patch.object(fetch_model, 'ROOT', self.root), \
             patch.object(fetch_model, 'PTH_SHA256', hashlib.sha256(weights).hexdigest()), \
             patch.object(fetch_model.urllib.request, 'urlopen', return_value=io.BytesIO(weights)), \
             patch.object(fetch_model.subprocess, 'run', side_effect=convert):
            self.assertIn('saved', fetch_model.fetch())
        self.assertFalse((self.root/'models/RealESRGAN_x4plus.pth.download').exists())
        with patch.object(fetch_model, 'ROOT', self.root), \
             patch.object(fetch_model.urllib.request, 'urlopen') as download:
            self.assertIn('already present', fetch_model.fetch())
            download.assert_not_called()

    def test_fetch_missing_builtin_and_failed_conversion(self):
        with patch.object(fetch_model, 'ROOT', self.root), \
             patch.object(fetch_model.urllib.request, 'urlopen') as download:
            with self.assertRaisesRegex(ValueError, 'environment missing'):
                fetch_model.fetch()
            download.assert_not_called()
        self.builtin()
        weights = b'fixture'
        with patch.object(fetch_model, 'ROOT', self.root), \
             patch.object(fetch_model, 'PTH_SHA256', hashlib.sha256(weights).hexdigest()), \
             patch.object(fetch_model.urllib.request, 'urlopen', return_value=io.BytesIO(weights)), \
             patch.object(fetch_model.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='failure')):
            with self.assertRaisesRegex(ValueError, 'Conversion failed'):
                fetch_model.fetch()
        self.assertFalse((self.root/'models/RealESRGAN_x4plus.pth.download').exists())

    def inference(self):
        torch = Mock(__version__='fixture')
        torch.inference_mode.return_value = lambda method: method
        class OutOfMemoryError(Exception):
            pass
        torch.cuda.OutOfMemoryError = OutOfMemoryError
        functional = Mock()
        modules = {'torch': torch, 'torch.nn': SimpleNamespace(functional=functional),
                   'safetensors': Mock(), 'safetensors.torch': Mock(),
                   'autoclip.rrdb': SimpleNamespace(RRDBNet=Mock())}
        spec = importlib.util.spec_from_file_location('autoclip._test_inference', ROOT/'autoclip/inference.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
        return module, torch, functional

    def test_inference_area_resize_cache_and_changed_input(self):
        module, torch, functional = self.inference()
        source, target = self.root/'source.png', self.root/'result.png'
        Image.new('RGB', (10, 20), 'green').save(source)
        runner = object.__new__(module.Upscaler)
        runner.settings = {'model_sha256': 'fixture', 'long_edge': 32}
        runner.tile, runner.pad, runner.precision, runner.device = 256, 32, 'fp32', 'cpu'
        runner.infer = Mock(return_value=Mock())
        pixels = np.full((32, 16, 3), .5, dtype=np.float32)
        functional.interpolate.return_value.__getitem__ = Mock(return_value=Mock())
        functional.interpolate.return_value[0].permute.return_value.numpy.return_value = pixels
        with patch.object(module, 'audit') as audit, contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(source, target)
            functional.interpolate.assert_called_once_with(runner.infer.return_value, size=(32, 16), mode='area')
            with Image.open(target) as output:
                self.assertEqual(output.size, (16, 32))
                self.assertEqual(output.getpixel((0, 0)), (128, 128, 128))
            self.assertEqual(result['output_sha256'], sha256(target))
            self.assertEqual(read_json(target.with_suffix('.upscale.json'))['job_key'], result['job_key'])
            runner.run(source, target)
            self.assertEqual(runner.infer.call_count, 1)
            Image.new('RGB', (10, 20), 'red').save(source)
            runner.run(source, target)
            self.assertEqual(runner.infer.call_count, 2)
            self.assertEqual(audit.call_count, 2)

    def test_inference_cuda_oom_tile_retry_and_input_guards(self):
        module, torch, functional = self.inference()
        source, target = self.root/'source.png', self.root/'result.png'
        Image.new('RGB', (10, 20)).save(source)
        runner = object.__new__(module.Upscaler)
        runner.settings = {'model_sha256': 'fixture', 'long_edge': 32}
        runner.tile, runner.pad, runner.precision, runner.device = 256, 32, 'fp32', 'cuda'
        runner.infer = Mock(side_effect=[torch.cuda.OutOfMemoryError(), torch.cuda.OutOfMemoryError(), RuntimeError('stop')])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                runner.run(source, target)
        self.assertEqual([c.args[1] for c in runner.infer.call_args_list], [256, 128, 64])
        self.assertEqual(torch.cuda.empty_cache.call_count, 2)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, 'must differ'):
            runner.run(source, source)
        with self.assertRaisesRegex(ValueError, '16..16384'):
            runner.run(source, target, 16385)
        Image.new('RGBA', (10, 20)).save(source)
        with self.assertRaisesRegex(ValueError, 'opaque'):
            runner.run(source, target)
