Write-Output "Stopping and removing old MongoDB service..."
Stop-Service MongoDB -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
sc.exe delete MongoDB
Start-Sleep -Seconds 2

Write-Output "Installing MongoDB service from E:\MongoDB..."
E:\MongoDB\bin\mongod.exe --config "E:\MongoDB\mongod.cfg" --install --serviceName MongoDB

Start-Sleep -Seconds 2
Write-Output "Starting MongoDB service..."
Start-Service MongoDB
Start-Sleep -Seconds 3

Get-Service MongoDB | Format-Table Name, Status, StartType
Write-Output "Done!"
Read-Host "Press Enter to exit"
