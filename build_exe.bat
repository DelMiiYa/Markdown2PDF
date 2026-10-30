@echo off
cd /d "%~dp0"
echo Building standalone Windows executable (dist\Markdown2PDF.exe)...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name Markdown2PDF --collect-submodules markdown app.py
) else (
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name Markdown2PDF --collect-submodules markdown app.py
)
if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build complete: dist\Markdown2PDF.exe
) else (
    echo.
    echo Build failed with exit code %ERRORLEVEL%.
)
