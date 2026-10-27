# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: tek dosyalık, penceresiz Windows exe üretir.
# Derleme:  pyinstaller OyunlarimRemote.spec
from PyInstaller.utils.hooks import collect_submodules

hidden = []
# Ses aktarımı için (varsa gömülür; yoksa derleme yine başarılı olur, o özellik kapalı kalır)
try:
    hidden += collect_submodules("pyaudiowpatch")
except Exception:
    pass

a = Analysis(
    ["steam_remote.py"],
    pathex=[],
    binaries=[],
    datas=[("steam_remote_assets", "steam_remote_assets")],  # varsayılan arka planlar gömülür
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OyunlarimRemote",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX kapalı: antivirüs yanlış-alarmını azaltır
    runtime_tmpdir=None,
    console=False,          # penceresiz: çift tıkla, arka planda çalışır
    icon="icon.ico" if __import__("os").path.exists("icon.ico") else None,
    version="file_version_info.txt" if __import__("os").path.exists("file_version_info.txt") else None,
)
