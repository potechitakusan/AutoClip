@echo off
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%~dp0.venv-tools\Scripts\python.exe" -B -m autoclip %*
exit /b %errorlevel%
