@echo off
setlocal
pushd "%~dp0"
python "%~dp0server.py" --stop
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo Platform is not running or could not stop. See logs\runtime.log.
popd
exit /b %RESULT%
