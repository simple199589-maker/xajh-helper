@echo off
setlocal
set VCVARS="C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat"
if not exist %VCVARS% (
  echo vcvarsall.bat not found
  exit /b 1
)
call %VCVARS% x86
if errorlevel 1 exit /b 1

set ROOT=%~dp0..\..
set BUILD=%ROOT%\build\native
if not exist "%BUILD%" mkdir "%BUILD%"

cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /DWIN32 /D_WINDOWS /Fo:"%BUILD%\\" "%~dp0dllmain.cpp" /Fe:"%BUILD%\xajh_bridge.dll" /link /DLL user32.lib kernel32.lib psapi.lib advapi32.lib
if errorlevel 1 exit /b 1
cl /nologo /utf-8 /O2 /MT /EHsc /W3 /DWIN32 /Fo:"%BUILD%\\" "%~dp0injector.cpp" /Fe:"%BUILD%\xajh_inject.exe" /link kernel32.lib
if errorlevel 1 exit /b 1
for /f "delims=" %%I in ('python -c "from app.core.bridge_protocol import BRIDGE_BUILD_ID; print(BRIDGE_BUILD_ID)"') do set "BRIDGE_BUILD_ID=%%I"
if not defined BRIDGE_BUILD_ID (
  echo failed to resolve BRIDGE_BUILD_ID from app/core/bridge_protocol.py
  exit /b 1
)
copy /Y "%BUILD%\xajh_bridge.dll" "%BUILD%\xajh_bridge_%BRIDGE_BUILD_ID%.dll" >nul
if errorlevel 1 exit /b 1

echo built:
dir /b "%BUILD%\xajh_bridge*.dll" "%BUILD%\xajh_inject.exe"
endlocal


