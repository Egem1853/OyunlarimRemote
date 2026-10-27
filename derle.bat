@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Oyunlarim Remote - exe derleyici
echo ============================================
echo   Oyunlarim Remote  -  .exe derleyici
echo ============================================
echo.

where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python bulunamadi. Once python.org adresinden Python 3 kur
  echo ^(kurulumda "Add Python to PATH" kutusunu isaretle^), sonra tekrar calistir.
  echo.
  pause
  exit /b 1
)
%PY% --version
echo.

echo Eski ciktilar temizleniyor...
if exist "dist\OyunlarimRemote.exe" (
  attrib -r -s -h "dist\OyunlarimRemote.exe" >nul 2>nul
  del /f /q "dist\OyunlarimRemote.exe" >nul 2>nul
)
if exist "dist\OyunlarimRemote.exe" (
  echo.
  echo UYARI: dist\OyunlarimRemote.exe silinemedi - dosya kullaniliyor olabilir.
  echo  - Calisan Oyunlarim Remote varsa telefondan Ayarlar ^> Sunucuyu kapat ile durdur,
  echo  - Dosya Gezgini onizleme bolmesini kapat,
  echo  - ya da bu PC'yi yeniden baslatip tekrar dene.
  echo.
  pause
  exit /b 1
)

echo [1/2] Gerekli paketler kuruluyor...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements-build.txt
if errorlevel 1 (
  echo.
  echo Paketler kurulamadi. Internet baglantisini kontrol edip tekrar dene.
  pause
  exit /b 1
)

echo.
echo [2/2] Derleniyor... Bu birkac dakika surebilir, lutfen bekle.
%PY% -m PyInstaller --noconfirm --clean OyunlarimRemote.spec
if errorlevel 1 (
  echo.
  echo HATA: Derleme basarisiz oldu. Yukaridaki mesaja bak.
  echo Not: Windows Defender/antivirus exe'yi kilitliyor olabilir;
  echo bu klasore gecici bir istisna ekleyip tekrar deneyebilirsin.
  pause
  exit /b 1
)

if exist "dist\OyunlarimRemote.exe" (
  echo ============================================
  echo   TAMAM! Dosya hazir:
  echo   dist\OyunlarimRemote.exe
  echo ============================================
  explorer dist
) else (
  echo HATA: exe uretilemedi. Yukaridaki mesajlari kontrol et.
)
echo.
pause
