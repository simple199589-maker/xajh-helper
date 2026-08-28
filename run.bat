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
for %%F in ("%SRC%\*.dll" "%SRC%\*.exe") do (
  copy /Y "%%F" "%DST%\" >nul 2>&1
  if errorlevel 1 (
    echo [run] copy failed ^(a running process may lock the DLL^): %%~nxF
    echo      close the helper/game first, then retry
    exit /b 1
  )
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