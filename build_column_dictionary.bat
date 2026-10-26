@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -B -X utf8 tools\build_column_description_catalog.py %*
set "task_exit=%ERRORLEVEL%"
pause
exit /b %task_exit%
