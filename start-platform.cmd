@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python 3，请先安装并将 python 加入 PATH。
  pause
  popd
  exit /b 1
)
python "%~dp0server.py" --launch --open
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo 平台启动失败，请查看 logs\runtime.log。
  pause
)
popd
exit /b %RESULT%
