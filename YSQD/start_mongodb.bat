@echo off
echo Starting MongoDB from E:\MongoDB...
start /B "" "E:\MongoDB\bin\mongod.exe" --dbpath "E:\MongoDB\data" --logpath "E:\MongoDB\log\mongod.log" --bind_ip 127.0.0.1 --port 27017 --logappend
echo MongoDB started on port 27017
echo Log: E:\MongoDB\log\mongod.log
pause
