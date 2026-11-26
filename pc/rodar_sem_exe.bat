@echo off
chcp 65001 >nul
title FTCam
cd /d "%~dp0"
rem --- acha o Python (py launcher, PATH, Anaconda/Miniconda ou instalação padrão) ---
set "PY="
where py >nul 2>nul && (py -3 --version >nul 2>nul && set "PY=py -3")
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY for %%P in ("%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe" "%ProgramData%\anaconda3\python.exe" "%ProgramData%\miniconda3\python.exe" "%LOCALAPPDATA%\anaconda3\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe") do if not defined PY if exist %%P set "PY=%%~P"
if not defined PY (
  echo [ERRO] Nao achei o Python neste PC.
  echo Instale o Python 3.12 em python.org marcando "Add python.exe to PATH" e rode de novo.
  pause
  exit /b 1
)
echo Usando Python: %PY%

if not exist .venv\Scripts\python.exe (
  echo Preparando o ambiente pela primeira vez ^(uns minutos^)...
  %PY% -m venv .venv || goto erro
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt || goto erro
)
if not exist ffmpeg.exe (where ffmpeg >nul 2>nul || echo [AVISO] ffmpeg nao encontrado: rode o build.bat uma vez que ele baixa, ou coloque o ffmpeg.exe nesta pasta.)
echo Abrindo o FTCam...
.venv\Scripts\python app.py
if errorlevel 1 goto erro
exit /b 0
:erro
echo.
echo [ERRO] Algo deu errado. Leia a mensagem acima ou o arquivo dados\ftcam.log
pause
exit /b 1
