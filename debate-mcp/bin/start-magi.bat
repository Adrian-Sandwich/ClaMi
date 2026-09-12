@echo off
rem Levanta el sistema MAGI completo en Windows: Postgres (portable, si no
rem corre), relay y UI. Uso: bin\start-magi.bat   (doble click o consola)
setlocal
set "ROOT=%~dp0..\.."
set "PG=%ROOT%\experiments\pg\pgsql\bin"
set "PY=%ROOT%\debate-mcp\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [start-magi] no encuentro el venv en %PY%
    echo   crealo:  cd debate-mcp ^&^& python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

"%PG%\pg_isready.exe" -h 127.0.0.1 -p 5432 >nul 2>nul
if errorlevel 1 (
    echo [start-magi] levantando postgres (experiments\pg)...
    rem Start-Process: el postmaster queda detachado de esta consola —
    rem si no, al cerrar la ventana se lleva puesto el servidor.
    powershell -NoProfile -Command "Start-Process -FilePath '%PG%\pg_ctl.exe' -ArgumentList '-D','%ROOT%\experiments\pg\data','-o','\"-p 5432 -c listen_addresses=127.0.0.1,::1\"','-l','%ROOT%\experiments\pg\pg.log','-w','start'"
    "%PG%\pg_isready.exe" -h 127.0.0.1 -p 5432 >nul 2>nul
    if errorlevel 1 (
        echo [start-magi] postgres no arranco — fijate en experiments\pg\pg.log
        exit /b 1
    )
) else (
    echo [start-magi] postgres ya estaba arriba
)

rem Esquema al dia (idempotente: no hace nada si ya esta aplicado)
"%PY%" "%ROOT%\debate-mcp\schema\migrate.py"

rem El conninfo default (dbname=debate) ya alcanza: el postmaster escucha en
rem localhost y libpq toma el usuario del sistema. Si tu setup difiere,
rem descomentá y ajustá:
rem set DEBATE_CONNINFO=dbname=debate user=postgres host=127.0.0.1 port=5432
start "MAGI relay" cmd /k ""%PY%" "%ROOT%\debate-mcp\relay.py""
start "MAGI ui" cmd /k ""%PY%" "%ROOT%\debate-mcp\magi_ui.py""
echo [start-magi] relay y UI arrancando.
echo [start-magi] Interfaz MAGI:  http://127.0.0.1:8051
echo [start-magi] Para parar todo:  bin\stop-magi.bat
