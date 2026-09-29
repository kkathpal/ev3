@echo off
rem EV3 dashboard setup for Windows: double-click this file on each laptop.
rem Installs Python (if missing) and the packages the apps need, sets this laptop's
rem brick, and can add an "EV3 RC" shortcut to the desktop. Safe to run again.
setlocal
cd /d "%~dp0"
echo.
echo  === EV3 dashboard setup ===
echo.

rem ---- 1. Python 3.8 or newer --------------------------------------------------
call :find_python
if not defined PY (
    echo Python 3 isn't installed. Installing it with winget...
    where winget >nul 2>nul || goto :no_winget
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    call :find_python
)
if not defined PY (
    echo.
    echo Python was installed, but this window can't see it yet.
    echo Close this window and double-click install.bat again.
    goto :end
)
%PY% -c "import sys; print('Python', sys.version.split()[0], 'found.')"

rem ---- 2. Packages --------------------------------------------------------------
echo.
echo Installing the packages (paramiko)...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo Installing the packages failed. Check the internet connection and run install.bat again.
    goto :end
)
%PY% -c "import tkinter, paramiko" 2>nul
if errorlevel 1 (
    echo Python is missing tkinter, which the apps' windows need.
    echo Reinstall Python from https://www.python.org/downloads/ with "tcl/tk" ticked, then run this again.
    goto :end
)
echo Packages OK.

rem ---- 3. This laptop's brick ----------------------------------------------------
echo.
%PY% ev3_config.py 2>&1 | findstr /b /c:"This laptop drives"
set "BRICK="
set /p "BRICK=Brick name or IP for this laptop (e.g. ev3kishan), or Enter to keep it: "
if defined BRICK %PY% ev3_config.py "%BRICK%"

rem ---- 4. Desktop shortcut -------------------------------------------------------
echo.
set "SHORTCUT="
set /p "SHORTCUT=Add an "EV3 RC" shortcut to the desktop? [Y/n] "
if /i "%SHORTCUT%"=="n" goto :done
if /i "%SHORTCUT%"=="no" goto :done
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\EV3 RC.lnk'); $s.TargetPath = '%~dp0ev3_drive.pyw'; $s.WorkingDirectory = '%~dp0'; $s.Save()" && echo Shortcut added: "EV3 RC" on the desktop.

:done
echo.
echo  All set. Connect the brick, then start EV3 RC (desktop shortcut, or double-click ev3_drive.pyw).
echo  Phone controller: python ev3_phone.py    Status widget: ev3_widget.pyw
goto :end

:no_winget
echo winget isn't available on this PC. Install Python 3 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then double-click install.bat again.
goto :end

rem Sets PY to a working Python 3.8+ command, if there is one. The "python" that only opens
rem the Microsoft Store fails the version check, so it isn't used.
:find_python
set "PY="
set "CHECK=import sys; sys.exit(sys.version_info < (3, 8))"
py -3 -c "%CHECK%" >nul 2>nul && (set "PY=py -3" & exit /b 0)
python -c "%CHECK%" >nul 2>nul && (set "PY=python" & exit /b 0)
rem Just installed by winget: not on this window's PATH yet, but the launcher is here
set "LAUNCHER=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if exist "%LAUNCHER%" "%LAUNCHER%" -3 -c "%CHECK%" >nul 2>nul && set PY="%LAUNCHER%" -3
exit /b 0

:end
echo.
pause
