@echo off
rem ---------------------------------------------------------------------------
rem  StudioBook - start the server for other people on the same Wi-Fi / LAN.
rem  Opens the port on this machine; share the printed http://192.168.x.x:5000
rem  address with students and colleagues.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

set "STUDIOBOOK_DB=%~dp0studiobook.db"
set "STUDIOBOOK_HOST=0.0.0.0"
set "PORT=5000"
set "TMPDIR=D:\workbuddy\mv-env\tmp"
if not exist "%TMPDIR%" mkdir "%TMPDIR%"

echo.
echo   StudioBook - shared mode
echo   -----------------------
echo   On this PC   : http://127.0.0.1:5000/login
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo   Same Wi-Fi   : http:%%a:5000/login
echo.
echo   If others cannot open it, allow "python" through Windows Firewall
echo   (Private networks) when Windows asks.
echo   Keep this window open. Close it to stop the server.
echo.

start "" "http://127.0.0.1:5000/login"
".venv\Scripts\python.exe" -m studiobook.app

echo.
echo   Server stopped.
pause
