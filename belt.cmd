@echo off
REM Launcher for the interactive dashboard - a .py cannot be pinned to the
REM taskbar, a .cmd can. No pause at the end: belt.py owns the screen while it
REM runs and restores the terminal on exit, so a pause would just leave a dead
REM prompt sitting under a cleared screen.
title UtilityBelt
cd /d "%~dp0"
python "%~dp0belt.py" %*
