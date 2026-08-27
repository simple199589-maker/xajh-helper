@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x86
if errorlevel 1 (
  echo VCVARS_FAIL
  exit /b 1
)
cd /d "%~dp0"
set OUT=%~dp0..\..\build\native\capture
if not exist "%OUT%" mkdir "%OUT%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
echo TS=%TS%
cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /DWIN32 /D_WINDOWS dllmain.cpp /Fe:"%OUT%\xajh_bridge_%TS%.dll" /Fo:"%OUT%\\" /link /DLL user32.lib kernel32.lib psapi.lib advapi32.lib
if errorlevel 1 (
  echo BUILD_FAIL
  exit /b 1
)
echo BUILD_OK %TS%
dir "%OUT%\xajh_bridge_%TS%.dll"
endlocal
