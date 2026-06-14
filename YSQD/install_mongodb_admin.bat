@echo off
cd /d %~dp0
echo Stopping and removing old MongoDB service...
net stop MongoDB 2>nul
sc delete MongoDB 2>nul
ping -n 3 127.0.0.1 >nul

echo Installing MongoDB service from E:\MongoDB...
"E:\MongoDB\bin\mongod.exe" --config "E:\MongoDB\mongod.cfg" --install --serviceName MongoDB
ping -n 2 127.0.0.1 >nul

echo Starting MongoDB service...
net start MongoDB
ping -n 3 127.0.0.1 >nul

sc query MongoDB
echo.
echo MongoDB installation complete!
pause
