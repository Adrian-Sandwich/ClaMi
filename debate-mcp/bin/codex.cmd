@echo off
rem Shim del codex CLI standalone: resuelve el release mas reciente en
rem %USERPROFILE%\.codex\packages\standalone\releases y lo delega. Asi las
rem actualizaciones de codex no rompen heads.json (la ruta lleva version).
setlocal
set "ROOT=%USERPROFILE%\.codex\packages\standalone\releases"
for /f "delims=" %%v in ('dir /b /ad /o-n "%ROOT%" 2^>nul') do (
    if exist "%ROOT%\%%v\bin\codex.exe" (
        "%ROOT%\%%v\bin\codex.exe" %*
        exit /b %ERRORLEVEL%
    )
)
echo [codex-shim] no encuentro ningun release en %ROOT% >&2
exit /b 1
