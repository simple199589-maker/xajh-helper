@echo off
setlocal
set VCVARS="C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat"
call %VCVARS% x86
if errorlevel 1 exit /b 1
set ROOT=%~dp0..\..
set BUILD=%ROOT%\build\native\dummy_damage_reader
set OUT=%ROOT%\build\native
if defined XAJH_NATIVE_BIN_DIR set OUT=%XAJH_NATIVE_BIN_DIR%
if not exist "%BUILD%" mkdir "%BUILD%"
if not exist "%OUT%" mkdir "%OUT%"
cl /nologo /utf-8 /O2 /MT /EHsc /W3 /DWIN32 /D_WINDOWS /Fo:"%BUILD%\\" "%~dp0reader.cpp" /Fe:"%BUILD%\dummy_damage_reader.exe" /link kernel32.lib
if errorlevel 1 exit /b 1
copy /Y "%BUILD%\dummy_damage_reader.exe" "%OUT%\dummy_damage_reader.exe" >nul
if errorlevel 1 exit /b 1
echo built: %OUT%\dummy_damage_reader.exe
endlocal
