@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Database Switcher ^& Diagnostics - Masar ERP

if exist "venv\Scripts\python.exe" (
    set "PY=%~dp0venv\Scripts\python.exe"
    "%PY%" switch_db.py
) else (
    echo [ERROR] Virtual environment 'venv' not found!
    echo Please run setup_venv.bat first.
    pause
)
