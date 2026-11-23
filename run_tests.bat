@echo off
REM Runs the automated test suite on an in-memory SQLite DB (never touches PostgreSQL).
cd /d "%~dp0"
venv\Scripts\python.exe manage.py test --settings=config.settings.test %*
