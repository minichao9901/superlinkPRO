@echo off
cd /d "%~dp0"
echo ETM page: http://127.0.0.1:8766
python -X utf8 server.py --port 8766
pause
