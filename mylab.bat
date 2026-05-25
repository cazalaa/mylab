@echo off
:: ─────────────────────────────────────────────────────────────
::  mylab.bat  —  Windows
::  Usage :
::    mylab.bat           → lancer l'application
::    mylab.bat --install → créer le venv et installer les dépendances
::    mylab.bat --clean   → supprimer les logs et les groupes sauvegardés
:: ─────────────────────────────────────────────────────────────

setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "VENV=%SCRIPT_DIR%.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

if "%~1"=="--install" goto :install
if "%~1"=="--clean"   goto :clean
if "%~1"==""          goto :run
echo Usage: mylab.bat [--install ^| --clean]
exit /b 1

:: ── --install ─────────────────────────────────────────────────
:install
echo.
echo ^[→^] Creation du virtualenv...
python -m venv "%VENV%"
if errorlevel 1 (
    echo ^[✗^] python introuvable. Installer Python 3.10+ et l'ajouter au PATH.
    exit /b 1
)
echo ^[✓^] Virtualenv cree : %VENV%

echo ^[→^] Installation des dependances...
"%PYTHON%" -m pip install --quiet --upgrade pip
"%PYTHON%" -m pip install --quiet -r "%SCRIPT_DIR%requirements.txt"
echo ^[✓^] Dependances installees

echo ^[→^] Installation de pywebview[winforms]...
"%PYTHON%" -m pip install --quiet "pywebview[winforms]"
echo ^[✓^] pywebview[winforms] installe

echo.
echo ^[✓^] Installation terminee. Lancer l'app avec : mylab.bat
goto :eof

:: ── --clean ───────────────────────────────────────────────────
:clean
echo.
set "LOG_DIR=%SCRIPT_DIR%logs"
set "GRP_DIR=%SCRIPT_DIR%groups"

:: logs
if exist "%LOG_DIR%\*.log" (
    del /q "%LOG_DIR%\*.log"
    echo ^[✓^] Logs supprimes ^(%LOG_DIR%^)
) else (
    echo ^[→^] Aucun log a supprimer
)

:: groupes
if exist "%GRP_DIR%\*.group" (
    set /p "CONFIRM=Supprimer les groupes sauvegardes ? (o/N) "
    if /i "!CONFIRM!"=="o" (
        del /q "%GRP_DIR%\*.group"
        echo ^[✓^] Groupes supprimes ^(%GRP_DIR%^)
    ) else (
        echo ^[→^] Groupes conserves
    )
) else (
    echo ^[→^] Aucun groupe a supprimer
)
echo.
goto :eof

:: ── run (défaut) ──────────────────────────────────────────────
:run
if not exist "%PYTHON%" (
    echo ^[✗^] Virtualenv introuvable. Lancer d'abord : mylab.bat --install
    exit /b 1
)
echo.
echo ^[→^] Demarrage de My Lab...
cd /d "%SCRIPT_DIR%"
"%PYTHON%" my_lab.py
