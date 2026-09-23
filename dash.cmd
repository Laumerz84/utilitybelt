@echo off
REM Launcher for the dashboard - a .py cannot be pinned to the taskbar, a .cmd can.
REM Keeps the window open afterwards so you can read it; without the pause it
REM would flash and vanish the moment the script finished.
title UtilityBelt
cd /d "%~dp0"
python "%~dp0dash.py" %*
echo.
pause
