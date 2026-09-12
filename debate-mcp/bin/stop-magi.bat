@echo off
rem Para relay y UI (las ventanas cmd que abrio start-magi) y baja Postgres.
setlocal
set "ROOT=%~dp0..\.."
set "PG=%ROOT%\experiments\pg\pgsql\bin"

taskkill /F /FI "WINDOWTITLE eq MAGI relay*" >nul 2>nul
taskkill /F /FI "WINDOWTITLE eq MAGI ui*" >nul 2>nul

"%PG%\pg_isready.exe" -h 127.0.0.1 -p 5432 >nul 2>nul
if not errorlevel 1 (
    "%PG%\pg_ctl.exe" -D "%ROOT%\experiments\pg\data" stop
) else (
    echo [stop-magi] postgres ya estaba abajo
)
echo [stop-magi] listo
