@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found: .venv\Scripts\python.exe
    pause
    exit /b 1
)

if "%~1"=="--cli" (
    ".venv\Scripts\python.exe" "tools\cantools_quick.py"
    pause
    exit /b 0
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "tools\cantools_gui.py"
) else (
    ".venv\Scripts\python.exe" "tools\cantools_gui.py"
)
