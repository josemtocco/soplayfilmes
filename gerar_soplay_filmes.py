#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerador AUTOMATICO de lista M3U SO Play Filmes / NxPlay para SS IPTV.

Roda 100% automatico no GitHub Actions (a cada 6h) sem secrets obrigatorios.
- Fonte unica: soplay.com.br/filmes (API oficial se houver token + seed)
- Regra das 3 falhas (estado_canais.json)
- Categorias unificadas (sinonimos de generos de filmes)
- Dedup por URL
- #EXTSIZE: medium (botoes medios no SS IPTV)
- Limpeza de tokens efemeros
- Auto-atualiza o seed quando encontra titulos novos

Secrets opcionais (melhoram a captura):
  SOPLAY_API    URL real do endpoint (ex: https://gateway.nxtv.com.br/api/...)
  SOPLAY_TOKEN  Bearer token capturado no F12 apos login no SoPlay
  SOPLAY_CHECK  "1" = testar liveness (use so no Brasil)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error, parse

# --------------------------------------------------------------------------
BASE_SITE  = "https://soplay.com.br"
OUT_DIR    = "playlists"
OUT_FILE   = os.path.join(OUT_DIR, "soplay_filmes.m3u")
STATE_FILE = "estado_canais.json"
SEED_FILE  = os.path.join("seed", "soplay_filmes_seed.m3u")
JSON_DUMP  = os.path.join("seed", "filmes_api.json")  # cole aqui o JSON do F12
DEBUG_DIR  = "debug_soplay_filmes"

MAX_FALHAS = 3
TIMEOUT    = 22
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEBUG = os.environ.get("SOPLAY_DEBUG") == "1"
CHECK = os.environ.get("SOPLAY_CHECK") == "1"

# SOMENTE fonte SoPlay Filmes (soplay.com.br/filmes)
# Nenhuma lista publica externa e usada.


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Categorias (generos de filmes + genericos)
# --------------------------------------------------------------------------
SINONIMOS = [
    ("Acao", ["acao", "action", "aventura", "adventure", "acao e aventura",
              "action adventure"]),
    ("Comedia", ["comedia", "comedy", "comico", "humor", "sitcom"]),
    ("Drama", ["drama", "dramas"]),
    ("Romance", ["romance", "romantico", "romanticos", "love"]),
    ("Terror", ["terror", "horror", "suspense", "thriller", "medo"]),
    ("Ficcao", ["ficcao", "ficcao cientifica", "sci-fi", "scifi",
                "science fiction", "fantasia", "fantasy"]),
    ("Documentario", ["documentario", "documentarios", "docs", "doc",
                      "documentary", "documentaries", "biografia", "biography"]),
    ("Infantil", ["infantil", "infantis", "kids", "kid", "crianca", "criancas",
                  "familia", "family", "animacao", "animacoes", "animation",
                  "cartoon", "desenhos"]),
    ("Policial", ["policial", "crime", "investigacao", "detective"]),
    ("Guerra", ["guerra", "war", "militar"]),
    ("Musical", ["musical", "musica", "music"]),
    ("Western", ["western", "faroeste"]),
    ("Filmes", ["filmes", "filme", "cinema", "movies", "movie", "cinemas",
                "so movies", "film plus", "filmes 24h", "moviesphere", "vod"]),
    ("Series", ["series", "serie", "seriados", "seriado", "tv shows", "shows"]),
    ("Adulto", ["adulto", "adultos", "+18", "18+", "xxx", "adult"]),
    ("Outros", ["outros", "diversos", "geral", "undefined", "variedades"]),
]


def _slug(texto: str) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"[|/\\\-_,.;:]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


_MAPA_SIN: dict[str, str] = {}
for _canon, _alts in SINONIMOS:
    for _a in _alts:
        _MAPA_SIN[_slug(_a)] = _canon
_CACHE_CAT: dict[str, str] = {}


def normalizar_categoria(bruta: str) -> str:
    s = _slug(bruta)
    if not s:
        return "Outros"
    if s in _MAPA_SIN:
        return _MAPA_SIN[s]
    for palavra, canon in _MAPA_SIN.items():
        if re.search(r"\b" + re.escape(palavra) + r"\b", s):
            return canon
    if s not in _CACHE_CAT:
        _CACHE_CAT[s] = " ".join(w.capitalize() for w in s.split())
    return _CACHE_CAT[s]


# Params de autenticacao/identidade do SoPlay que DEVEM ser preservados
_KEEP_QS = {"app_id", "app_name", "user_id", "userid", "user_Id"}

# Valores padrao injetados em streams origin-fast.b-cdn.net (e similares)
DEFAULT_APP_ID = os.environ.get("SOPLAY_APP_ID", "1")
DEFAULT_APP_NAME = os.environ.get("SOPLAY_APP_NAME", "soplay")
DEFAULT_USER_ID = os.environ.get("SOPLAY_USER_ID", "1078638")


def limpar_url(url: str) -> str:
    """Remove tokens efemeros, mas PRESERVA app_id/app_name/user_Id.
    Em CDNs do SoPlay (origin-fast.b-cdn.net, etc.) injeta os params se faltarem.
    """
    if not url:
        return url
    try:
        p = parse.urlparse(url)
        qs = parse.parse_qs(p.query, keep_blank_values=False)
        for k in list(qs.keys()):
            kl = k.lower()
            if kl in _KEEP_QS:
                continue
            if kl in ("token", "auth", "authorization", "session", "sessionid",
                      "bpkio_sessionid", "bpkio_serviceid", "mm_sp", "category",
                      "signature", "expires", "exp", "sig", "jwt", "access_token"):
                qs.pop(k, None)

        host = (p.netloc or "").lower()
        # Bunny Stream VOD (vz-XXXX.b-cdn.net): NAO injetar app_id —
        # query na master m3u8 quebra resolucao relativa e pode silenciar audio
        is_bunny_vod = host.startswith("vz-") and host.endswith(".b-cdn.net")
        # Canais live SoPlay: injeta identidade
        is_soplay_live = (not is_bunny_vod) and any(h in host for h in (
            "origin-fast.b-cdn.net",
            "fast-channels.b-cdn.net",
            "cdn-fast.nxtv.com.br",
            "nxplay.com.br",
            "ssai.nxtv.com.br",
            "edge-proxy.nxtv.com.br",
            "edge-proxy-mediatailor",
        ))
        if is_bunny_vod:
            # remove params de identidade se existirem
            for k in list(qs.keys()):
                if k.lower() in ("app_id", "app_name", "user_id", "userid", "user_Id"):
                    qs.pop(k, None)
        elif is_soplay_live:
            has_app = any(k.lower() == "app_id" for k in qs)
            has_name = any(k.lower() == "app_name" for k in qs)
            has_user = any(k.lower() in ("user_id", "userid") for k in qs)
            if not has_app:
                qs["app_id"] = [DEFAULT_APP_ID]
            if not has_name:
                qs["app_name"] = [DEFAULT_APP_NAME]
            if not has_user:
                qs["user_Id"] = [DEFAULT_USER_ID]

        # reconstruir query mantendo user_Id com capitalizacao original se existir
        pairs = []
        for k, vals in qs.items():
            pairs.append((k, vals[0]))
        new_q = parse.urlencode(pairs)
        cleaned = parse.urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, ""))
        if ".m3u8" not in cleaned and ".m3u8" in url:
            return url
        return cleaned.rstrip("?")
    except Exception:
        return url


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _headers(extra: dict | None = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Origin": BASE_SITE,
        "Referer": BASE_SITE + "/filmes",
    }
    tok = os.environ.get("SOPLAY_TOKEN")
    if tok:
        h["Authorization"] = tok if tok.lower().startswith("bearer") else "Bearer " + tok
    if extra:
        h.update(extra)
    return h


def http_get(url: str, headers: dict | None = None) -> str:
    req = request.Request(url, headers=headers or _headers())
    with request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _dump_debug(nome: str, conteudo) -> None:
    if not DEBUG:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, nome), "w", encoding="utf-8") as f:
        f.write(conteudo if isinstance(conteudo, str)
                else json.dumps(conteudo, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# Parseadores
# --------------------------------------------------------------------------
CAMPO_NOME = ("name", "title", "nome", "channel_name", "titulo", "label",
              "channelName", "movie_name", "movieName", "content_name",
              "contentName", "original_title", "originalTitle")
CAMPO_URL  = ("url_hls", "url_dash", "url", "stream", "stream_url", "streamUrl",
              "link", "source", "src", "hls", "m3u8", "playback_url", "playbackUrl",
              "file", "hls_url", "hlsUrl", "manifest", "playlist", "video_url",
              "videoUrl", "media_url", "mediaUrl", "play_url", "playUrl", "trailer",
              "video", "playback", "content_url", "contentUrl")
CAMPO_GRUPO = ("category_name", "categoryName", "group", "group_title", "groupTitle",
               "category", "categoria", "genre", "genero", "grupo", "section",
               "type", "tag", "genres", "genre_name")
CAMPO_LOGO = ("cover", "logo", "image", "img", "icon", "thumbnail", "thumb", "poster",
              "logo_url", "tvg_logo", "picture", "image_url", "imageUrl",
              "poster_url", "posterUrl", "backdrop", "banner", "banner_mobile")


def _primeiro(d: dict, chaves) -> str:
    for k in chaves:
        if isinstance(d, dict) and d.get(k):
            v = d.get(k)
            if isinstance(v, (str, int, float)):
                return str(v).strip()
            if isinstance(v, list) and v:
                # generos podem vir como lista
                if isinstance(v[0], str):
                    return str(v[0]).strip()
                if isinstance(v[0], dict):
                    inner = _primeiro(v[0], CAMPO_NOME + CAMPO_GRUPO)
                    if inner:
                        return inner
            if isinstance(v, dict):
                inner = _primeiro(v, CAMPO_URL + CAMPO_NOME)
                if inner:
                    return inner
    return ""


def _achar_lista_canais(obj) -> list:
    achados = []

    def parece(d):
        return isinstance(d, dict) and _primeiro(d, CAMPO_URL) and _primeiro(d, CAMPO_NOME)

    def walk(o, depth=0):
        if depth > 14:
            return
        if isinstance(o, list):
            if o and sum(1 for x in o if parece(x)) >= max(1, len(o) // 4):
                achados.extend(x for x in o if parece(x))
            for x in o:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)

    walk(obj)
    return achados


def _parse_m3u(texto: str) -> list[dict]:
    canais = []
    nome = grupo = logo = ""
    duration = 0
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', linha, re.I)
            grupo = m.group(1) if m else ""
            m = re.search(r'tvg-logo="([^"]*)"', linha, re.I)
            logo = m.group(1) if m else ""
            m = re.search(r'#EXTINF:(-?\d+)', linha)
            try:
                duration = int(m.group(1)) if m else 0
            except (TypeError, ValueError):
                duration = 0
            if duration < 0:
                duration = 0
            nome = linha.split(",", 1)[-1].strip()
        elif linha and not linha.startswith("#"):
            canais.append({"nome": nome, "url": linha, "grupo": grupo,
                           "logo": logo, "duration": duration})
            nome = grupo = logo = ""
            duration = 0
    return canais


# Endpoints candidatos para catalogo de filmes / VOD
# /api/movies retorna todas as categorias com content[] (token obrigatorio)
CANDIDATOS_API = [
    "https://gateway.nxtv.com.br/api/movies",
    "https://gateway.nxtv.com.br/api/movies/category/2215",
    "https://gateway.nxtv.com.br/api/movies/category/2368",
    "https://gateway.nxtv.com.br/api/movies/category/2206",
    "https://gateway.nxtv.com.br/api/movies/category/2210",
    "https://gateway.nxtv.com.br/api/movies/category/2209",
    "https://gateway.nxtv.com.br/api/movies/category/2359",
    "https://gateway.nxtv.com.br/api/movies/category/2208",
    "https://gateway.nxtv.com.br/api/movies/category/2211",
    "https://gateway.nxtv.com.br/api/movies/category/2213",
    "https://gateway.nxtv.com.br/api/movies/category/2214",
    "https://gateway.nxtv.com.br/api/movies/category/2207",
    "https://gateway.nxtv.com.br/api/v1/movies",
    "https://gateway.nxtv.com.br/api/vod",
    "https://soplay.com.br/api/movies",
    "https://soplay.com.br/api/filmes",
]



def carregar_json_dump() -> list[dict]:
    """Lê seed/filmes_api.json (cole o JSON do F12 Network > Response)."""
    if not os.path.exists(JSON_DUMP):
        return []
    try:
        with open(JSON_DUMP, encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return []
        obj = json.loads(raw)
        lista = _achar_lista_canais(obj) if not isinstance(obj, list) else [
            x for x in obj if isinstance(x, dict) and (
                _primeiro(x, CAMPO_URL) and _primeiro(x, CAMPO_NOME)
            )
        ]
        # se lista vazia mas obj é lista de dicts com trailer/title
        if not lista and isinstance(obj, list):
            for it in obj:
                if not isinstance(it, dict):
                    continue
                nome = _primeiro(it, CAMPO_NOME)
                url = _primeiro(it, CAMPO_URL)
                if nome and url:
                    lista.append(it)
        brutos = []
        for it in lista:
            brutos.append({
                "nome":  _primeiro(it, CAMPO_NOME),
                "url":   _primeiro(it, CAMPO_URL),
                "grupo": _primeiro(it, CAMPO_GRUPO) or "Filmes",
                "logo":  _primeiro(it, CAMPO_LOGO),
                "duration": _duracao_segundos(it),
            })
        log(f"  [json] {len(brutos)} itens em {JSON_DUMP}")
        return brutos
    except Exception as e:
        log(f"  [json] erro lendo {JSON_DUMP}: {e}")
        return []


def carregar_seed() -> list[dict]:
    if not os.path.exists(SEED_FILE):
        return []
    try:
        with open(SEED_FILE, encoding="utf-8") as f:
            lista = _parse_m3u(f.read())
        log(f"  [seed] {len(lista)} itens em {SEED_FILE}")
        return lista
    except Exception as e:
        log(f"  [seed] erro: {e}")
        return []



def _duracao_segundos(it: dict) -> int:
    """Converte video_duration da API (minutos) ou valor ja em segundos para int > 0."""
    raw = it.get("video_duration") or it.get("duration") or it.get("duracao") or 0
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    # API SoPlay manda minutos (ex: 95). Se vier > 600, assume ja ser segundos.
    if v <= 600:
        return v * 60
    return v


def _fmt_duracao(segundos: int) -> str:
    """Formata segundos como 1h35min ou 45min."""
    try:
        s = int(segundos)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h > 0 and m > 0:
        return f"{h}h{m:02d}min"
    if h > 0:
        return f"{h}h"
    return f"{m}min"


def _extrair_filmes_api(obj) -> list[dict]:
    """Extrai filmes do formato SoPlay/NxTV (lista plana ou categorias com content[])."""
    brutos: list[dict] = []

    def add(it: dict, grupo_fallback: str = "") -> None:
        if not isinstance(it, dict):
            return
        nome = _primeiro(it, CAMPO_NOME)
        url = _primeiro(it, CAMPO_URL)
        mid = it.get("id")
        if not nome:
            return
        grupo = _primeiro(it, CAMPO_GRUPO) or grupo_fallback or "Filmes"
        logo = _primeiro(it, CAMPO_LOGO)
        brutos.append({
            "nome": nome,
            "url": url or "",
            "grupo": grupo,
            "logo": logo,
            "id": mid,
            "duration": _duracao_segundos(it),
        })

    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "content" in obj[0]:
            for cat in obj:
                label = (cat.get("cat_label") or cat.get("category_name")
                         or cat.get("name") or "")
                for it in (cat.get("content") or []):
                    add(it, label)
        else:
            for it in obj:
                add(it)
    elif isinstance(obj, dict):
        for key in ("content", "data", "movies", "items", "results"):
            if isinstance(obj.get(key), list):
                return _extrair_filmes_api(obj[key])
        for it in _achar_lista_canais(obj):
            add(it)
    return brutos


def _detalhe_filme(movie_id) -> dict | None:
    """Busca /api/movie/{id} para obter url_hls completo."""
    if not movie_id:
        return None
    url = f"https://gateway.nxtv.com.br/api/movie/{movie_id}"
    try:
        dados = http_get(url)
        obj = json.loads(dados)
        if isinstance(obj, dict) and obj.get("id"):
            return obj
    except Exception as e:
        log(f"  [detail] falhou movie/{movie_id}: {e}")
    return None


def coletar_api() -> list[dict]:
    """API autenticada: /api/movies (catalogo) + /api/movie/{id} (url_hls)."""
    api = os.environ.get("SOPLAY_API")
    urls = ([api] if api else []) + CANDIDATOS_API
    brutos: list[dict] = []
    ids_vistos: set = set()

    for url in urls:
        if not url:
            continue
        try:
            dados = http_get(url)
        except Exception as e:
            log(f"  [api] falhou {url} -> {e}")
            continue
        _dump_debug("api_" + _slug(url)[:40] + ".txt", dados)
        try:
            obj = json.loads(dados)
            lista = _extrair_filmes_api(obj)
            if lista:
                log(f"  [api] {len(lista)} entradas em {url}")
                # indices por id para preferir categoria de genero
                por_id = {b.get("id"): i for i, b in enumerate(brutos) if b.get("id") is not None}
                genericas = {"recomendados", "mais assistidos da semana", "mais assistidos",
                             "destaques", "lancamentos", "filmes"}
                for it in lista:
                    mid = it.get("id")
                    if mid is not None and mid in por_id:
                        # troca grupo se o novo for mais especifico
                        prev = brutos[por_id[mid]]
                        g_new = _slug(it.get("grupo") or "")
                        g_old = _slug(prev.get("grupo") or "")
                        if g_old in genericas and g_new not in genericas:
                            prev["grupo"] = it.get("grupo") or prev["grupo"]
                        continue
                    if mid is not None:
                        ids_vistos.add(mid)
                        por_id[mid] = len(brutos)
                    brutos.append(it)
                if "/api/movies" in url and "category" not in url:
                    break
                continue
        except (ValueError, json.JSONDecodeError):
            pass
        if "#EXTINF" in dados:
            lista = _parse_m3u(dados)
            if lista:
                log(f"  [api] {len(lista)} itens M3U em {url}")
                brutos.extend(lista)
                break

    # Completa url_hls via /api/movie/{id} para quem nao tem stream na lista
    faltando = [b for b in brutos if b.get("id") and not (b.get("url") or "").startswith("http")]
    com_url = [b for b in brutos if (b.get("url") or "").startswith("http")]
    log(f"  [api] com stream na lista: {len(com_url)} | precisando detail: {len(faltando)}")

    if faltando:
        resolvidos = 0
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(_detalhe_filme, b["id"]): b for b in faltando}
            for f in as_completed(futs):
                base = futs[f]
                det = f.result()
                if not det:
                    continue
                url = _primeiro(det, CAMPO_URL)
                if not url:
                    continue
                base["url"] = url
                if not base.get("logo"):
                    base["logo"] = _primeiro(det, CAMPO_LOGO)
                if not base.get("grupo") or base["grupo"] == "Filmes":
                    g = _primeiro(det, CAMPO_GRUPO)
                    if g:
                        base["grupo"] = g
                dur = _duracao_segundos(det)
                if dur > 0:
                    base["duration"] = dur
                resolvidos += 1
        log(f"  [api] details resolvidos: {resolvidos}/{len(faltando)}")

    # descarta quem ainda nao tem url
    return [b for b in brutos if (b.get("url") or "").startswith("http")]



def coletar_tudo() -> list[dict]:
    """Somente SoPlay Filmes: API oficial (se token) + seed capturado do soplay.com.br/filmes.
    Nao usa listas publicas de terceiros.
    """
    brutos: list[dict] = []
    brutos.extend(coletar_api())
    brutos.extend(carregar_seed())
    if not brutos:
        raise RuntimeError(
            "Nenhum titulo de filme do SoPlay encontrado. "
            "1) Rode rodar_no_brasil.bat apos login em soplay.com.br/filmes "
            "2) Ou defina SOPLAY_API + SOPLAY_TOKEN (F12 > Network). "
            "O seed em seed/soplay_filmes_seed.m3u tambem deve existir."
        )
    return brutos


def stream_vivo(url: str) -> bool:
    try:
        req = request.Request(url, headers=_headers({"Range": "bytes=0-1"}))
        with request.urlopen(req, timeout=12) as r:
            return 200 <= r.status < 400
    except error.HTTPError as e:
        return e.code in (200, 206, 301, 302, 403)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
def carregar_estado() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("canais", {})
            return st
        except Exception as e:
            log(f"  [estado] corrompido, recomecando ({e})")
    return {"canais": {}, "atualizado_em": ""}


def salvar_estado(st: dict) -> None:
    st["atualizado_em"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------
def escrever_m3u(canais: list[dict], path: str = OUT_FILE, header_extra: list | None = None) -> dict:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    por_cat: dict[str, list] = {}
    for c in canais:
        por_cat.setdefault(c["grupo"], []).append(c)

    linhas = ["#EXTM3U"]
    if header_extra:
        linhas.extend(header_extra)
    else:
        # Header SS IPTV: size=medium nos tiles; itens usam type="video"
        linhas[0] = '#EXTM3U size="medium"'
        linhas += [
            "#PLAYLIST: SO Play Filmes auto (SS IPTV)",
            f"#UPDATED: {dt.datetime.now().isoformat(timespec='seconds')}",
            "#EXTSIZE: medium",
        ]
    for cat in sorted(por_cat, key=lambda x: _slug(x)):
        for c in sorted(por_cat[cat], key=lambda x: _slug(x["nome"])):
            logo = f' tvg-logo="{c["logo"]}"' if c.get("logo") else ""
            try:
                dur = int(c.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            # SS IPTV: type="video" habilita pausa / seek / barra de progresso
            # Duracao em segundos no EXTINF; nome exibe horas e minutos
            ext = dur if dur > 0 else 0
            nome_base = c["nome"]
            nome_limpo = re.sub(r"\s*\(\d+h\d{0,2}min\)$", "", nome_base)
            nome_limpo = re.sub(r"\s*\(\d+min\)$", "", nome_limpo).strip()
            rotulo = _fmt_duracao(dur)
            nome_exib = f"{nome_limpo} ({rotulo})" if rotulo else nome_limpo
            desc = f' description="{rotulo}"' if rotulo else ""
            # audio-track=por: SS IPTV prioriza faixa em portugues quando houver multi-audio
            linhas.append(
                f'#EXTINF:{ext} tvg-name="{nome_limpo}"{logo} '
                f'type="video" audio-track="por"{desc} group-title="{cat}",{nome_exib}'
            )
            linhas.append(c["url"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    return por_cat


def atualizar_seed(ativos: list[dict]) -> None:
    """Regrava o seed priorizando streams com identidade SoPlay (origin-fast / user_Id)."""
    bons = [c for c in ativos if c.get("falhas", 0) == 0] or ativos
    if not bons:
        return
    # preferir URLs do ecossistema SoPlay autenticado
    prefer = [c for c in bons if (
            "user_Id=" in c.get("url","")
            or "app_name=soplay" in c.get("url","")
            or "origin-fast.b-cdn.net" in c.get("url","")
            or "ssai.nxtv.com.br" in c.get("url","")
            or "edge-proxy" in c.get("url","")
            or "b-cdn.net" in c.get("url","")
        )]
    if len(prefer) >= 5:
        bons = prefer
    os.makedirs(os.path.dirname(SEED_FILE), exist_ok=True)
    escrever_m3u(
        [{"nome": c["nome"], "url": c["url"], "grupo": c["grupo"],
          "logo": c.get("logo", ""), "duration": c.get("duration", 0)} for c in bons],
        path=SEED_FILE,
        header_extra=["#EXTM3U", "# SEED auto-atualizado (prioriza origin-fast + user_Id) - FILMES"],
    )
    log(f"  [seed] atualizado com {len(bons)} itens -> {SEED_FILE}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    inicio = time.time()
    log("=" * 64)
    log("SO Play FILMES M3U auto -> SS IPTV")
    log(f"Inicio: {dt.datetime.now().isoformat(timespec='seconds')}")
    log(f"Fonte: {BASE_SITE}/filmes")
    log(f"Liveness (SOPLAY_CHECK): {'sim' if CHECK else 'nao'}")
    log(f"Seed: {SEED_FILE} ({'ok' if os.path.exists(SEED_FILE) else 'ausente'})")
    log(f"Token: {'sim' if os.environ.get('SOPLAY_TOKEN') else 'nao'}")
    log("=" * 64)

    estado = carregar_estado()
    canais_estado = estado["canais"]

    try:
        brutos = coletar_tudo()
    except Exception as e:
        log(f"\n[AVISO] Fontes inacessiveis: {e}")
        log("[AVISO] Preservando playlist anterior (sem penalizar itens).")
        ativos = [c for c in canais_estado.values() if c.get("falhas", 0) < MAX_FALHAS]
        if ativos:
            escrever_m3u(ativos)
            log(f"[AVISO] Playlist preservada: {len(ativos)} itens.")
        salvar_estado(estado)
        return 0

    log(f"\nBrutos coletados: {len(brutos)}")

    # dedup + normalizacao
    vistos: set[str] = set()
    atuais: dict[str, dict] = {}
    dups = 0
    for b in brutos:
        raw = (b.get("url") or "").strip()
        if not raw.startswith("http"):
            continue
        url = limpar_url(raw)
        nome = (b.get("nome") or "").strip() or "Sem nome"
        if url in vistos:
            dups += 1
            continue
        vistos.add(url)
        # se grupo vazio, tenta inferir pelo nome
        grupo_raw = b.get("grupo") or nome
        dur = 0
        try:
            dur = int(b.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        atuais[url] = {
            "nome":  nome,
            "url":   url,
            "grupo": normalizar_categoria(grupo_raw),
            "logo":  (b.get("logo") or "").strip(),
            "duration": dur if dur > 0 else 0,
        }
    log(f"Unicos apos dedup: {len(atuais)} ({dups} dups)")

    funcionando = dict(atuais)
    if CHECK and atuais:
        log("Testando liveness...")
        mortos = []
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = {ex.submit(stream_vivo, u): u for u in atuais}
            for f in as_completed(futs):
                u = futs[f]
                if not f.result():
                    mortos.append(u)
        for u in mortos:
            funcionando.pop(u, None)
        log(f"  vivos={len(funcionando)} mortos={len(mortos)}")

    # reconciliacao 3 falhas
    agora = dt.datetime.now().isoformat(timespec="seconds")
    novos = reativados = 0
    for url, c in atuais.items():
        if url in funcionando:
            if url not in canais_estado:
                novos += 1
            elif canais_estado[url].get("falhas", 0) > 0:
                reativados += 1
            canais_estado[url] = {**c, "falhas": 0, "visto_em": agora}
        else:
            prev = canais_estado.get(url, {**c, "falhas": 0})
            prev.update({k: c[k] for k in ("nome", "grupo", "logo", "url", "duration") if k in c})
            prev["falhas"] = prev.get("falhas", 0) + 1
            canais_estado[url] = prev

    sumiram = [u for u in list(canais_estado) if u not in atuais]
    for u in sumiram:
        canais_estado[u]["falhas"] = canais_estado[u].get("falhas", 0) + 1

    removidos = [u for u, c in canais_estado.items() if c.get("falhas", 0) >= MAX_FALHAS]
    for u in removidos:
        canais_estado.pop(u, None)

    ativos = [c for c in canais_estado.values() if c.get("falhas", 0) < MAX_FALHAS]
    por_cat = escrever_m3u(ativos)
    salvar_estado(estado)

    # auto-atualiza seed com itens que estao ok
    atualizar_seed(ativos)

    log("\n" + "-" * 64)
    log(f"Novos:        {novos}")
    log(f"Reativados:   {reativados}")
    log(f"Sumiram agora:{len(sumiram)}")
    log(f"Removidos 3x: {len(removidos)}")
    log(f"ATIVOS:       {len(ativos)}")
    log(f"Categorias ({len(por_cat)}):")
    for cat in sorted(por_cat, key=lambda x: _slug(x)):
        log(f"  - {cat}: {len(por_cat[cat])}")
    log(f"Arquivo: {OUT_FILE}")
    log(f"Tempo: {time.time() - inicio:.1f}s")
    log("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
