@echo off
chcp 65001 >nul
title Antigravity Telegram Köprüsü - Kurulum
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (python --version >nul 2>nul && set PY=python)
if not defined PY (
  echo Python bulunamadi, winget ile kuruluyor...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python kuruldu. Bu pencereyi KAPATIN ve Kurulum.bat dosyasina TEKRAR cift tiklayin.
  pause
  exit /b
)
echo Gerekli parca yukleniyor...
%PY% -m pip install --quiet --disable-pip-version-check websocket-client
%PY% bridge.py --kurulum
echo.
echo Pencere kapandi. Gerekirse Kopruyu-Baslat.bat ile tekrar acabilirsiniz.
pause
