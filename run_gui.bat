@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto verify
echo Creating Python environment...
py -3.12 -m venv .venv
if not errorlevel 1 goto verify
py -3.13 -m venv .venv
if not errorlevel 1 goto verify
python -m venv .venv
if errorlevel 1 goto failure
:verify
".venv\Scripts\python.exe" -c "import sys,tkinter; assert sys.version_info >= (3,12)"
if errorlevel 1 goto failure
".venv\Scripts\python.exe" -c "import ifcopenshell; assert ifcopenshell.version == '0.8.5'; assert ifcopenshell.schema_by_name('IFC4X3_ADD2').name() == 'IFC4X3_ADD2'"
if not errorlevel 1 goto launch
echo Installing IFC dependencies. Internet access is required on first run...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failure
:launch
".venv\Scripts\python.exe" bibim_gui.py
if errorlevel 1 goto failure
exit /b 0
:failure
echo.
echo Failed. Install Python 3.12 or newer with Tcl/Tk and check the messages above.
pause
exit /b 1
