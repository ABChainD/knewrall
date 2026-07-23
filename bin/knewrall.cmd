@echo off
REM Knewrall launcher (Windows). Forwards all args to the path-independent
REM Python entry point next to this file.
python "%~dp0knewrall.py" %*
