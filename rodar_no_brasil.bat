@echo off
REM ============================================================
REM  Rodar no PC do Brasil (1a vez ou para renovar o seed/token)
REM  Fonte: https://soplay.com.br/filmes
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

REM --- Opcional: endpoint + token do F12 (Network > movies/vod/catalog) ---
REM set SOPLAY_API=https://gateway.nxtv.com.br/api/movies
REM set SOPLAY_TOKEN=Bearer eyJhbGciOi...

REM Testar streams (recomendado no Brasil):
set SOPLAY_CHECK=1

REM set SOPLAY_DEBUG=1
REM set SOPLAY_USER_ID=1078638

python gerar_soplay_filmes.py
if errorlevel 1 (
  echo.
  echo [ERRO] Se a API falhou:
  echo   1. Login em https://soplay.com.br/filmes
  echo   2. F12 ^> Network ^> filtre "movies", "vod", "catalog" ou "gateway"
  echo   3. Copie Request URL + header Authorization
  echo   4. Descomente e preencha SOPLAY_API / SOPLAY_TOKEN acima
  echo   5. Rode de novo
  echo.
  echo Mesmo sem API, o seed ja gera uma lista util (apos a 1a captura).
)

echo.
echo Saidas:
echo   playlists\soplay_filmes.m3u     ^<- use no SS IPTV / envie ao GitHub
echo   seed\soplay_filmes_seed.m3u     ^<- base automatica para o Actions
echo   estado_canais.json              ^<- contador das 3 falhas
pause
