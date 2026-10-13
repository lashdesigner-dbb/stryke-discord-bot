@echo off
setlocal
cd /d "%~dp0.."

where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3.10+ nao foi encontrado. Instale-o e marque "Add Python to PATH".
    pause
    exit /b 1
  )
  set "PY=python"
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Criei .env. Preencha DISCORD_TOKEN e DISCORD_CLIENT_ID, salve e execute este arquivo novamente.
  notepad .env
  pause
  exit /b 1
)

findstr /R /C:"^DISCORD_TOKEN=..*" .env >nul
if errorlevel 1 (
  echo DISCORD_TOKEN esta vazio em .env. Preencha-o e execute novamente.
  notepad .env
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  "%PY%" -m venv .venv
  if errorlevel 1 goto setup_error
)

call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_error

:run_bot
echo Iniciando STRYKE.BOT...
".venv\Scripts\python.exe" -u main.py
echo O bot encerrou. Tentando iniciar novamente em 10 segundos...
timeout /t 10 /nobreak >nul
goto run_bot

:setup_error
echo Falha ao preparar Python ou instalar dependencias.
pause
exit /b 1
