@echo off
rem Builds AIMOD.exe into build\dist (then copy it to app\ to run it, or into release\ to ship it).
rem Uses build\release\AIMOD.spec: it carries the comtypes imports the scanner needs, so don't build
rem from the PyInstaller command line (that writes a new spec without them).
setlocal
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
"%PY%" -m unittest discover -s tests || exit /b 1
"%PY%" -m PyInstaller --noconfirm --distpath build\dist --workpath build\work build\release\AIMOD.spec || exit /b 1
echo built build\dist\AIMOD.exe
