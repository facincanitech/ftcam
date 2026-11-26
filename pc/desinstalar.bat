@echo off
title Desinstalar FTCam
set "DEST=%LOCALAPPDATA%\Programs\FTCam"
echo.
echo  Desinstalar o FTCam de %DEST%
choice /c SN /m " Tem certeza"
if errorlevel 2 exit /b 0
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { (($_.Name -like 'python*') -and ($_.CommandLine -match 'FTCam')) -or (($_.Name -like 'ffmpeg*') -and ($_.ExecutablePath -match 'FTCam')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
taskkill /f /im ffmpeg.exe >nul 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v FTCam /f >nul 2>nul
powershell -NoProfile -Command "Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'FTCam.lnk'),(Join-Path ([Environment]::GetFolderPath('Programs')) 'FTCam.lnk') -ErrorAction SilentlyContinue"
choice /c SN /m " Apagar tambem as GRAVACOES e configuracoes"
if errorlevel 2 (
  for %%D in (.venv modelos ui __pycache__) do rmdir /s /q "%DEST%\%%D" 2>nul
  del /q "%DEST%\*.py" "%DEST%\*.bat" "%DEST%\*.exe" "%DEST%\*.ico" "%DEST%\*.png" "%DEST%\*.txt" "%DEST%\*.md" 2>nul
  echo  Programa removido. Gravacoes e configuracoes mantidas em %DEST%
) else (
  rmdir /s /q "%DEST%"
  echo  Tudo removido.
)
pause
