@echo off
rem Build the Windows executables natively (no Wine).
rem Requires Python 3 for Windows (python.org) on PATH or the "py" launcher.
rem
rem Result:
rem   dist\windows_x64_gnsf.exe      - CLI (console)
rem   dist\windows_x64_gnsf-GUI.exe  - GUI (no console)

setlocal
cd /d "%~dp0.."

set "PYTHON=python"
where py >nul 2>nul && set "PYTHON=py -3"

set "VENV=.venv_windows_build"

echo Creating build environment (%VENV%)...
if exist "%VENV%" rmdir /s /q "%VENV%"
%PYTHON% -m venv "%VENV%" || goto :fail
"%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller || goto :fail

set PYI="%VENV%\Scripts\pyinstaller.exe" --onefile --clean --noconfirm --icon AppIcons\app_icon.ico --add-data "AppIcons;AppIcons"

echo Building CLI version...
%PYI% --console --name windows_x64_gnsf gnsf.py || goto :fail

echo Building GUI version...
%PYI% --windowed --name windows_x64_gnsf-GUI gnsf-GUI.py || goto :fail

rmdir /s /q build
del /q *.spec
rmdir /s /q "%VENV%"

echo.
echo Done:
echo   dist\windows_x64_gnsf.exe
echo   dist\windows_x64_gnsf-GUI.exe
endlocal
exit /b 0

:fail
echo.
echo Build failed.
endlocal
exit /b 1
