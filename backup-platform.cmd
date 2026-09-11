@echo off
setlocal
pushd "%~dp0"
python "%~dp0server.py" --backup
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo Backup failed. Check data and backups directory permissions.
  pause
)
popd
exit /b %RESULT%
