#!/bin/bash
# Invoice intake agent updater: opens the update page in your browser.
cd "$(dirname "$0")"
python3 updater.py || { echo; echo "Python 3 is needed - install it from https://www.python.org/downloads/"; read -r -p "Press Enter to close."; }
