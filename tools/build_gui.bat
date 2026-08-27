@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
echo cwd=%CD%

REM Channel: default prod; pass "dev" for test package.
set "BUILD_CHANNEL=prod"
if /I "%~1"=="dev" set "BUILD_CHANNEL=dev"
if /I "%~1"=="test" set "BUILD_CHANNEL=dev"
if /I "%~1"=="prod" set "BUILD_CHANNEL=prod"
if /I "%~1"=="production" set "BUILD_CHANNEL=prod"
echo build_channel=%BUILD_CHANNEL%
if /I "%BUILD_CHANNEL%"=="dev" if not defined XAJH_CAPTCHA_API_KEY (
  if not exist ".env" (
    echo WARNING: no captcha key for dev package.
    echo          Set XAJH_CAPTCHA_API_KEY, or create .env from .env.example.
  )
)

REM Optional second arg: versioned output dir, e.g. "build_gui.bat prod v1.0.7"
REM packs into dist\xajh_helper-v1.0.7 instead of the default dist\xajh_helper.
REM Use this when the default folder is locked by a running game / loaded DLL.
set "PKG_DIR=xajh_helper"
set "XAJH_PKG_VERSION=%~2"
if defined XAJH_PKG_VERSION if not "%XAJH_PKG_VERSION%"=="" set "PKG_DIR=xajh_helper-%XAJH_PKG_VERSION%"
set "XAJH_PKG_DIR=%PKG_DIR%"
echo pkg_dir=%PKG_DIR%

set "XAJH_BUILD_PROFILE_PATH=%CD%\build\generated\app\data\build_profile.json"
set "XAJH_NATIVE_BIN_DIR=%CD%\build\native"
if not exist "%XAJH_NATIVE_BIN_DIR%" mkdir "%XAJH_NATIVE_BIN_DIR%"
echo [0/5] write generated build_profile.json (%BUILD_CHANNEL%)...
call python tools\_write_build_profile.py %BUILD_CHANNEL%
if errorlevel 1 (
  echo write build_profile failed
  exit /b 1
)

echo [1/5] ensure native bridge...
REM Always rebuild: an existing DLL may predate dllmain.cpp and must never be
REM silently copied into a new package.
call "native\xajh_bridge\_build_once.bat"
if errorlevel 1 (
  echo native bridge build failed
  exit /b 1
)
copy /Y "native\bin\xajh_chat_tap.dll" "%XAJH_NATIVE_BIN_DIR%\xajh_chat_tap.dll" >nul
if errorlevel 1 (
  echo failed to stage native\bin\xajh_chat_tap.dll
  exit /b 1
)
copy /Y "native\bin\xajh_team_tap.dll" "%XAJH_NATIVE_BIN_DIR%\xajh_team_tap.dll" >nul
if errorlevel 1 (
  echo failed to stage native\bin\xajh_team_tap.dll
  exit /b 1
)
copy /Y "native\bin\xajh_login_bridge_v2.dll" "%XAJH_NATIVE_BIN_DIR%\xajh_login_bridge_v2.dll" >nul
if errorlevel 1 (
  echo failed to stage native\bin\xajh_login_bridge_v2.dll
  exit /b 1
)
copy /Y "native\bin\xajh_login_inject.exe" "%XAJH_NATIVE_BIN_DIR%\xajh_login_inject.exe" >nul
if errorlevel 1 (
  echo failed to stage native\bin\xajh_login_inject.exe
  exit /b 1
)
if not exist "%XAJH_NATIVE_BIN_DIR%\dummy_damage_reader.exe" (
  call "native\dummy_damage_reader\build_x86.bat"
  if errorlevel 1 (
    echo native dummy damage reader build failed
    exit /b 1
  )
)
if not exist "%XAJH_NATIVE_BIN_DIR%\xajh_bridge.dll" (
  echo missing %XAJH_NATIVE_BIN_DIR%\xajh_bridge.dll
  exit /b 1
)
if not exist "%XAJH_NATIVE_BIN_DIR%\xajh_inject.exe" (
  echo missing %XAJH_NATIVE_BIN_DIR%\xajh_inject.exe
  exit /b 1
)
if not exist "%XAJH_NATIVE_BIN_DIR%\xajh_team_tap.dll" (
  echo missing %XAJH_NATIVE_BIN_DIR%\xajh_team_tap.dll
  exit /b 1
)
if not exist "%XAJH_NATIVE_BIN_DIR%\xajh_login_bridge_v2.dll" (
  echo missing %XAJH_NATIVE_BIN_DIR%\xajh_login_bridge_v2.dll
  exit /b 1
)
if not exist "%XAJH_NATIVE_BIN_DIR%\xajh_login_inject.exe" (
  echo missing %XAJH_NATIVE_BIN_DIR%\xajh_login_inject.exe
  exit /b 1
)
if not exist "%XAJH_NATIVE_BIN_DIR%\dummy_damage_reader.exe" (
  echo missing %XAJH_NATIVE_BIN_DIR%\dummy_damage_reader.exe
  exit /b 1
)
echo native bridge: OK

echo [2/5] ensure PyInstaller...
REM pyenv "python" is a .bat shim. Without "call", this script exits after pip.
REM Also avoid "pyinstaller>=6.0": bare ">" is cmd redirect.
call python -m pip install -q pyinstaller
if errorlevel 1 (
  echo pip install pyinstaller failed
  exit /b 1
)
call python -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
if errorlevel 1 (
  echo PyInstaller not available. Try: python -m pip install pyinstaller
  exit /b 1
)

echo [3/5] package GUI onedir (%BUILD_CHANNEL%)...
REM dist is locked if helper is still running (staged native\bin under dist).
REM Injected game may also lock staged xajh_bridge.dll under dist\...\native\bin.
tasklist /FI "IMAGENAME eq xajh_helper.exe" 2>nul | findstr /I "xajh_helper.exe" >nul
if not errorlevel 1 (
  echo WARNING: xajh_helper.exe is running - closing it so dist can be rebuilt
  taskkill /F /IM xajh_helper.exe >nul 2>&1
  timeout /t 1 /nobreak >nul
)
call python tools\_free_dist_lock.py "%PKG_DIR%"
if errorlevel 1 (
  echo Cannot free dist\%PKG_DIR% - close helper/game and retry
  exit /b 1
)
call python -m PyInstaller --noconfirm --clean xajh_helper.spec
if errorlevel 1 (
  echo PyInstaller package failed
  echo If PermissionError on dist\, close xajh_helper.exe / game and retry.
  exit /b 1
)
call python tools\_verify_packaged_bridge.py "%PKG_DIR%"
if errorlevel 1 (
  echo Packaged native bridge verification failed
  exit /b 1
)

REM Production uses a normal UPX shell on the top-level launcher only.
REM Dev stays unpacked for diagnostics. Override either default with:
REM   set XAJH_ENABLE_UPX=0   or   set XAJH_ENABLE_UPX=1
set "ENABLE_UPX=1"
if /I "%BUILD_CHANNEL%"=="dev" set "ENABLE_UPX=0"
if defined XAJH_ENABLE_UPX set "ENABLE_UPX=%XAJH_ENABLE_UPX%"
if /I "%ENABLE_UPX%"=="false" set "ENABLE_UPX=0"
if /I "%ENABLE_UPX%"=="no" set "ENABLE_UPX=0"
if /I "%ENABLE_UPX%"=="off" set "ENABLE_UPX=0"

echo [4/5] apply launcher shell enable_upx=%ENABLE_UPX%...
if "%ENABLE_UPX%"=="0" (
  echo UPX shell skipped.
) else (
  call python tools\_pack_upx.py
  if errorlevel 1 (
    echo UPX shell failed - refusing to publish an unverified production package
    exit /b 1
  )
)

echo [5/5] frozen runtime smoke test...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = Start-Process -FilePath '.\dist\%PKG_DIR%\xajh_helper.exe' -ArgumentList '--xajh-build-smoke-test' -Wait -PassThru; exit $p.ExitCode"
if errorlevel 1 (
  echo Frozen runtime smoke test failed
  exit /b 1
)
echo frozen runtime: OK

echo.
echo BUILD_OK channel=%BUILD_CHANNEL%
echo launcher_upx=%ENABLE_UPX%
echo.
if /I "%BUILD_CHANNEL%"=="dev" (
  echo Test package: debug default ON; captcha key from env or .env.
) else (
  echo Production package: debug default OFF, captcha key empty.
)
echo.
echo Run this (NOT build\):
echo   dist\%PKG_DIR%\xajh_helper.exe
echo.
echo The full package is the whole folder:
echo   dist\%PKG_DIR%\
echo Do not run build\xajh_helper\xajh_helper.exe (incomplete intermediate).
if not exist "dist\%PKG_DIR%\xajh_helper.exe" (
  echo ERROR: dist\%PKG_DIR%\xajh_helper.exe missing
  exit /b 1
)
dir "dist\%PKG_DIR%\xajh_helper.exe"
if exist "dist\%PKG_DIR%\_internal\python312.dll" (
  echo python312.dll: OK
) else (
  echo ERROR: python312.dll missing under dist\_internal
  exit /b 1
)
if exist "dist\%PKG_DIR%\_internal\app\data\build_profile.json" (
  echo build_profile.json: OK ^(contents hidden; dev profile may contain credential^)
) else if exist "dist\%PKG_DIR%\app\data\build_profile.json" (
  echo build_profile.json: OK ^(contents hidden; dev profile may contain credential^)
) else (
  echo WARNING: build_profile.json not found under dist (check datas in spec)
)
endlocal
exit /b 0
