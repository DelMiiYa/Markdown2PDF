@echo off
cd /d "%~dp0"
start "" pythonw -X utf8 app.py %*
