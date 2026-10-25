@echo off
chcp 65001 >nul
title Antigravity - Köprülü Başlat
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (python --version >nul 2>nul && set PY=python)
if not defined PY (
  echo Python bulunamadi. Once Kurulum.bat dosyasini calistirin.
  pause
  exit /b
)
%PY% bridge.py --baslat
pause
