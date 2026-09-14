@echo off
rem One-shot start on Windows: makes a venv on first run, installs deps, launches the app.
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
.venv\Scripts\python app.py
