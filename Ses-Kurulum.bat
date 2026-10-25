@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Sesli mesajlari yaziya cevirme ozelligi kuruluyor (bir kez yapilir, birkac dakika surebilir)...
echo.
python -m pip install --upgrade faster-whisper
if errorlevel 1 (
  echo.
  echo  KURULUM BASARISIZ. Internet baglantisini kontrol edip tekrar dene.
  pause
  exit /b 1
)
echo.
echo  Tamam! Simdi Kopru-Yeniden-Baslat.bat dosyasini calistir.
echo  Ilk sesli mesajda dil modeli indirilir (biraz bekletebilir), sonrakiler hizli olur.
echo.
pause
