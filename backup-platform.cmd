@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
python "%~dp0server.py" --backup
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo 备份失败，请检查 data 与 backups 目录权限。
  pause
)
popd
exit /b %RESULT%
