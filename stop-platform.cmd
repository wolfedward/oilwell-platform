@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
python "%~dp0server.py" --stop
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo 平台未运行或停止失败，请查看 logs\runtime.log。
popd
exit /b %RESULT%
