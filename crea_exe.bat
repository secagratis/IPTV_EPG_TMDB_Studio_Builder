@echo off
title Compilazione IPTV-EPG-TMDB Studio (.exe)
echo ========================================================
echo   Installazione librerie e creazione eseguibile .EXE
echo ========================================================
echo.
python -m pip install --upgrade pip
pip install PyQt6 requests pyinstaller
echo.
echo Avvio compilazione con PyInstaller...
pyinstaller --noconfirm --onefile --windowed --name "IPTV_EPG_TMDB_Studio" iptv_epg_auto_tmdb.py
echo.
echo ========================================================
echo   COMPLETATO! Trovi il file nella cartella "dist"
echo ========================================================
pause
