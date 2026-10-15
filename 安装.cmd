@echo off
chcp 65001 >nul
set PYTHONDONTWRITEBYTECODE=1
set PYTHONIOENCODING=utf-8
"%~dp0vendor\python\python.exe" -B "%~dp0setup_wizard.py" %*
set "lingxi_exit=%errorlevel%"
echo.
pause
exit /b %lingxi_exit%
