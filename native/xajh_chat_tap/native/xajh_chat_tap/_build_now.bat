@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x86
if errorlevel 1 exit /b 1

set OUT=D:\work\python\game-get\native\xajh_chat_tap\build
if not exist "%OUT%" mkdir "%OUT%"

cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /DWIN32 /D_WINDOWS ^
  /Fo:"%OUT%\" dllmain.cpp ^
  /Fe:"%OUT%\xajh_chat_tap.dll" /link /DLL kernel32.lib
if errorlevel 1 exit /b 1

echo BUILD OK
dir "%OUT%\xajh_chat_tap.dll"
