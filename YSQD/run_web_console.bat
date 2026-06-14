@echo off
cd /d %~dp0

set "REDIS_EXE=D:\phpstudy_pro\Extensions\redis3.0.504\redis-server.exe"
set "MONGO_EXE=E:\MongoDB\bin\mongod.exe"

echo === Checking MongoDB (port 27017) ===
netstat -ano | findstr ":27017 " | findstr "LISTENING" >nul 2>nul
if %errorlevel% neq 0 (
    echo MongoDB not running, attempting to start...
    net start MongoDB >nul 2>nul
    if errorlevel 1 (
        echo Starting MongoDB directly...
        start "" /b "%MONGO_EXE%" --dbpath "E:\MongoDB\data" --logpath "E:\MongoDB\log\mongod.log" --bind_ip 127.0.0.1 --port 27017 --logappend
    )
    echo Waiting for MongoDB...
    timeout /t 3 /nobreak >nul
) else (
    echo MongoDB already running
)

echo.
echo === Checking Redis (port 6379) ===
netstat -ano | findstr ":6379 " | findstr "LISTENING" >nul 2>nul
if %errorlevel% neq 0 (
    echo Starting Redis...
    start "" /b "%REDIS_EXE%"
    echo Waiting for Redis...
    timeout /t 3 /nobreak >nul
) else (
    echo Redis already running
)

echo.
echo === Killing old process on port 5003 ===
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5003 " ^| findstr "LISTENING"') do (
    echo Killing PID: %%a
    taskkill /f /pid %%a >nul 2>nul
)

echo.
echo === Starting web_console.py ===
where python >nul 2>nul
if %errorlevel% equ 0 (
    python web_console.py
) else (
    "D:\Python\Python3\python.exe" web_console.py
)

echo.
echo === Server stopped. Exit code: %errorlevel% ===
pause
