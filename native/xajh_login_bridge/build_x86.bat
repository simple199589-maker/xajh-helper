@echo off
rem Build standalone login-stage bridge (x86) — independent of production bridge.
rem @author by ak
setlocal
set VCVARS="C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat"
if not exist %VCVARS% (
  echo vcvarsall.bat not found
  exit /b 1
)
call %VCVARS% x86
if errorlevel 1 exit /b 1

set ROOT=%~dp0..\..
set OUT=%ROOT%\native\bin
if defined XAJH_NATIVE_BIN_DIR set OUT=%XAJH_NATIVE_BIN_DIR%
set BUILD=%ROOT%\build\native_login
if not exist "%OUT%" mkdir "%OUT%"
if not exist "%BUILD%" mkdir "%BUILD%"

cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /DWIN32 /D_WINDOWS /Fo:"%BUILD%\\" "%~dp0dllmain.cpp" /Fe:"%BUILD%\xajh_login_bridge_v2.dll" /link /DLL user32.lib kernel32.lib
if errorlevel 1 exit /b 1
cl /nologo /utf-8 /O2 /MT /EHsc /W3 /DWIN32 /Fo:"%BUILD%\\" "%~dp0injector.cpp" /Fe:"%BUILD%\xajh_login_inject.exe" /link kernel32.lib
if errorlevel 1 exit /b 1
copy /Y "%BUILD%\xajh_login_bridge_v2.dll" "%OUT%\xajh_login_bridge_v2.dll" >nul
if errorlevel 1 exit /b 1
copy /Y "%BUILD%\xajh_login_inject.exe" "%OUT%\xajh_login_inject.exe" >nul
if errorlevel 1 exit /b 1

echo built:
dir /b "%OUT%\xajh_login_bridge_v2.dll" "%OUT%\xajh_login_inject.exe"
endlocal
