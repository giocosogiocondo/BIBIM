@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
echo Validating databases in the configured output directory...
echo Original IFC files and databases are read-only.
echo Results are saved in a new entity_validation_DATE_TIME folder.
echo.
".venv\Scripts\python.exe" -X utf8 validate_all_entity_views.py %*
set "validation_exit=%ERRORLEVEL%"
echo.
if "%validation_exit%"=="0" echo Completed. Check the summary and unverified fields.
if not "%validation_exit%"=="0" echo Check the summary for differences, unsupported layouts, or errors.
pause
exit /b %validation_exit%
:missing
echo Python environment not found. Run run_gui.bat to set up this project first.
pause
exit /b 2
