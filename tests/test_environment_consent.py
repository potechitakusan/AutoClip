"""Ensure routine commands never discover external environments implicitly."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autoclip import jobs
from autoclip.__main__ import build_parser


class EnvironmentConsentTests(unittest.TestCase):
    def test_default_scan_does_not_search_or_probe_external_environments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'input').mkdir()
            with patch.object(jobs, 'ROOT', root), patch.object(jobs, 'write_json'), \
                    patch.object(jobs.envscan, 'default_roots') as roots, \
                    patch.object(jobs.envscan, 'find') as find, \
                    patch.object(jobs.envscan, 'probe_python') as probe, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                report = jobs.scan()
                roots.assert_not_called()
                find.assert_not_called()
                probe.assert_not_called()
                self.assertTrue(report['environment']['search_skipped'])
                self.assertIn('request permission', output.getvalue())

    def test_search_path_requires_explicit_environment_discovery(self):
        with patch.object(jobs, 'environment') as environment:
            with self.assertRaisesRegex(ValueError, '--search requires --discover-env'):
                jobs.scan(['specified-folder'])
            environment.assert_not_called()

    def test_explicit_discovery_uses_requested_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'input').mkdir()
            env = {'builtin_venv': False, 'external_pythons': [], 'models': []}
            with patch.object(jobs, 'ROOT', root), patch.object(jobs, 'write_json'), \
                    patch.object(jobs, 'environment', return_value=env) as environment, \
                    contextlib.redirect_stdout(io.StringIO()):
                report = jobs.scan(['specified-folder'], discover_env=True)
                environment.assert_called_once_with(['specified-folder'])
                self.assertEqual(report['environment'], env)
        args = build_parser().parse_args(['scan', '--discover-env', '--search', 'specified-folder'])
        self.assertTrue(args.discover_env)
        self.assertEqual(args.search, ['specified-folder'])

    def test_configure_does_not_search_for_unspecified_model(self):
        with patch.object(jobs.envscan, 'find') as find, \
                patch.object(jobs.envscan, 'default_roots') as roots:
            with self.assertRaisesRegex(ValueError, '--model is required'):
                jobs.apply_upscale({'upscale': {}}, 'external', 'specified-python', None, [])
            find.assert_not_called()
            roots.assert_not_called()
