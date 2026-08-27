@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x86
cd /d D:\work\python\game-get\native\xajh_chat_tap
if not exist build mkdir build
cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 /DWIN32 /D_WINDOWS /Fobuild\ dllmain.cpp /Febuild\xajh_chat_tap.dll
if errorlevel 1 exit /b 1
echo BUILD OK
dir build\xajh_chat_tap.dll
