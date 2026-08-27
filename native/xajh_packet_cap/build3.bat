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
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format HHmmss"') do set TS=%%i
cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /DWIN32 /D_WINDOWS /Fo:"%BUILD%\\" "%~dp0dllmain3.cpp" /Fe:"%BUILD%\xajh_packet_cap3_%TS%.dll" /link /DLL user32.lib kernel32.lib
if errorlevel 1 exit /b 1
echo BUILD_OK %TS%
dir /b "%BUILD%\xajh_packet_cap3_%TS%.dll"
endlocal
