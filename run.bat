@echo off
REM Source-run launcher: sync the latest native products from build\native into
REM the run dir native\bin, then start the GUI. Build products first with
REM   native\build_all.bat    (or tools\build_gui.bat, which also syncs).
REM @author by ak
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

set "SRC=%ROOT%build\native"
set "DST=%ROOT%native\bin"

REM Make sure there is actually a built product dir to copy from.
call python tools\check_native_bin.py build\native
if errorlevel 1 (
  echo [run] product dir incomplete: %SRC%
  echo      build native products first: native\build_all.bat
  exit /b 1
)

if not exist "%DST%" mkdir "%DST%"
set "SYNC_FAIL="
for %%F in ("%SRC%\*.dll" "%SRC%\*.exe") do (
  call :sync_one "%%F"
  if errorlevel 1 set "SYNC_FAIL=1"
)
if defined SYNC_FAIL (
  echo [run] sync failed: a running process may lock a DLL in %DST%
  echo      close the helper/game first, then retry
  exit /b 1
)

call python tools\check_native_bin.py native\bin
if errorlevel 1 (
  echo [run] run dir still incomplete: %DST%
  exit /b 1
)

echo [run] native dir synced: %DST%
call python main.py
set "RC=%errorlevel%"
endlocal
exit /b %RC%

REM ---- incremental sync helpers ----
REM Copy a native file only when its content differs from the run dir. Identical
REM files are left untouched so a source-run never re-copies (and so never locks)
REM an unchanged DLL.
:sync_one
set "SRC_FILE=%~1"
set "DST_FILE=%DST%\%~nx1"

if exist "%DST_FILE%" goto :sync_maybe_diff

copy /Y "%SRC_FILE%" "%DST_FILE%" >nul 2>&1
exit /b %errorlevel%

:sync_maybe_diff
fc /b "%SRC_FILE%" "%DST_FILE%" >nul 2>&1
if not errorlevel 1 exit /b 0

copy /Y "%SRC_FILE%" "%DST_FILE%" >nul 2>&1
exit /b %errorlevel%