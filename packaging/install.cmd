@echo off
setlocal
set "WX_NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
if not exist "%WX_NODE%" (
  echo Codex Node.js runtime not found. Open Codex once, then try again.
  exit /b 1
)
"%WX_NODE%" "%~dp0install.mjs"
exit /b %errorlevel%
