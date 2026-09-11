@echo off
setlocal
pushd "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found on PATH.
  pause
  popd
  exit /b 1
)
python "%~dp0server.py" --launch --open
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo Platform startup failed. See logs\runtime.log.
  pause
)
popd
exit /b %RESULT%
