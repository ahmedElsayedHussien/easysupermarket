@echo off
chcp 65001 >nul
title Database Backup Tool - Masar ERP
color 0B
cd /d %~dp0

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    python manage.py db_backup
) else (
    echo [ERROR] Virtual environment 'venv' not found!
    echo Please run setup_venv.bat first.
)

echo.
pause
