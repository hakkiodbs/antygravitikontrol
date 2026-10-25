@echo off
chcp 65001 >nul
title Kisisel verileri sil (paylasmadan once)
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (python --version >nul 2>nul && set PY=python)
if not defined PY (
  echo Python bulunamadi. Once Kurulum.bat dosyasini calistir.
  pause
  exit /b
)
%PY% bridge.py --temizle
echo.
pause
