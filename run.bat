@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"%~dp0..\MuseAI-API\.venv\Scripts\python.exe" -m autovideo.server %*
