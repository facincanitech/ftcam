@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Instalador do FTCam

rem ---- precisa de administrador (instalar Python/WebView2 e liberar o Firewall)
net session >nul 2>&1
if errorlevel 1 (
  echo  Pedindo permissao de administrador...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
  exit /b 0
)
cd /d "%~dp0"
echo.
echo  ==========================================
echo           Instalador do FTCam
echo  ==========================================
echo.

rem ---- pasta de instalacao (do usuario: o Windows sempre deixa gravar, nao precisa admin)
set "DEST=%LOCALAPPDATA%\Programs\FTCam"
echo  Vai instalar em: %DEST%
echo  (gravacoes e configuracoes ficam nessa pasta; da pra mudar a pasta das gravacoes depois nos Ajustes)
echo.

rem ---- fecha um FTCam aberto (pra poder atualizar os arquivos)
taskkill /f /im FTCam.exe >nul 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { (($_.Name -like 'python*') -and ($_.CommandLine -match 'FTCam')) -or (($_.Name -like 'ffmpeg*') -and ($_.ExecutablePath -match 'FTCam')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
timeout /t 2 /nobreak >nul
taskkill /f /im ffmpeg.exe >nul 2>nul

rem ---- acha o Python
call :achapy
echo  Conferindo requisitos...
if not defined PY (
  echo  - Python: nao encontrado. Instalando o Python 3.12...
  where winget >nul 2>nul && winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
  call :achapy
)
if not defined PY (
  echo    Baixando o instalador oficial do Python...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile (Join-Path $env:TEMP 'py312.exe')"
  "%TEMP%\py312.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1
  call :achapy
)
if not defined PY (
  echo.
  echo  [ERRO] Nao consegui instalar o Python.
  echo  Instale o Python 3.12 em python.org marcando "Add python.exe to PATH" e rode este instalador de novo.
  pause & exit /b 1
)
echo  - Python: ok (%PY%)

rem ---- WebView2 (a janela do FTCam usa; Windows 11 ja tem, Windows 10 as vezes nao)
set "WV2=0"
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WV2=1"
reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WV2=1"
if "%WV2%"=="0" (
  echo  - WebView2: nao encontrado. Instalando...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile (Join-Path $env:TEMP 'wv2.exe')"
  "%TEMP%\wv2.exe" /silent /install
) else (echo  - WebView2: ok)

rem ---- copia os arquivos (sem apagar dados/gravacoes de uma instalacao anterior)
echo.
echo  [1/5] Copiando o programa...
if not exist "%DEST%" mkdir "%DEST%"
robocopy "%~dp0." "%DEST%" /E /NFL /NDL /NJH /NJS /NP /XD .venv dados gravacoes __pycache__ /XF instalar.bat >nul
if errorlevel 8 (echo  [ERRO] Falha ao copiar os arquivos. & pause & exit /b 1)

rem ---- ambiente Python + dependencias
echo  [2/5] Preparando o Python (na primeira vez demora uns minutos)...
if not exist "%DEST%\.venv\Scripts\python.exe" (
  %PY% -m venv "%DEST%\.venv" || (echo  [ERRO] Nao criou o ambiente Python. & pause & exit /b 1)
)
"%DEST%\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
"%DEST%\.venv\Scripts\pip.exe" install -r "%DEST%\requirements.txt" -q || (echo  [ERRO] Falha ao instalar as dependencias. Confira a internet. & pause & exit /b 1)

rem ---- ffmpeg (grava e gera o som ao vivo; esta versao tem MP3)
echo  [3/5] Conferindo o ffmpeg...
if not exist "%DEST%\ffmpeg.exe" (
  echo       Baixando ffmpeg ^(uns 150 MB^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $t=Join-Path $env:TEMP 'ftcam_ff'; New-Item -ItemType Directory -Force $t | Out-Null; Invoke-WebRequest 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile (Join-Path $t 'ff.zip'); Expand-Archive (Join-Path $t 'ff.zip') -DestinationPath $t -Force; Copy-Item (Get-ChildItem $t -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName '%DEST%\ffmpeg.exe'; Remove-Item $t -Recurse -Force"
)
if not exist "%DEST%\ffmpeg.exe" echo  [AVISO] Nao consegui baixar o ffmpeg. Coloque o ffmpeg.exe em %DEST% manualmente.

rem ---- modelos de IA
echo  [4/5] Conferindo os modelos de IA...
if not exist "%DEST%\modelos" mkdir "%DEST%\modelos"
call :modelo face_detection_yunet face_detection_yunet_2023mar.onnx
call :modelo face_recognition_sface face_recognition_sface_2021dec.onnx
call :modelo object_detection_yolox object_detection_yolox_2022nov.onnx

rem ---- atalhos (abre sem janela preta)
echo  [5/5] Criando atalhos...
set "PYW=%DEST%\.venv\Scripts\pythonw.exe"
powershell -NoProfile -Command "$s=New-Object -ComObject WScript.Shell; foreach($p in @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) '')) ){ $l=$s.CreateShortcut((Join-Path $p 'FTCam.lnk')); $l.TargetPath='%PYW%'; $l.Arguments='\"%DEST%\app.py\"'; $l.WorkingDirectory='%DEST%'; $l.IconLocation='%DEST%\icon.ico'; $l.Description='FTCam - DVR'; $l.Save() }"

rem ---- Firewall: libera a porta 8080 na rede de casa (pra abrir pelo celular)
netsh advfirewall firewall delete rule name="FTCam" >nul 2>nul
netsh advfirewall firewall add rule name="FTCam" dir=in action=allow protocol=TCP localport=8080 profile=private,domain >nul && echo  - Firewall: porta 8080 liberada na rede de casa

rem ---- PC ligado 24h: nao suspender na tomada
echo.
choice /c SN /m " Deixar este PC sem suspender/hibernar quando estiver na tomada (recomendado pra DVR 24h)"
if not errorlevel 2 (
  powercfg /change standby-timeout-ac 0 >nul 2>nul
  powercfg /change hibernate-timeout-ac 0 >nul 2>nul
  echo  Ok: nao vai suspender na tomada.
)

rem ---- iniciar com o Windows
choice /c SN /m " Abrir o FTCam sozinho quando o Windows ligar"
if not errorlevel 2 (
  reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v FTCam /t REG_SZ /d "\"%PYW%\" \"%DEST%\app.py\" --inicio" /f >nul
  echo  Ok: inicia com o Windows.
)

echo.
echo  ==========================================
echo   Pronto! FTCam instalado.
echo   Abra pelo atalho "FTCam" na area de trabalho.
echo   Se o Windows ainda perguntar sobre o Firewall, clique em PERMITIR.
echo  ==========================================
echo.
choice /c SN /m " Abrir o FTCam agora"
if not errorlevel 2 start "" "%PYW%" "%DEST%\app.py"
exit /b 0

:modelo
if exist "%DEST%\modelos\%2" exit /b 0
echo       Baixando %2...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/%1/%2' -OutFile '%DEST%\modelos\%2'"
exit /b 0

:achapy
set "PY="
where py >nul 2>nul && (py -3 --version >nul 2>nul && set "PY=py -3")
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY for %%P in ("%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe" "%ProgramData%\anaconda3\python.exe") do if not defined PY if exist %%P set "PY=%%~P"
exit /b 0
