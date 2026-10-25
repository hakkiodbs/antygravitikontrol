@echo off
chcp 65001 >nul
cd /d "%~dp0"
call Kopru-Durdur.bat
wscript "%~dp0Kopru-Gizli-Baslat.vbs"
echo Kopru yeniden baslatildi (gizli).
timeout /t 3 >nul
