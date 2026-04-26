@echo off
title Shelly Manager
color 0A

echo.
echo  ==========================================
echo   Shelly Network Manager
echo  ==========================================
echo.

:: Pruefen ob Python (py Launcher) vorhanden ist
py --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo  [FEHLER] Python wurde nicht gefunden!
    echo.
    echo  Bitte Python installieren:
    echo  https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: Abhaengigkeiten installieren falls noetig
echo  [1/2] Pruefe Abhaengigkeiten...
py -m pip install flask requests zeroconf --quiet --disable-pip-version-check
if %errorlevel% neq 0 (
    color 0E
    echo.
    echo  [WARNUNG] Einige Pakete konnten nicht installiert werden.
    echo.
    timeout /t 2 /nobreak >nul
    color 0A
)

:: Browser nach 2 Sekunden oeffnen (im Hintergrund, waehrend Server startet)
echo  [2/2] Starte Server...
echo.
echo  Browser oeffnet sich automatisch unter http://localhost:5000
echo  Dieses Fenster offen lassen - Schliessen beendet den Server.
echo.
echo  ==========================================
echo.
start /b cmd /c "ping -n 3 127.0.0.1 >nul && start http://localhost:5000"

:: Server starten (blockiert bis Strg+C oder Fenster schliessen)
py "%~dp0shelly_manager.py"

:: Server wurde beendet
echo.
color 0C
echo  Server wurde beendet.
pause
