@echo off
rem Windows one-time setup (writes %USERPROFILE%\.config\gpumonitor\config.json).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 setup.py %*) else (python setup.py %*)
pause
