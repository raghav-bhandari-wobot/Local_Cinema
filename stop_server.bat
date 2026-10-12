@echo off
rem Stops the background Local Cinema server.
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*movie_server.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; 'Local Cinema stopped.' } else { 'Local Cinema is not running.' }"
ping -n 4 127.0.0.1 >nul
