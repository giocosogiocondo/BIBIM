@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Create .venv and install requirements.txt first. See README.md.
    pause
    exit /b 1
)
if not exist "configs\pipeline.local.json" (
    echo Copy configs\pipeline.example.json to configs\pipeline.local.json and edit the paths.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -u -B -X utf8 text_to_sql.py run %*
set "task_exit=%ERRORLEVEL%"
pause
exit /b %task_exit%
