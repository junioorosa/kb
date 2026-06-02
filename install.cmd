@echo off
REM KB installer -- double-click this after cloning.
REM Installs/updates KB (hooks + daily sync) and opens the manager in your browser.
REM Extra args pass through to install.ps1 (e.g. -NoManager, -Time 02:30).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" -Apply %*
echo.
pause
