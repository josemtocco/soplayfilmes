# SO Play Filmes M3U → SS IPTV (somente soplay.com.br/filmes)

Playlist gerada **exclusivamente** a partir do ecossistema **SO Play Filmes**
(`https://soplay.com.br/filmes`). Nenhuma lista publica de terceiros e usada.

Atualizacao automatica a cada **6 horas** no GitHub Actions.

## Fonte unica

| Item | Valor |
|------|--------|
| Site | https://soplay.com.br/filmes |
| CDN | `origin-fast.b-cdn.net` / `b-cdn.net` / `ssai.nxtv.com.br` |
| Identidade | `app_id=1&app_name=soplay&user_Id=...` |
| API (opcional) | `gateway.nxtv.com.br/api/movies` (ou vod/catalog) + Bearer token |

## O que o script faz

- Busca titulos de filmes **somente** do SoPlay (API oficial se houver token + seed)
- Regra das **3 falhas** (nao apaga item que ainda funciona)
- Categorias unificadas por genero (Acao, Comedia, Drama, Terror, Infantil...)
- Sem links duplicados
- `#EXTSIZE: medium` para botoes medios no SS IPTV
- Preserva e injeta `app_id` / `app_name` / `user_Id` nos streams

## Uso automatico no GitHub

1. Envie esta pasta para um repositorio.
2. Actions → rode o workflow uma vez.
3. Link da lista:
   ```
   https://raw.githubusercontent.com/SEU-USUARIO/SEU-REPO/main/playlists/soplay_filmes.m3u
   ```
4. No SS IPTV: Listas externas → cole o link.

O bot atualiza sozinho a cada 6h.

## Enriquecer o catalogo (Brasil)

### Opcao A — colar o JSON do F12 (mais simples)

1. Login em https://soplay.com.br/filmes
2. F12 → Network → filtre a resposta que traz a lista de filmes (JSON com `title`, `trailer`, `cover`...)
3. Clique na resposta → **Response** → copie o JSON inteiro
4. Cole em `seed/filmes_api.json` (substitua o conteudo)
5. Rode `python gerar_soplay_filmes.py` ou o `rodar_no_brasil.bat`
6. Commit de `seed/` + `playlists/`

O script le `trailer` como stream, `cover`/`logo` como imagem e `category_name` como grupo.

### Opcao B — token da API

1. Login em https://soplay.com.br/filmes
2. F12 → Network → copie a URL de `movies` / `vod` / `catalog` e o header `Authorization`
3. No `rodar_no_brasil.bat`:
   ```bat
   set SOPLAY_API=https://gateway.nxtv.com.br/api/movies
   set SOPLAY_TOKEN=Bearer eyJ...
   set SOPLAY_USER_ID=1078638
   set SOPLAY_CHECK=1
   ```
4. Rode o `.bat` e faca commit do `seed/` + `playlists/`.

## Identidade (user_Id)

```
?app_id=1&app_name=soplay&user_Id=1078638
```

| Variavel | Padrao |
|----------|--------|
| `SOPLAY_APP_ID` | `1` |
| `SOPLAY_APP_NAME` | `soplay` |
| `SOPLAY_USER_ID` | `1078638` |

Troque o `user_Id` pelo seu (aparece nos links do app apos o login).

## Estrutura

```
gerar_soplay_filmes.py
rodar_no_brasil.bat
seed/soplay_filmes_seed.m3u   # somente filmes SoPlay
playlists/soplay_filmes.m3u   # lista final
estado_canais.json
.github/workflows/update.yml  # cron 6h
```

## Aviso

Conteudo das emissoras e da plataforma SO Play. Uso pessoal. Respeite os termos do servico.
