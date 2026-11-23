@echo off
title Masar ERP - Setup Database ^& Superuser
color 0B
cd /d %~dp0

echo ===================================================
echo   Masar ERP - Database Setup ^& Superuser
echo ===================================================
echo.

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

REM NOTE: makemigrations is a developer step - migrations ship with the code.
echo [1/2] Applying migrations...
findstr /i "DB_ENGINE=postgresql" .env >nul
if %errorlevel% equ 0 (
    python manage.py migrate_schemas --noinput
) else (
    python manage.py migrate --noinput
)

echo.
echo [2/2] Checking for a superuser...
python manage.py shell -c "from django.contrib.auth import get_user_model; import sys; sys.exit(0 if get_user_model().objects.filter(is_superuser=True).exists() else 3)"
if %errorlevel% equ 3 (
    echo No superuser found. Create one now ^(choose a strong password^):
    python manage.py createsuperuser
) else (
    echo A superuser already exists - existing passwords were NOT changed.
)

echo.
echo ===================================================
echo   Setup completed.
echo ===================================================
echo.
pause
