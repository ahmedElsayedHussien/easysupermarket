@echo off
chcp 65001 >nul
title EasySupermarket - Cleanup Duplicate Products
cls

echo ======================================================================
echo          EasySupermarket ERP - Duplicate Products Cleaner
echo ======================================================================
echo.
echo This tool will safely merge all duplicate products (by Name or Barcode),
echo re-link all invoices, FIFO inventory batches, and stock movements,
echo and create an automatic safety backup before making any changes.
echo.

set /p CONFIRM="Do you want to proceed with duplicate cleanup? (Y/N): "
if /i not "%CONFIRM%"=="Y" (
    echo [INFO] Operation cancelled by user.
    pause
    exit /b 0
)

echo.
echo [1/2] Running duplicate cleanup...
.\venv\Scripts\python.exe scripts\cleanup_duplicates.py --sqlite

echo.
echo ======================================================================
echo [2/2] Done! Press any key to exit.
echo ======================================================================
pause
