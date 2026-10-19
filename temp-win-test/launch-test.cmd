@echo off
rem Standalone test launcher for the KiroCrew PR 22024 build.
rem Keeps the test build away from the installed KiroCrew:
rem   - own Electron data folder (--user-data-dir), so its single-instance lock and settings are separate
rem   - own KiroCrew data home (KIROCREW_HOME), so no sessions, secrets or config are shared
rem   - own gateway port 5499 (the installed app uses 5476)
rem   - update checks pointed at a dead address so it can never offer to install over your app
rem Pass "debug" to paint Electron's window-drag regions: launch-test.cmd debug
setlocal
set "BASE=%LOCALAPPDATA%\KiroCrewPR22024Test"
set "KIROCREW_HOME=%BASE%\home"
set "KIROCREW_UPDATE_FEED=http://127.0.0.1:9/no-updates"
if /i "%~1"=="debug" set "ELECTRON_DEBUG_DRAGGABLE_REGIONS=1"
if not exist "%KIROCREW_HOME%" mkdir "%KIROCREW_HOME%"
if not exist "%KIROCREW_HOME%\config.json" > "%KIROCREW_HOME%\config.json" echo {"dashboard":{"url":"http://127.0.0.1:5499"}}
echo Test data lives in %BASE%
echo Delete that folder to reset the test build.
set "EXE=%~dp0KiroCrew.exe"
if not exist "%EXE%" set "EXE=%~dp0kirocrew-desktop.exe"
start "" "%EXE%" --user-data-dir="%BASE%\userdata"
