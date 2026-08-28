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
set OUT=%ROOT%\build\native
if defined XAJH_NATIVE_BIN_DIR set OUT=%XAJH_NATIVE_BIN_DIR%
set BUILD=%ROOT%\build\native\chat_tap
if not exist "%OUT%" mkdir "%OUT%"
if not exist "%BUILD%" mkdir "%BUILD%"

cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /Brepro /DWIN32 /D_WINDOWS ^
  /Fo:"%BUILD%\\" "%~dp0dllmain.cpp" ^
  /Fe:"%BUILD%\xajh_chat_tap.dll" /link /Brepro /DLL kernel32.lib
if errorlevel 1 exit /b 1
copy /Y "%BUILD%\xajh_chat_tap.dll" "%OUT%\xajh_chat_tap.dll" >nul
if errorlevel 1 exit /b 1

echo built:
dir /b "%OUT%\xajh_chat_tap.dll"
endlocal
