Write-Host "Starting Peer 1001 in new terminal..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python main.py 1001"

Write-Host "Starting Peer 1003 in new terminal..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python main.py 1003"

Write-Host "Both peers started."