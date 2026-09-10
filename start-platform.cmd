@echo off
setlocal
pushd "%~dp0"
python "%~dp0server.py" --launch --open
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
