@echo off
cd /d "%~dp0"
python -m pip install flask gunicorn
set PORT=5000
start "" http://localhost:5000
python app.py
pause
