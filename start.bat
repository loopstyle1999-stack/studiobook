@echo off
rem ---------------------------------------------------------------------------
rem  StudioBook - start the local server
rem  Admin password is read from admin-password.txt (same folder as this file).
rem  To change it, edit that file and restart.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

set "STUDIOBOOK_DB=%~dp0studiobook.db"
set "PORT=5000"
set "TMPDIR=D:\workbuddy\mv-env\tmp"
if not exist "%TMPDIR%" mkdir "%TMPDIR%"

echo.
echo   StudioBook starting...
echo     Sign-in page : http://127.0.0.1:5000/login
echo     Student tab  : http://127.0.0.1:5000/login?role=student
echo     Teacher tab  : http://127.0.0.1:5000/login?role=teacher
echo.
echo   Keep this window open. Close it to stop the server.
echo.

start "" "http://127.0.0.1:5000/login"
".venv\Scripts\python.exe" -m studiobook.app

echo.
echo   Server stopped.
pause
