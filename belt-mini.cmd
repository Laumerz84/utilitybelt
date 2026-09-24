@echo off
REM Launcher for the mini strip: a small UtilityBelt window in the top-right
REM corner of the main screen. Pin this to the taskbar next to belt.cmd.
REM Windows Terminal can keep it above other windows:
REM Ctrl+Shift+P > "Toggle always on top".
REM
REM The corner is worked out each time (screen width minus ~1120 px for 112
REM columns), so it follows a resolution change. "%~dp0." rather than "%~dp0":
REM a trailing backslash before the closing quote escapes the quote and wt
REM receives a mangled path.
set X=0
for /f %%x in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [Math]::Max(0, [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Right - 1120)"') do set X=%%x
start "" wt -w new --size 112,5 --pos %X%,0 --title "UtilityBelt Mini" --suppressApplicationTitle -d "%~dp0." python belt.py --mini
