@echo off
REM ============================================================
REM  Rodar no PC do Brasil (1a vez ou para renovar o seed/token)
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

REM --- Opcional: endpoint + token do F12 (Network > channels) ---
REM set SOPLAY_API=https://gateway.nxtv.com.br/api/channels
REM set SOPLAY_TOKEN=Bearer eyJhbGciOi...

REM Testar streams (recomendado no Brasil):
set SOPLAY_CHECK=1

REM set SOPLAY_DEBUG=1
REM set SOPLAY_USER_ID=1078638

python gerar_soplay.py
if errorlevel 1 (
  echo.
  echo [ERRO] Se a API falhou:
  echo   1. Login em https://soplay.com.br/canais
  echo   2. F12 ^> Network ^> filtre "channels" ou "gateway"
  echo   3. Copie Request URL + header Authorization
  echo   4. Descomente e preencha SOPLAY_API / SOPLAY_TOKEN acima
  echo   5. Rode de novo
  echo.
  echo Mesmo sem API, o seed + fontes publicas ja geram uma lista util.
)

echo.
echo Saidas:
echo   playlists\soplay.m3u     ^<- use no SS IPTV / envie ao GitHub
echo   seed\soplay_seed.m3u     ^<- base automatica para o Actions
echo   estado_canais.json       ^<- contador das 3 falhas
pause
