@echo off
rem Reconectar el celular a este proyecto (Remote Control de Claude Code).
rem
rem La linea de comandos de Claude viene dentro del paquete de la app de
rem escritorio y no esta en el PATH, y ademas vive en una carpeta con el numero
rem de version: cada actualizacion cambia la ruta. Por eso no se escribe fija
rem aqui, se busca la mas reciente cada vez.

cd /d "%~dp0.."

set "CLAUDE="
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-ChildItem -Path ""$env:LOCALAPPDATA\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\*\claude.exe"" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName"') do set "CLAUDE=%%i"

if not defined CLAUDE (
  echo.
  echo No encontre claude.exe dentro de la app de Claude.
  echo Puede que la app se haya desinstalado o movido.
  echo.
  pause
  exit /b 1
)

echo Proyecto : %CD%
echo Claude   : %CLAUDE%
echo.
echo Deja esta ventana abierta. Ctrl+C corta la conexion con el celular.
echo.

"%CLAUDE%" rc
pause
