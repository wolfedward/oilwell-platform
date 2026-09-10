@echo off
setlocal
pushd "%~dp0"
python "%~dp0server.py" --stop
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
