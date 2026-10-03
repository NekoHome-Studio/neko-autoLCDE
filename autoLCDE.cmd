@echo off
rem ===========================================================================
rem  autoLCDE launcher - finds a Python interpreter and forwards all arguments.
rem  ASCII only on purpose: this file must render correctly in any console code page.
rem ===========================================================================
setlocal
set "HERE=%~dp0"
set "PY="

where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [autoLCDE] Python not found. Install Python 3.9+ and make sure "python" is on PATH.
  echo [autoLCDE] Download: https://www.python.org/downloads/
  exit /b 1
)

%PY% "%HERE%autoLCDE.py" %*
exit /b %ERRORLEVEL%
