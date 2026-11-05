@echo off
rem Windows launcher: starts the dashboard and opens it in the browser.
rem Close this window (or Ctrl-C) to stop. Flags pass through to server.py.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 server.py --open %*) else (python server.py --open %*)
if errorlevel 1 pause
