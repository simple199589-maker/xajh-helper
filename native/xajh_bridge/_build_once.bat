@echo off
REM Keep the GUI build entry point and native bridge build on one stamped-output path.
call "%~dp0build_x86.bat"
exit /b %errorlevel%

