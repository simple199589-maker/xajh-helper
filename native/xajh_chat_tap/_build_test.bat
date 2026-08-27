@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x86
cd /d D:\work\python\game-get\native\xajh_chat_tap
cl /nologo /utf-8 /O2 /MT /LD /EHsc /W3 test_min.cpp /link /DLL /OUT:test_min.dll
if errorlevel 1 exit /b 1
echo TEST DLL BUILD OK
dir test_min.dll
