"""Exercise setup selection with a fake uv; never install packages."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from autoclip.common import CODE_ROOT


@unittest.skipUnless(shutil.which('powershell'), 'Windows PowerShell required')
class SetupTests(unittest.TestCase):
    def run_setup(self, mode='', fail_tools=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'scripts').mkdir()
            shutil.copyfile(CODE_ROOT/'scripts/setup.ps1', root/'scripts/setup.ps1')
            harness = root/'harness.ps1'
            harness.write_text('''param([string]$Mode, [switch]$FailTools)
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
$tracePath = Join-Path $PSScriptRoot 'trace.jsonl'
function uv {
    Add-Content -LiteralPath $tracePath -Value (ConvertTo-Json -InputObject @($args) -Compress)
    if ($FailTools -and $args -contains 'requirements-tools.txt') {
        $global:LASTEXITCODE = 7
    } elseif ($args -contains 'torch==2.10.0') {
        throw 'TEST: torch install intercepted'
    } else {
        $global:LASTEXITCODE = 0
    }
}
$options = @{}
if ($Mode -eq 'builtin') { $options.BuiltinUpscale = $true }
if ($Mode -eq 'cpu') { $options.BuiltinUpscale = $true; $options.CpuOnly = $true }
if ($Mode -eq 'cpu-without-builtin') { $options.CpuOnly = $true }
& (Join-Path $PSScriptRoot 'scripts/setup.ps1') @options
''', encoding='utf-8')
            command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                       '-File', str(harness), '-Mode', mode]
            if fail_tools:
                command.append('-FailTools')
            result = subprocess.run(command, capture_output=True, text=True)
            trace = root/'trace.jsonl'
            calls = [json.loads(line) for line in trace.read_text(encoding='utf-8-sig').splitlines()] if trace.exists() else []
            self.assertFalse((root/'.venv').exists())
            return result, calls

    def test_default_installs_only_tools_and_guides_to_scan(self):
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            ['venv', '.venv-tools', '--python', '3.12'],
            ['pip', 'install', '--python', '.venv-tools/Scripts/python.exe', '-r', 'requirements-tools.txt']])
        self.assertIn('autoclip.cmd scan', result.stdout)

    def test_explicit_builtin_selects_torch_wheel(self):
        for mode, index in [('builtin', 'cu130'), ('cpu', 'cpu')]:
            with self.subTest(mode=mode):
                result, calls = self.run_setup(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('torch install intercepted', result.stderr)
                self.assertIn(['venv', '.venv', '--python', '3.12'], calls)
                self.assertEqual(calls[-1], ['pip', 'install', '--python', '.venv/Scripts/python.exe',
                                            'torch==2.10.0', '--index-url', f'https://download.pytorch.org/whl/{index}'])

    def test_cpu_requires_explicit_builtin_before_any_install(self):
        result, calls = self.run_setup('cpu-without-builtin')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Use -BuiltinUpscale -CpuOnly', result.stderr)
        self.assertEqual(calls, [])

    def test_tools_failure_stops_before_builtin_install(self):
        result, calls = self.run_setup('builtin', fail_tools=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uv failed (exit 7)', result.stderr)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
