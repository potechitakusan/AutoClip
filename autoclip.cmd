@echo off
rem AutoClip launcher: runs the CLI with the tools environment created by scripts\setup.ps1
"%~dp0.venv-tools\Scripts\python.exe" -B -m autoclip %*
