@echo off
setlocal
set "WX_NODE=%WECHAT_DIRECT_NODE%"
if not "%WX_NODE%"=="" if exist "%WX_NODE%" goto launch
set "WX_NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
if exist "%WX_NODE%" goto launch
echo Codex Node.js runtime not found. 1>&2
exit /b 1
:launch
"%WX_NODE%" "%~dp0mcp-server.mjs"
exit /b %errorlevel%
