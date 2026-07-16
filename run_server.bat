@echo off
echo Starting EasyStore Server...
call venv\Scripts\activate.bat
python run_waitress.py
pause
