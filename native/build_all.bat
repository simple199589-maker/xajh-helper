@echo off
REM One-shot native bootstrap: build every bridge / injector / tap / reader into
REM the product dir build\native and verify the full inventory. Run after a fresh
REM clone, or whenever a native .cpp/.h changes. Startup (run.bat) then copies
REM build\native -> native\bin, so source-run never hits "missing dll".
REM @author by ak
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%"
REM Always target build\native (the product dir), regardless of any lingering
REM XAJH_NATIVE_BIN_DIR left by tools\build_gui.bat.
set "XAJH_NATIVE_BIN_DIR="

echo [build_all] xajh_bridge...
call "native\xajh_bridge\build_x86.bat"
if errorlevel 1 goto :fail

echo [build_all] xajh_chat_tap...
call "native\xajh_chat_tap\build_x86.bat"
if errorlevel 1 goto :fail

echo [build_all] xajh_team_tap...
call "native\xajh_team_tap\build_x86.bat"
if errorlevel 1 goto :fail

echo [build_all] xajh_login_bridge...
call "native\xajh_login_bridge\build_x86.bat"
if errorlevel 1 goto :fail

echo [build_all] dummy_damage_reader...
call "native\dummy_damage_reader\build_x86.bat"
if errorlevel 1 goto :fail

echo [build_all] verify inventory...
call python tools\check_native_bin.py build\native
if errorlevel 1 goto :fail

echo.
echo native/build_all: OK ^(full inventory present in build\native^)
echo startup copies to native\bin via run.bat
endlocal
exit /b 0

:fail
echo native/build_all: FAILED
endlocal
exit /b 1