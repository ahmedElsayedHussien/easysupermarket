@echo off
chcp 65001 >nul
title Masar ERP - Setup Database ^& Superuser
color 0B
cd /d %~dp0

echo ===================================================
echo   Masar ERP - Database Setup ^& Superuser
echo ===================================================
echo.

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else (
    echo [ERROR] Virtual environment 'venv' not found! Run setup_venv.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [0/3] .env not found - creating it from .env.example with a new SECRET_KEY...
    python -c "import secrets,io; t=io.open('.env.example',encoding='utf-8').read(); io.open('.env','w',encoding='utf-8').write(t.replace('SECRET_KEY=change-me','SECRET_KEY='+secrets.token_urlsafe(50)))"
    echo       Done. Edit .env later if you use PostgreSQL ^(or run switch_database.bat^).
    echo.
)

REM NOTE: makemigrations is a developer step - migrations ship with the code.
echo [1/3] Applying migrations...
findstr /i "DB_ENGINE=postgresql" .env >nul
if %errorlevel% equ 0 (
    python manage.py migrate_schemas --noinput
) else (
    python manage.py migrate --noinput
)

echo.
echo [2/3] Setting up the chart of accounts...
python manage.py setup_chart_of_accounts

echo.
echo [3/3] Checking for a superuser...
python manage.py shell -c "from django.contrib.auth import get_user_model; import sys; sys.exit(0 if get_user_model().objects.filter(is_superuser=True).exists() else 3)"
if %errorlevel% equ 3 (
    echo No superuser found. Create one now ^(choose a strong password^):
    python manage.py createsuperuser
) else (
    echo A superuser already exists - existing passwords were NOT changed.
)

echo.
echo ===================================================
echo   Setup completed. Start the system with start_server.bat
echo ===================================================
echo.
pause
