@echo off
REM SPDX-License-Identifier: GPL-3.0-or-later
REM Launcher called by the Windows context menu.
REM Replace the path below with the real path of your metmux.py file :
set "METMUX=C:\Tools\metmux\metmux.py"

if not exist "%METMUX%" (
  echo metmux.py not found: "%METMUX%"
  echo Fix the path above in metmux.bat.
  echo.
  pause
  exit /b 1
)

REM The menu opens one window per selected file; --gather merges them into one session, followers exit with code 3 ("handed off").
REM Prefer "py", fall back to "python" only when py is absent: "py ... || python ..." would also re-run metmux on a non-zero exit.
where py >nul 2>nul
if %errorlevel%==0 (
  py "%METMUX%" --mode=ask --gather %*
) else (
  python "%METMUX%" --mode=ask --gather %*
)
if "%errorlevel%"=="3" exit /b 0
echo.
pause
