@echo off
chcp 65001 >nul
title Masar ERP Server - Waitress
color 0A
cd /d %~dp0

echo ===================================================
echo   Masar ERP Server - Starting Waitress...
echo ===================================================

findstr /i "DB_ENGINE=postgresql" .env >nul
if %errorlevel% equ 0 (
    echo   [DATABASE]: PostgreSQL (easysupermarket @ localhost:5432)
) else (
    echo   [DATABASE]: SQLite (db.sqlite3)
)
echo ===================================================

if exist "venv\Scripts\python.exe" goto ACTIVATE

echo [WARNING] Virtual environment 'venv' not found!
echo Calling setup_venv.bat to create environment and install packages...
echo.

if not exist "setup_venv.bat" goto NO_SETUP
call setup_venv.bat
if not exist "venv\Scripts\python.exe" goto NO_VENV

:ACTIVATE
rem Use the venv of THIS folder directly (activate.bat keeps the absolute path
rem the venv was created in, which breaks after renaming / moving the folder).
set "PY=%~dp0venv\Scripts\python.exe"
"%PY%" -c "import django" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] The venv in this folder is not usable - rebuilding it...
    if exist "setup_venv.bat" call setup_venv.bat
)

echo Checking required packages...
"%PY%" -c "import sys; from importlib.metadata import distributions; norm = lambda n: n.strip().lower().replace('_', '-'); installed = {norm(d.metadata['Name']): d.version for d in distributions() if d.metadata.get('Name')}; reqs = [l.strip() for l in open('requirements.txt', encoding='utf-8') if l.strip() and not l.strip().startswith('#')]; bad = [r for r in reqs if norm(r.split('==')[0]) not in installed or ('==' in r and installed.get(norm(r.split('==')[0])) != r.split('==')[1].strip())]; sys.exit(1 if bad else 0)" >nul 2>&1
if %errorlevel% neq 0 (
    echo Updating packages from requirements.txt...
    "%PY%" -m pip install --timeout 120 --retries 5 -r requirements.txt
)

if not exist ".env" (
    echo [FIRST RUN] No .env found - running first-time database setup...
    set "SETUP_NO_PAUSE=1"
    call setup_database.bat
    set "SETUP_NO_PAUSE="
)

echo Checking and applying database updates...
findstr /i "DB_ENGINE=postgresql" .env >nul
if %errorlevel% equ 0 (
    "%PY%" manage.py migrate_schemas --noinput
) else (
    "%PY%" manage.py migrate --noinput
)

echo Running automatic backup (if enabled in settings)...
"%PY%" manage.py auto_backup
if %errorlevel% neq 0 echo [WARNING] Automatic backup failed - the server will start anyway.

echo Building static files (CSS/JS)...
"%PY%" manage.py collectstatic --noinput >nul

echo Freeing port 8080 if previously occupied...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8080 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

echo ===================================================
echo   Masar ERP is LIVE!
echo   Browser URL: http://localhost:8080
echo   (Do NOT type 0.0.0.0 in browser)
echo ===================================================
echo Opening browser automatically...
start "" http://localhost:8080
echo Press Ctrl+C to stop the server.
echo.

"%PY%" -m waitress --listen=*:8080 --threads=8 config.wsgi:application
pause
exit /b 0

:NO_SETUP
echo [ERROR] setup_venv.bat not found!
pause
exit /b 1

:NO_VENV
echo [ERROR] Virtual environment 'venv' could not be activated!
pause
exit /b 1
