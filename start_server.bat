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

if exist "venv\Scripts\activate.bat" goto ACTIVATE

echo [WARNING] Virtual environment 'venv' not found!
echo Calling setup_venv.bat to create environment and install packages...
echo.

if not exist "setup_venv.bat" goto NO_SETUP
call setup_venv.bat
if not exist "venv\Scripts\activate.bat" goto NO_VENV

:ACTIVATE
call venv\Scripts\activate.bat

echo Checking and applying database updates...
findstr /i "DB_ENGINE=postgresql" .env >nul
if %errorlevel% equ 0 (
    python manage.py migrate_schemas --noinput
) else (
    python manage.py migrate --noinput
)

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

python -m waitress --listen=*:8080 --threads=8 config.wsgi:application
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
