@echo off
cd /d %~dp0
call venv\Scripts\activate
python milo.py %*
pause
 