@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PLATFORM_PYTHON=%~dp0.venv\Scripts\python.exe"
  goto runtime_ready
)
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found on PATH.
  pause
  popd
  exit /b 1
)
set "PLATFORM_PYTHON=python"
:runtime_ready
if exist "%~dp0config\center.local.toml" (
  "%PLATFORM_PYTHON%" "%~dp0server.py" --launch --open --center-config "%~dp0config\center.local.toml" %*
) else (
  "%PLATFORM_PYTHON%" "%~dp0server.py" --launch --open %*
)
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo Platform startup failed. See logs\runtime.log.
  pause
)
popd
exit /b %RESULT%
