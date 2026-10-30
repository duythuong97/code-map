@echo off
rem Code Map: codemap.cmd ^<init^|validate^|extract^|serve^|mcp^|stale^|kb^> [options]   (codemap.cmd --help)
"%~dp0.venv\Scripts\python.exe" -m codemap %*
exit /b %ERRORLEVEL%
