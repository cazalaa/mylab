@echo off
:: ─────────────────────────────────────────────────────────────
::  mylab.bat  —  Windows
::  Usage :
::    mylab.bat           -> lancer l'application
::    mylab.bat --install -> creer/reparer le venv et installer les dependances
::    mylab.bat --clean   -> supprimer les logs et les groupes sauvegardes
:: ─────────────────────────────────────────────────────────────

setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "VENV=%SCRIPT_DIR%.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

if "%~1"=="--install" goto :install
if "%~1"=="--clean"   goto :clean
if "%~1"=="--traces"  goto :run_traces
if "%~1"==""          goto :run
echo Usage: mylab.bat [--install ^| --clean ^| --traces]
exit /b 1

:: ── --install ─────────────────────────────────────────────────
:install
echo.
if exist "%VENV%" (
    echo [--^>] Suppression du virtualenv existant pour repartir proprement...
    rmdir /s /q "%VENV%"
)

echo [--^>] Creation du virtualenv...
py -3.11 -m venv "%VENV%" 2>nul
if errorlevel 1 (
    py -3 -m venv "%VENV%" 2>nul
    if errorlevel 1 (
        python -m venv "%VENV%"
        if errorlevel 1 (
            echo [X] Python introuvable. Installer Python 3.10+ ou 3.11+ et l'ajouter au PATH.
            exit /b 1
        )
    )
)
echo [OK] Virtualenv cree : %VENV%

echo [--^>] Mise a jour de pip/setuptools/wheel...
"%PYTHON%" -m pip install --quiet --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1

echo [--^>] Installation des dependances Python...
"%PYTHON%" -m pip install --quiet --upgrade -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 exit /b 1
echo [OK] Dependances installees

echo [--^>] Installation/reparation du backend Windows pour pywebview...
"%PYTHON%" -m pip install --quiet --upgrade --force-reinstall "pywebview[winforms]"
if errorlevel 1 (
    echo [!] pywebview[winforms] a echoue. Tentative avec pythonnet...
    "%PYTHON%" -m pip install --quiet --upgrade pywebview pythonnet
    if errorlevel 1 exit /b 1
)
echo [OK] Backend Windows pret

echo.
echo [OK] Installation terminee. Lancer l'app avec : mylab.bat
goto :eof

:: ── --clean ───────────────────────────────────────────────────
:clean
echo.
set "LOG_DIR=%SCRIPT_DIR%logs"
set "GRP_DIR=%SCRIPT_DIR%groups"

for /d /r "%SCRIPT_DIR%" %%D in (__pycache__) do if exist "%%D" rmdir /s /q "%%D"
del /s /q "%SCRIPT_DIR%*.pyc" "%SCRIPT_DIR%*.pyo" "%SCRIPT_DIR%.DS_Store" >nul 2>nul
echo [OK] Caches Python supprimes

if exist "%LOG_DIR%\*.log" (
    del /q "%LOG_DIR%\*.log"
    echo [OK] Logs supprimes ^(%LOG_DIR%^)
) else (
    echo [--^>] Aucun log a supprimer
)

if exist "%GRP_DIR%\*.group" (
    set /p "CONFIRM=Supprimer les groupes sauvegardes ? (o/N) "
    if /i "!CONFIRM!"=="o" (
        del /q "%GRP_DIR%\*.group"
        echo [OK] Groupes supprimes ^(%GRP_DIR%^)
    ) else (
        echo [--^>] Groupes conserves
    )
) else (
    echo [--^>] Aucun groupe a supprimer
)
echo.
goto :eof

:: ── run (defaut) ──────────────────────────────────────────────
:run
if not exist "%PYTHON%" (
    echo [X] Virtualenv introuvable. Lancer d'abord : mylab.bat --install
    exit /b 1
)
echo.
echo [--^>] Demarrage de My Lab...
cd /d "%SCRIPT_DIR%"
"%PYTHON%" my_lab.py
goto :eof

:run_traces
if not exist "%PYTHON%" (
    echo [X] Virtualenv introuvable. Lancer d'abord : mylab.bat --install
    exit /b 1
)
echo.
echo [--^>] Demarrage de My Lab ^(traces actives^)...
cd /d "%SCRIPT_DIR%"
"%PYTHON%" my_lab.py --traces
