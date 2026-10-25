@echo off
rem 中文横幅需要 UTF-8 代码页，否则 echo 出来是乱码
chcp 65001 >nul 2>nul
setlocal EnableExtensions
rem ===========================================================
rem  hub - unified launcher (WorkBuddy x Qoder, one process)
rem
rem  Usage:
rem    start-hub.bat                      local, default ports
rem    start-hub.bat lan                  LAN mode (listen on 0.0.0.0)
rem    start-hub.bat 9001 9002            custom engine ports
rem    start-hub.bat 9001 9002 9003       custom engine + portal ports
rem    start-hub.bat 9001 9002 9003 lan   custom ports + LAN mode
rem
rem  One command starts everything: both engines (internal) and the
rem  fusion portal - the ONLY address that matters (panel + /api/v1).
rem  Engine ports are intentionally not shown: they are implementation
rem  details behind the portal.
rem
rem  Defaults deliberately avoid 8789/8790: those often hold the
rem  operator's own standalone instances, and a collision there would
rem  fool the hub's health checks (fixed in hub/server.py, port_free).
rem
rem  ASCII-only on purpose: .bat files are read using the console code
rem  page, so non-ASCII text breaks the parser.
rem
rem  NOTE: no %VAR% inside an if( ... ) block, because a path containing
rem  ( or ) breaks the cmd.exe parser. All checks use single-line if /
rem  errorlevel / goto instead.
rem ===========================================================

set "HERE=%~dp0"

rem ---- args: "lan" may sit anywhere in %1..%4 ----
set "LANMODE="
set "A1=%~1" & set "A2=%~2" & set "A3=%~3" & set "A4=%~4"
if /I "%A1%"=="lan" set "LANMODE=1"
if /I "%A2%"=="lan" set "LANMODE=1"
if /I "%A3%"=="lan" set "LANMODE=1"
if /I "%A4%"=="lan" set "LANMODE=1"

set "WBPORT=%A1%"
set "QDPORT=%A2%"
if not defined TRAPORT set "TRAPORT=8813"
set "HUBPORT=%A3%"
if /I "%A1%"=="lan" set "WBPORT="
if /I "%A2%"=="lan" set "QDPORT="
if /I "%A3%"=="lan" set "HUBPORT="
if "%WBPORT%"=="" set "WBPORT=8811"
if "%QDPORT%"=="" set "QDPORT=8812"
if "%HUBPORT%"=="" set "HUBPORT=8817"

cd /d "%HERE%"

if not exist "hub\server.py" goto no_entry

rem ---- 1) bundled runtime (ships with the zip) ----
set "PYEXE="
if not exist "python\python.exe" goto try_path
"python\python.exe" --version >nul 2>nul
if errorlevel 1 goto try_path
set "PYEXE=%HERE%python\python.exe"
goto banner

:try_path
rem ---- 2) python on PATH (need 3.9+) ----
python --version >nul 2>nul
if errorlevel 1 goto try_py
set "PYEXE=python"
goto banner

:try_py
rem ---- 3) py launcher ----
py --version >nul 2>nul
if errorlevel 1 goto try_codex
set "PYEXE=py"
goto banner

:try_codex
rem ---- 4) Codex bundled runtimes ----
call :find_codex
if defined PYEXE goto banner

echo [ERROR] No usable Python found (need 3.9+).
echo.
echo Options:
echo   1. Use the packaged zip, which already contains python\
echo   2. Install Python 3.9+ from https://www.python.org/downloads/
echo      (tick "Add python.exe to PATH" during setup)
echo   3. Edit this file and set PYEXE to a full path
echo.
pause
exit /b 1

:no_entry
echo [ERROR] hub\server.py not found next to this script.
echo         expected: %HERE%hub\server.py
echo.
pause
exit /b 1

:find_codex
if not exist "%USERPROFILE%\.cache\codex-runtimes" goto :eof
for /d %%D in ("%USERPROFILE%\.cache\codex-runtimes\*") do call :probe "%%~D"
goto :eof

:probe
if defined PYEXE goto :eof
for /f "delims=" %%P in ('dir /b /s "%~1\python.exe" 2^>nul') do call :probe_one "%%~P"
goto :eof

:probe_one
if defined PYEXE goto :eof
"%~1" --version >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:banner
echo ===========================================================
echo   WorkBuddy x Qoder x Trae-hub
echo.
if defined LANMODE goto banner_lan
echo   唯一入口 : http://127.0.0.1:%HUBPORT%/
echo   API     : http://127.0.0.1:%HUBPORT%/api/v1    (也接受 /v1，二者等价)
goto banner_mid
:banner_lan
echo   唯一入口 : http://0.0.0.0:%HUBPORT%/    [LAN mode]
echo   API     : http://0.0.0.0:%HUBPORT%/api/v1    (也接受 /v1)
echo            other devices use this machine's LAN IP:%HUBPORT%
echo            blocked by firewall? run allow-firewall.bat %HUBPORT%
:banner_mid
echo.
echo   Keep this window open. Press Ctrl+C to stop.
echo ===========================================================
echo.

call :check_dash "hub\engines\wb\dashboard.html" wb
call :check_dash "hub\engines\qd\dashboard.html" qd

rem  One command starts everything: both engines + the fusion portal.
set "PYTHONIOENCODING=utf-8"
set "EXTRA="
if defined LANMODE set "EXTRA=--host 0.0.0.0 --wb-args=--lan --qd-args=--lan --trae-args=--lan"
"%PYEXE%" -m hub.server --wb-port %WBPORT% --qd-port %QDPORT% --trae-port %TRAPORT% --portal-port %HUBPORT% %EXTRA%
set "RC=%ERRORLEVEL%"

echo.
echo [hub exited with code %RC%]
pause
exit /b %RC%

:check_dash
if exist "%~1" goto :eof
echo [WARN] %~1 is MISSING - GET / on the %~2 engine will answer 500.
echo        See README-HUB.md "Known limitations" for the one-line fix.
goto :eof
