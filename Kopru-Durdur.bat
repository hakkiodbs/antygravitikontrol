@echo off
chcp 65001 >nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bridge.py*' -and $_.Name -match 'python|pyw' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Kopru durduruldu.
rem
