@echo off
rem ===========================================================
rem  hub - LAN mode launcher (double-click me for LAN access)
rem
rem  Forwards to start-hub.bat with the "lan" keyword: listens on
rem  0.0.0.0 and passes --lan to both engines.
rem
rem  Optional port overrides (order: wb qd portal):
rem    start-hub-lan.bat 9001 9002 9003
rem
rem  ASCII-only on purpose: .bat files are read using the console
rem  code page, so non-ASCII text breaks the parser.
rem ===========================================================
call "%~dp0start-hub.bat" %* lan
