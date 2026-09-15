@echo off
rem Invoice intake agent updater: opens the update page in your browser.
cd /d "%~dp0"
python updater.py
if errorlevel 1 (
  echo.
  echo Python 3 is needed. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
)
