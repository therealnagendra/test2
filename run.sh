#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install flask gunicorn
PORT=5001 python3 app.py
