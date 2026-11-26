@echo off
chcp 65001 >nul
title FTCam - gerando o .exe
cd /d "%~dp0"

echo.
echo  === FTCam: gerando o executavel ===
echo.

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
  echo [1/4] Criando ambiente virtual...
  %PY% -m venv .venv || goto erro
)
call .venv\Scripts\activate.bat

echo [2/4] Instalando dependencias...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto erro

if not exist icon.ico python make_icon.py

echo [3/4] Empacotando...
pyinstaller --noconfirm --clean --windowed --name FTCam --icon icon.ico ^
  --add-data "ui;ui" --add-data "icon.png;." --add-data "modelos;modelos" ^
  --collect-all webview --hidden-import pystray._win32 ^
  app.py || goto erro

echo [4/4] Conferindo o ffmpeg...
if exist ffmpeg.exe copy /y ffmpeg.exe dist\FTCam\ >nul
if not exist dist\FTCam\ffmpeg.exe (
  echo      Baixando ffmpeg ^(uns 90 MB^)...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile ffmpeg.zip; Expand-Archive ffmpeg.zip -DestinationPath ffmpeg_tmp -Force; Copy-Item (Get-ChildItem ffmpeg_tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName 'dist\FTCam\ffmpeg.exe'; Copy-Item 'dist\FTCam\ffmpeg.exe' 'ffmpeg.exe'; Remove-Item ffmpeg.zip; Remove-Item ffmpeg_tmp -Recurse -Force"
)
if not exist dist\FTCam\ffmpeg.exe echo [AVISO] Nao consegui baixar o ffmpeg. Coloque o ffmpeg.exe dentro de dist\FTCam manualmente.

echo.
echo  Pronto! O programa esta em:  %cd%\dist\FTCam\FTCam.exe
echo  Pode mover a pasta dist\FTCam inteira pra onde quiser.
echo.
explorer dist\FTCam
pause
exit /b 0

:erro
echo.
echo [ERRO] Algo deu errado. Leia a mensagem acima.
pause
exit /b 1
