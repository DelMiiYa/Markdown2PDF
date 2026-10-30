@echo off
cd /d "%~dp0"
echo Closing any running Markdown2PDF.exe instances...
taskkill /F /IM Markdown2PDF.exe >nul 2>&1
echo Building standalone Windows executable (dist\Markdown2PDF.exe)...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import app, PyInstaller.__main__; PyInstaller.__main__.run(['--noconfirm', '--clean', '--onefile', '--windowed', '--name', 'Markdown2PDF', '--collect-submodules', 'markdown', 'app.py'])"
) else (
    python -c "import app, PyInstaller.__main__; PyInstaller.__main__.run(['--noconfirm', '--clean', '--onefile', '--windowed', '--name', 'Markdown2PDF', '--collect-submodules', 'markdown', 'app.py'])"
)
if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build complete: dist\Markdown2PDF.exe
) else (
    echo.
    echo Build failed with exit code %ERRORLEVEL%.
)
