#!/usr/bin/env python3
"""
Scraper dlpsgame.com - Category PS5
-----------------------------------
Crawl toutes les pages de https://dlpsgame.com/category/ps5/,
visite chaque page de jeu, décode les payloads base64 des div.secure-data,
suit les redirections des liens downloadgameps3.net pour obtenir les URLs
directes vers les hébergeurs (akirabox, vikingfile, datanodes, filekeeper,
datavaults, etc.) et produit un JSON au format catalogue Pegasus DL.

Le JSON de sortie respecte la structure attendue par le projet
pegasus-ps5/pegasus-dl et par le script
https://pippo26442999.github.io/.exFAT/script.js (convertSingleGame).

Usage:
    python scrape_dlpsgame.py [--max-pages N] [--max-games N] [--out PATH]
                              [--concurrency N] [--verbose]
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from formats import detect_formats
from sizes import extract_size

# ---------------------------------------------------------------------------
# Configuration générale
# ---------------------------------------------------------------------------

BASE_URL = "https://dlpsgame.com"
PS5_CATEGORY_URL = f"{BASE_URL}/category/ps5/"
PS5_LIST_URL = f"{BASE_URL}/list-game-ps5/"
# Provenance écrite dans chaque package (traçabilité des sources lors d'une fusion).
SITE_SOURCE = "dlpsgame.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

REQUEST_TIMEOUT = 30  # secondes (mode curl)
PAGE_DELAY = 0.5  # délai entre 2 requêtes de pages de jeu (polit de scraping)
HTTP_RETRIES = 5  # nombre de retry sur 429/503/timeout

# FlareSolverr a besoin de plus de temps : Chrome doit charger la page,
# résoudre le challenge Cloudflare, et attendre que le JS rende les spoilers.
FS_REQUEST_TIMEOUT = 90  # secondes par requête FlareSolverr
FS_MAX_TIMEOUT = 90000   # millisecondes (90s) passé à FlareSolverr

# FlareSolverr NE SAIT PAS attendre un sélecteur CSS (waitForSelector n'existe
# pas dans son API). Le seul levier supporté pour laisser le JS de la page
# injecter les spoilers .secure-data / .su-spoiler est `waitInSeconds` : un
# délai fixe appliqué APRÈS résolution du challenge, AVANT de renvoyer le HTML.
FS_WAIT_SECONDS = 8  # délai de rendu JS pour les pages de jeu

# Cache disque pour reprendre après timeout/erreur sans tout rescrap.
# Stocke le HTML de chaque page de jeu déjà scrapée avec succès.
DISK_CACHE_DIR = Path(".scrape_cache")
DISK_CACHE_ENABLED = True  # activé par défaut, --no-cache pour désactiver

# Cache global pour éviter de résoudre plusieurs fois le même
# downloadgameps3.net/archives/{id} (plusieurs jeux peuvent partager le même ID,
# ou un même jeu peut avoir plusieurs liens pointant vers la même page de redirection).
_RESOLVE_CACHE: dict[str, str | None] = {}

# On utilise curl en subprocess car le site est protégé par Cloudflare
# avec TLS fingerprinting (JA3) : requests/Python est bloqué (403), mais
# curl (avec sa signature TLS OpenSSL native) passe sans problème.
# curl est disponible en standard sur ubuntu-latest (GitHub Actions).
CURL_BIN = shutil.which("curl") or "curl"

# ---------------------------------------------------------------------------
# Mapping hébergeurs → nom "miroir" affiché dans le JSON
# ---------------------------------------------------------------------------
# Inspiré du script.js (mirrors: akia, viki, buzz, data, filek, vault)
# On y ajoute les hébergeurs secondaires vus sur dlpsgame.com.
MIRROR_PATTERNS = [
    # (pattern dans l'URL, nom affiché)
    ("akirabox", "Akia"),
    ("vikingfile", "Viki"),
    ("datanodes.to", "Data"),
    ("filekeeper", "Filek"),
    ("datavaults", "Vault"),
    ("buzzheavier", "Buzz"),
    ("1fichier", "1File"),
    ("mediafire", "Mediafire"),
    ("rootz.so", "Rootz"),
    ("gofile", "Gofile"),
    ("1cloudfile", "1Cloud"),
    ("mega.nz", "Mega"),
    ("mega.co.nz", "Mega"),
]

# Groupes reconnus (cf. convertSingleGame dans script.js)
# - "files" = groupe par défaut (liens divers)
# - "standard" / "backport" / "dlc" / "dump" / "exFAT"
def detect_group(label_text: str) -> str:
    t = label_text.lower()
    if "exfat" in t:
        return "exFAT"
    if "backport" in t:
        return "Backport"
    if "dlc" in t:
        return "DLC"
    if "dump" in t:
        return "Dump"
    if "standard" in t or "update" in t:
        return "Standard"
    return "Standard"


# Les URLs passant par ces domaines sont des pages de redirection :
# il faut suivre la 302/301 ou scraper le HTML pour récupérer l'URL directe
# de l'hébergeur (akirabox, vikingfile, datanodes, etc.).
REDIRECT_HOSTS = (
    "downloadgameps3.net",
    "downloadgameps3.com",
    "dlpsgame.com",
)

# Sur downloadgameps3.net, les liens sont obfqués en JavaScript via deux
# attributs data-* qu'il faut concaténer : data-domain + data-path.
# Exemple : data-domain="https://akirabox." data-path="com/abc/file"
#         → https://akirabox.com/abc/file
SECURE_LNK_RE = re.compile(
    r'<a[^>]*class=["\'][^"\']*secure-lnk[^"\']*["\'][^>]*'
    r'data-domain=["\']([^"\']+)["\'][^>]*'
    r'data-path=["\']([^"\']+)["\'][^>]*>([^<]+)</a>',
    re.IGNORECASE | re.DOTALL,
)


# Mots-clés de liens à ignorer (Guide/Tool/DMCA/contacts, etc.)
IGNORED_LINK_TEXTS = {
    "guide download",
    "tool download",
    "guide download game",
    "tool download°",
    "guide download°",
    "dmca",
    "guide",
    "tool",
    "download",          # trop générique
    "here",
    "click",
    "click here",
    "link",
    "this link",
    "download here",
    "more info",
    "read more",
    "pre-db",
    "predb",
}

# Hôtes qu'on ne veut jamais garder comme URL finale (non-hébergeurs)
# On compare le hostname parsé (pas substring de l'URL complète) pour éviter
# les faux positifs comme "x.com" qui match "akirabox.com".
NON_HOST_HOSTS = {
    "api.predb.net",
    "predb.org",
    "predb.me",
    "www.google.com",
    "google.com",
    "www.facebook.com",
    "facebook.com",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "discord.gg",
    "discord.com",
    "www.discord.com",
    "t.me",
    "reddit.com",
    "www.reddit.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "wikipedia.org",
    "dlpsgame.com",
    "downloadgameps3.com",
    "downloadgameps3.net",
    "ad.a-ads.com",
    "pagead2.googlesyndication.com",
}


def get_hostname(url: str) -> str:
    """Extrait le hostname en minuscules (sans port)."""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_non_host_url(url: str) -> bool:
    """True si l'URL pointe vers un domaine non-hébergeur (social, etc.)."""
    host = get_hostname(url)
    if not host:
        return True  # URL invalide → on exclut
    return host in NON_HOST_HOSTS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("dlpsgame-scraper")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
# Deux backends disponibles :
#   1. "curl"          — rapide, marche en local (IP résidentielle non bloquée
#                        par Cloudflare)
#   2. "flaresolverr"  — proxy Docker qui route TOUTES les requêtes via un vrai
#                        Chrome non-headless. Requis sur GitHub Actions car les
#                        IP datacenter sont bloquées par Cloudflare (403).
#                        Utilise une session persistante FlareSolverr pour
#                        éviter de relancer Chrome à chaque requête.
#
# NOTE : On ne peut PAS récupérer les cookies cf_clearance via FlareSolverr
# puis les réutiliser avec curl, car Cloudflare valide aussi l'empreinte TLS
# (JA3/JA4). curl a une signature TLS différente de Chrome, donc la requête
# est rejetée (403) même avec les bons cookies + User-Agent.

# Backend HTTP actif (défini par --http-backend)
_HTTP_BACKEND: str = "curl"

# URL du FlareSolverr (par défaut : localhost:8191)
FLARESOLVERR_URL = "http://localhost:8191/v1"

# Session FlareSolverr persistante (garde Chrome ouvert avec les cookies CF)
_FS_SESSION_ID: str | None = None

# Pool multi-instances FlareSolverr (chemin de FALLBACK HTML uniquement).
# Renseigné par init_flaresolverr_session() quand FLARESOLVERR_URLS contient
# plusieurs URLs (round-robin sur N conteneurs Docker → N× débit). Avec une
# seule URL, on reste sur la session unique historique (_FS_SESSION_ID) pour
# garantir un comportement strictement identique à aujourd'hui.
_FS_POOL = None  # type: "FlareSolverrPool | None"


class CurlResponse:
    """Wrapper léger pour ressembler à requests.Response."""
    def __init__(self, status_code: int, text: str, final_url: str):
        self.status_code = status_code
        self.text = text
        self.url = final_url


def _curl_get(url: str, *, follow_redirects: bool = True, max_time: int | None = None) -> CurlResponse:
    """Exécute curl en subprocess et retourne (status, body, final_url).

    curl est utilisé pour le backend local car sa signature TLS OpenSSL passe
    Cloudflare JA3 quand l'IP n'est pas en liste noire."""
    timeout = max_time or REQUEST_TIMEOUT
    cmd = [
        CURL_BIN,
        "--silent", "--show-error",
        "--compressed",                    # gère gzip/deflate/br automatiquement
        "--max-time", str(timeout),
        "--connect-timeout", "15",
        "-A", USER_AGENT,
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Cache-Control: no-cache",
        "-w", "\n__CURL_META__\n%{http_code}\n%{url_effective}",
    ]
    if follow_redirects:
        cmd += ["--location", "--max-redirs", "10"]
    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    out = proc.stdout or ""
    err = proc.stderr or ""

    # On sépare le body du meta
    body = out
    status = 0
    final_url = url
    marker = "\n__CURL_META__\n"
    if marker in out:
        body, meta = out.split(marker, 1)
        meta_lines = meta.strip().split("\n")
        if len(meta_lines) >= 1:
            try:
                status = int(meta_lines[0])
            except ValueError:
                status = 0
        if len(meta_lines) >= 2:
            final_url = meta_lines[1]

    if proc.returncode != 0 and not out:
        raise RuntimeError(f"curl a échoué (code {proc.returncode}): {err.strip()}")

    return CurlResponse(status_code=status or proc.returncode, text=body, final_url=final_url)


def _flaresolverr_post(payload: dict, *, timeout: int = 120) -> dict:
    """Envoie une commande à FlareSolverr via POST /v1 et retourne la réponse JSON."""
    import urllib.request
    import urllib.error

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        FLARESOLVERR_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"FlareSolverr injoignable sur {FLARESOLVERR_URL}. "
            f"Lancez-le : docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest. "
            f"Détail : {exc}"
        ) from exc


def _cache_key(url: str) -> str:
    """Génère un nom de fichier de cache sûr depuis une URL."""
    import hashlib
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    # On garde aussi un prefix lisible pour debug
    slug = re.sub(r"[^a-z0-9]+", "_", url.lower())[-60:]
    return f"{slug}_{h}.html"


def _cache_get(url: str) -> str | None:
    """Récupère le HTML mis en cache pour cette URL, ou None."""
    if not DISK_CACHE_ENABLED:
        return None
    cache_file = DISK_CACHE_DIR / _cache_key(url)
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _cache_set(url: str, html: str) -> None:
    """Stocke le HTML en cache disque."""
    if not DISK_CACHE_ENABLED:
        return
    try:
        DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = DISK_CACHE_DIR / _cache_key(url)
        cache_file.write_text(html, encoding="utf-8")
    except Exception as exc:
        log.debug("  cache write failed for %s: %s", url, exc)


def _flaresolverr_get(url: str, *, max_time: int | None = None,
                      wait_seconds: int | None = None) -> CurlResponse:
    """GET via FlareSolverr en utilisant la session persistante si disponible.

    FlareSolverr lance un vrai Chrome non-headless qui résout automatiquement
    les challenges Cloudflare JS. La session persistante garde Chrome ouvert
    avec ses cookies, évitant de relancer le navigateur à chaque requête.

    `wait_seconds` : délai (en secondes) à laisser s'écouler APRÈS la résolution
                     du challenge et AVANT de récupérer le HTML, via le paramètre
                     `waitInSeconds` de FlareSolverr. Indispensable pour les pages
                     de jeu dont les blocs .secure-data / .su-spoiler sont injectés
                     par JavaScript après le chargement. (FlareSolverr ne supporte
                     PAS l'attente d'un sélecteur CSS : il n'y a pas de
                     `waitForSelector` dans son API.)
    """
    timeout = max_time or FS_REQUEST_TIMEOUT

    # Chemin pool multi-instances : si un FlareSolverrPool est actif (plusieurs
    # conteneurs), on délègue le GET au pool qui répartit en round-robin sur ses
    # sessions. CurlResponse du pool est API-compatible (.status_code/.text/.url).
    if _FS_POOL is not None:
        return _FS_POOL.get(
            url,
            max_timeout=FS_MAX_TIMEOUT,
            wait_seconds=wait_seconds,
        )

    payload: dict = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": FS_MAX_TIMEOUT,
    }
    if _FS_SESSION_ID:
        payload["session"] = _FS_SESSION_ID
    if wait_seconds and wait_seconds > 0:
        payload["waitInSeconds"] = wait_seconds

    # On laisse à la requête HTTP de quoi couvrir le challenge + le délai de rendu.
    post_timeout = timeout + 30 + (wait_seconds or 0)
    data = _flaresolverr_post(payload, timeout=post_timeout)

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr a échoué: {data.get('message', 'erreur inconnue')}")

    solution = data.get("solution", {})
    status = solution.get("status", 200)
    html = solution.get("response", "")
    final_url = solution.get("url", url)

    # Vérification de sécurité : si le HTML retourné est encore une page
    # de challenge Cloudflare, on signale l'échec
    if html and ("Just a moment" in html[:500] or "challenge-platform" in html[:2000]):
        raise RuntimeError(
            f"FlareSolverr n'a pas réussi à résoudre le challenge Cloudflare pour {url}"
        )

    return CurlResponse(status_code=status, text=html, final_url=final_url)


def _fetch(url: str, *, follow_redirects: bool = True) -> CurlResponse:
    """Dispatch vers le backend HTTP configuré."""
    if _HTTP_BACKEND == "flaresolverr":
        return _flaresolverr_get(url)
    else:
        return _curl_get(url, follow_redirects=follow_redirects)


def _backoff_with_jitter(attempt: int, base: float = 2.0, max_wait: float = 120.0) -> float:
    """Backoff exponentiel avec jitter (style AWS).

    Prévient les orages de retry synchronisés quand plusieurs workers
    frappent la même limite de débit simultanément.

    Retourne un délai aléatoire entre 0 et min(base^attempt, max_wait).
    """
    import random
    ceiling = min(base ** attempt, max_wait)
    return random.uniform(0, ceiling)


def http_get(url: str, session=None, *, follow_redirects: bool = True) -> CurlResponse:
    """GET avec retry.

    Backoff exponentiel avec jitter sur 429/503/403.
    Le paramètre `session` est conservé pour compatibilité mais ignoré."""
    last_exc: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = _fetch(url, follow_redirects=follow_redirects)
            if resp.status_code in (403, 429, 503):
                wait = _backoff_with_jitter(attempt)
                log.warning("  HTTP %s sur %s — pause %.1fs (tentative %d/%d)",
                            resp.status_code, url, wait, attempt, HTTP_RETRIES)
                time.sleep(wait)
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            wait = _backoff_with_jitter(attempt)
            log.warning("  tentative %d/%d échouée (%s): %s", attempt, HTTP_RETRIES, url, exc)
            time.sleep(wait)
    raise RuntimeError(f"échec HTTP après {HTTP_RETRIES} tentatives: {url}") from last_exc


def init_flaresolverr_session() -> None:
    """Crée une session persistante FlareSolverr.

    La session garde une instance Chrome ouverte avec les cookies Cloudflare
    valides, ce qui évite de relancer le navigateur à chaque requête.
    Toutes les requêtes suivantes réutiliseront cette session."""
    global _FS_SESSION_ID, _FS_POOL
    if _HTTP_BACKEND != "flaresolverr":
        return

    # Détection multi-instances : FLARESOLVERR_URLS (CSV) prioritaire, sinon
    # FLARESOLVERR_URL unique. Avec ≥ 2 URLs distinctes, on monte un pool
    # round-robin pour le FALLBACK HTML ; avec une seule URL, on garde le
    # comportement historique (session unique) pour ne rien régresser.
    try:
        from flaresolverr_pool import FlareSolverrPool, parse_flaresolverr_urls
        fs_urls = parse_flaresolverr_urls()
    except Exception as exc:
        log.debug("  pool FlareSolverr indisponible (%s) — session unique", exc)
        fs_urls = []

    if len(fs_urls) > 1:
        log.info("Création pool FlareSolverr multi-instances (%d URLs)...", len(fs_urls))
        try:
            _FS_POOL = FlareSolverrPool(fs_urls, verbose=False)
            log.info("  ✓ Pool FlareSolverr prêt : %d instance(s) active(s)",
                     _FS_POOL.size)
            return
        except Exception as exc:
            log.warning("  ⚠ Échec init pool (%s) — repli sur session unique", exc)
            _FS_POOL = None

    session_id = f"dlpsgame-{int(time.time())}"
    log.info("Création session FlareSolverr persistante...")
    try:
        data = _flaresolverr_post({"cmd": "sessions.create", "session": session_id})
        if data.get("status") == "ok":
            _FS_SESSION_ID = session_id
            log.info("  ✓ Session FlareSolverr créée: %s", _FS_SESSION_ID)
            # On fait une première requête pour "pré-chauffer" la session
            # (résout le challenge Cloudflare et stocke les cookies)
            log.info("  Pré-chargement de la page de catégorie...")
            resp = _flaresolverr_get(PS5_CATEGORY_URL, max_time=60)
            log.info("  ✓ Pré-chargement OK (HTTP %d, %d octets)",
                     resp.status_code, len(resp.text))
        else:
            log.warning("  ⚠ Session non créée: %s — utilisation sans session",
                        data.get("message"))
    except Exception as exc:
        log.warning("  ⚠ Impossible de créer la session: %s", exc)
        log.warning("    Les requêtes seront indépendantes (plus lent)")


def destroy_flaresolverr_session() -> None:
    """Détruit la session FlareSolverr (ou le pool) à la fin du scrape."""
    global _FS_SESSION_ID, _FS_POOL
    if _FS_POOL is not None:
        try:
            _FS_POOL.destroy_all()
        except Exception:
            pass
        _FS_POOL = None
        return
    if not _FS_SESSION_ID:
        return
    try:
        _flaresolverr_post({"cmd": "sessions.destroy", "session": _FS_SESSION_ID})
        log.info("  ✓ Session FlareSolverr détruite: %s", _FS_SESSION_ID)
    except Exception:
        pass
    _FS_SESSION_ID = None


def _unwrap_shortener(url: str) -> str:
    """Déballe les liens emballés dans un raccourcisseur monétisé.

    dlpsgame route désormais ses miroirs via des liens du type :
      https://shrinkearn.com/full?api=<clé>&url=<base64>&type=2
    où la vraie destination (downloadgameps3.net/... ou un hébergeur direct)
    est encodée en base64 dans le paramètre `url`. On la décode pour la rendre
    résolvable par la suite. Si rien n'est déballable, on renvoie l'URL telle quelle.
    """
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return url
    raw = params.get("url", [None])[0]
    if not raw:
        return url

    candidate = raw
    if not candidate.lower().startswith("http"):
        # tentative de décodage base64 (standard puis url-safe, padding corrigé)
        candidate = ""
        padded = raw + "=" * (-len(raw) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                txt = decoder(padded).decode("utf-8", errors="strict")
            except Exception:
                continue
            if txt.lower().startswith("http"):
                candidate = txt
                break
        if not candidate:
            return url

    # Garde-fou : on n'accepte que des cibles pertinentes (redirecteur connu
    # ou hébergeur connu), pour ne jamais réécrire un lien légitime par erreur.
    if any(h in candidate for h in REDIRECT_HOSTS) or any(p in candidate for p, _ in MIRROR_PATTERNS):
        return candidate
    return url


def resolve_redirect(url: str, session=None, mirror_hint: str | None = None) -> str | None:
    """Si l'URL est une page de redirection (downloadgameps3.net/...),
    on la résout en URL directe vers l'hébergeur (akirabox, vikingfile,
    datanodes, ...).

    Sur downloadgameps3.net, les liens sont obfqués en JavaScript :
    on doit scraper le HTML et reconstruire l'URL depuis les attributs
    data-domain + data-path. Si plusieurs liens existent, on prend
    celui qui correspond au `mirror_hint` (Akia/Viki/Data/...).

    Retourne None si on n'arrive pas à résoudre vers un hébergeur connu.
    """
    # Déballe d'abord un éventuel raccourcisseur (shrinkearn & co) pour
    # retrouver la vraie cible avant toute résolution.
    url = _unwrap_shortener(url)

    if not any(host in url for host in REDIRECT_HOSTS):
        return url

    # Cache : si on a déjà résolu cette URL, on retourne le résultat caché
    cache_key = f"{url}#{mirror_hint or ''}"
    if cache_key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[cache_key]

    try:
        resp = http_get(url, follow_redirects=True)
    except Exception as exc:
        log.warning("    resolve_redirect: échec pour %s — %s", url, exc)
        _RESOLVE_CACHE[cache_key] = None
        return None

    # 1) Si curl a suivi une 302/301 vers une URL directe, on l'utilise
    final_url = resp.url
    if final_url and not is_non_host_url(final_url) and not any(host in final_url for host in REDIRECT_HOSTS):
        if any(p in final_url for p, _ in MIRROR_PATTERNS):
            _RESOLVE_CACHE[cache_key] = final_url
            return final_url

    # 2) Sur downloadgameps3.net : on cherche les <a class="secure-lnk">
    #    avec data-domain + data-path. On prend celui qui matche mirror_hint.
    if resp.text:
        soup = BeautifulSoup(resp.text, "html.parser")
        secure_links: list[tuple[str, str, str]] = []  # (domain, path, text)
        for a in soup.find_all("a", class_="secure-lnk"):
            domain = a.get("data-domain", "")
            path = a.get("data-path", "")
            text = a.get_text(strip=True)
            if domain and path:
                secure_links.append((domain, path, text))

        # D'abord on essaie de matcher avec mirror_hint
        if mirror_hint and secure_links:
            hint_lower = mirror_hint.lower()
            mirror_to_pattern = {
                "akia": "akirabox",
                "viki": "vikingfile",
                "data": "datanodes",
                "filek": "filekeeper",
                "vault": "datavaults",
                "buzz": "buzzheavier",
                "1file": "1fichier",
                "mediafire": "mediafire",
                "rootz": "rootz",
            }
            target_pattern = mirror_to_pattern.get(hint_lower, hint_lower)
            for domain, path, _text in secure_links:
                if target_pattern in domain.lower() or target_pattern in path.lower():
                    resolved = _assemble_secure_url(domain, path)
                    if resolved:
                        _RESOLVE_CACHE[cache_key] = resolved
                        return resolved

        # Sinon : on prend le premier lien secure qui pointe vers un hébergeur connu
        for domain, path, _text in secure_links:
            full = _assemble_secure_url(domain, path)
            if full and any(p in full for p, _ in MIRROR_PATTERNS):
                _RESOLVE_CACHE[cache_key] = full
                return full

        # 3) Fallback : on cherche un <a href> direct vers un hébergeur connu
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and not any(h in href for h in REDIRECT_HOSTS):
                if any(p in href for p, _ in MIRROR_PATTERNS):
                    _RESOLVE_CACHE[cache_key] = href
                    return href

        # 4) Recherche d'une meta refresh
        meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        if meta and meta.get("content"):
            m = re.search(r"url\s*=\s*([^\s;]+)", meta["content"], re.I)
            if m:
                target = m.group(1)
                if not target.startswith("http"):
                    target = urljoin(resp.url, target)
                if not any(h in target for h in REDIRECT_HOSTS):
                    if any(p in target for p, _ in MIRROR_PATTERNS):
                        _RESOLVE_CACHE[cache_key] = target
                        return target

    _RESOLVE_CACHE[cache_key] = None
    return None  # échec de résolution


def _assemble_secure_url(domain: str, path: str) -> str | None:
    """Reconstruit l'URL finale depuis data-domain + data-path.

    Exemple : domain="https://akirabox.", path="com/abc/file"
            → "https://akirabox.com/abc/file"
    """
    if not domain or not path:
        return None
    # domain se termine par un point (ex : "https://akirabox.")
    # path commence par le TLD (ex : "com/abc/file")
    domain = domain.strip()
    path = path.strip()
    if not domain.endswith("."):
        # parfois c'est déjà complet
        return domain + path if not path.startswith("/") else domain + path
    return domain + path


# ---------------------------------------------------------------------------
# Découverte des jeux
# ---------------------------------------------------------------------------

GAME_LINK_RE = re.compile(r"^https?://dlpsgame\.com/[a-z0-9][a-z0-9\-]*\-ps5/?$")


def is_game_url(url: str) -> bool:
    """Filtre les URLs qui pointent réellement vers une page de jeu PS5.
    On exclut les pages d'index comme /list-game-ps5/ ou /category/ps5/."""
    if not GAME_LINK_RE.match(url):
        return False
    if "/list-game-ps5" in url or "/category/ps5" in url:
        return False
    return True


def discover_via_list_page(session: requests.Session) -> list[str]:
    """Découvre les jeux via la page d'index unique /list-game-ps5/.

    Cette page WordPress liste TOUS les jeux PS5 sous forme de liens
    <a href=".../xxx-ps5/">, à jour et déjà filtrée PS5. Un seul fetch
    suffit donc à découvrir l'intégralité du catalogue, contrairement à
    la pagination de catégorie (~35 requêtes, fragile en cas de 403/503).

    Retourne la liste dédupliquée des URLs de jeu, ou [] en cas d'échec
    (l'appelant retombera alors sur discover_via_category).
    """
    log.info("Découverte via la page de liste : %s", PS5_LIST_URL)
    try:
        resp = http_get(PS5_LIST_URL, session)
    except Exception as exc:
        log.warning("  page de liste inaccessible: %s", exc)
        return []
    if resp.status_code != 200:
        log.warning("  page de liste → HTTP %d", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    game_urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if is_game_url(href) and href not in seen:
            seen.add(href)
            game_urls.append(href)

    log.info("  page de liste : %d jeux trouvés", len(game_urls))
    return game_urls


def discover_via_category(session: requests.Session, max_pages: int | None) -> list[str]:
    """Parcourt toutes les pages /category/ps5/page/N et collecte les URLs
    de pages de jeu. La pagination WordPress suit la forme
    /category/ps5/page/2/, /category/ps5/page/3/, ..."""
    game_urls: list[str] = []
    seen: set[str] = set()

    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break

        if page == 1:
            url = PS5_CATEGORY_URL
        else:
            url = f"{PS5_CATEGORY_URL}page/{page}/"

        log.info("Découverte page %d : %s", page, url)
        try:
            resp = http_get(url, session)
        except Exception as exc:
            log.error("  page %d inaccessible: %s — arrêt pagination", page, exc)
            break

        if resp.status_code != 200:
            log.info("  page %d → HTTP %d — fin de pagination", page, resp.status_code)
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Tous les liens internes qui finissent par -ps5/
        page_links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if is_game_url(href) and href not in seen:
                seen.add(href)
                page_links.append(href)
                game_urls.append(href)

        if not page_links:
            log.info("  page %d : 0 lien de jeu trouvé — fin de pagination", page)
            break

        log.info("  page %d : %d jeux", page, len(page_links))
        page += 1
        time.sleep(PAGE_DELAY)

    log.info("Total : %d jeux découverts sur %d pages", len(game_urls), page - 1)
    return game_urls


def discover_game_urls(session: requests.Session, max_pages: int | None) -> list[str]:
    """Aiguilleur de découverte.

    Par défaut (run complet, max_pages=None) : on tente d'abord la page de
    liste unique /list-game-ps5/ — bien plus rapide et robuste. Si elle est
    indisponible ou vide, on retombe sur la pagination de catégorie.

    Si --max-pages est fourni (mode test/limité), on garde directement la
    pagination, puisque la page de liste ne connaît pas cette notion de pages.
    """
    if max_pages is None:
        urls = discover_via_list_page(session)
        if urls:
            return urls
        log.warning("Page de liste vide/indisponible — repli sur la pagination catégorie")
    return discover_via_category(session, max_pages)




# ---------------------------------------------------------------------------
# Parsing d'une page de jeu
# ---------------------------------------------------------------------------

PPSA_RE = re.compile(r"\b([A-Z]{4}\d{5})\b")
VERSION_RE = re.compile(r"v(?:ersion)?\s*0?(\d+\.\d+(?:\.\d+)?)", re.I)
# La détection de taille est centralisée dans sizes.py : ancrage sur « SIZE: »
# puis unités anglaises complètes uniquement (KB/MB/GB/TB), ce qui corrige le
# bug historique où le mot anglais « to » (= T+o) était lu en Téraoctets.


def decode_payload(payload: str) -> str:
    """Décode le base64 du data-payload (parfois URL-safe, parfois standard)."""
    payload = payload.strip()
    # Souvent le contenu est en base64 standard ; on teste aussi URL-safe.
    try:
        return base64.b64decode(payload).decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        return base64.urlsafe_b64decode(payload + "===").decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_mirror_name(url: str, link_text: str) -> str:
    """Détecte le nom du miroir à partir de l'URL (prioritaire) ou du texte du lien."""
    url_lower = url.lower()
    for pattern, name in MIRROR_PATTERNS:
        if pattern in url_lower:
            return name
    # Fallback sur le texte du lien
    txt = (link_text or "").strip().capitalize()
    return txt or "Mirror"


def is_ignored_link(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in IGNORED_LINK_TEXTS


def extract_links_from_html(
    html_fragment: str,
    base_url: str,
) -> list[tuple[str, str]]:
    """Extrait tous les (texte, href) d'un fragment HTML décodé.
    On ignore les liens Guide/Tool/DMCA et les ancres internes."""
    soup = BeautifulSoup(html_fragment, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href or href.startswith("#"):
            continue
        if is_ignored_link(text):
            continue
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        out.append((text, href))
    return out


def extract_poster_url(soup: BeautifulSoup, page_url: str) -> str | None:
    """Récupère l'URL de l'image (cover/poster) du jeu."""
    # 1) OpenGraph image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]
    # 2) thumbnailUrl dans le JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("thumbnailUrl"):
                    return node["thumbnailUrl"]
        if isinstance(data, dict) and data.get("thumbnailUrl"):
            return data["thumbnailUrl"]
    # 3) Première image du corps d'article
    article = soup.find("article") or soup
    img = article.find("img")
    if img and img.get("src"):
        src = img["src"]
        if not src.startswith("http"):
            src = urljoin(page_url, src)
        return src
    return None


def extract_main_title(soup: BeautifulSoup) -> str:
    """Titre principal du jeu (sans le suffixe '- Download Game PSX...')."""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"]
        # On retire le suffixe standard du site
        title = re.sub(r"\s*-\s*Download Game PSX.*$", "", title, flags=re.I)
        return title.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


def find_spoiler_groups(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Retourne la liste des (label_text, decoded_html) pour chaque
    div.secure-data trouvé sur la page. Le label est déterminé par le
    texte du <p> ou <h2> qui précède immédiatement le spoiler.

    On détecte aussi les spoilers WordPress (div.su-spoiler) qui
    encapsulent les div.secure-data.
    """
    groups: list[tuple[str, str]] = []
    for secure in soup.find_all("div", class_="secure-data"):
        payload = secure.get("data-payload", "")
        if payload:
            # Cas "non rendu" : l'attribut base64 est encore là, on le décode.
            decoded = decode_payload(payload)
        else:
            # Cas "déjà rendu" : le JS de la page a décodé le payload dans le
            # DOM puis retiré l'attribut data-payload. Le HTML interne du div
            # contient donc directement les liens en clair — on le lit tel quel.
            decoded = secure.decode_contents()
        if not decoded or not decoded.strip():
            continue

        # Recherche du label : on remonte aux ancêtres de type su-spoiler,
        # puis on cherche le <p> précédent.
        label = ""
        spoiler = secure.find_parent("div", class_=re.compile(r"su-spoiler"))
        if spoiler:
            prev = spoiler.find_previous_sibling()
            # On remonte jusqu'à trouver un <p> ou <h*> non vide
            while prev is not None:
                txt = prev.get_text(" ", strip=True) if hasattr(prev, "get_text") else ""
                if txt and not txt.lower().startswith("link download"):
                    label = txt
                    break
                prev = prev.find_previous_sibling()
        groups.append((label, decoded))
    return groups


def detect_file_format(groups: list[tuple[str, str]], download_links: list[dict]) -> list[str]:
    """Déduit le(s) type(s) de format/distribution d'un jeu.

    Combine deux sources de signal :
      1. le texte décodé de la page (exFAT, ffpkg, APR-EMU, Backport 4.xx, ...)
      2. l'extension de fichier visible dans les URLs de téléchargement
         (.pkg, .rar, .7z, .zip)
    Retourne une liste de marqueurs (ex. ["exFAT", "APR-EMU", "Backport 4.xx"]),
    ou ["unknown"] si aucun signal n'est trouvé.
    """
    text = " ".join(
        BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        for _, html in groups
    )
    # Les labels de groupe (ex. "exFAT", "Backport 4.xx") sont aussi du signal.
    group_labels = [label for label, _ in groups if label]
    urls = [link.get("url") or "" for link in download_links]
    return detect_formats([text, *group_labels], urls=urls)


def build_download_links(
    groups: list[tuple[str, str]],
    page_url: str,
    session=None,
) -> list[dict]:
    """Construit la liste finale des downloadLinks à partir des groupes
    de spoilers. Suit les redirections downloadgameps3.net et déduplique."""
    seen_urls: set[str] = set()
    out: list[dict] = []

    for label, html_fragment in groups:
        group = detect_group(label)
        for text, href in extract_links_from_html(html_fragment, page_url):
            # On calcule d'abord le miroir à partir du TEXTE du lien (Akia/Viki/...)
            # car c'est la seule info fiable avant résolution.
            mirror_hint = extract_mirror_name(href, text).lower()

            # Résout downloadgameps3.net → akirabox/vikingfile/datanodes
            direct = resolve_redirect(href, session, mirror_hint=mirror_hint)

            # Si la résolution échoue (lien toujours sur downloadgameps3.net), on skip
            if direct is None:
                log.debug("    skip (non résolu): %s — %s", text, href)
                continue
            if is_non_host_url(direct):
                log.debug("    skip (non-hébergeur): %s — %s", text, direct)
                continue

            # On recalcule le nom du miroir à partir de l'URL finale (plus fiable)
            mirror = extract_mirror_name(direct, text)

            # Si on a un groupe spécifique (Backport/DLC/Dump/exFAT), on préfixe
            # le nom pour rester compatible avec le script.js convertSingleGame.
            if group in ("Backport", "DLC", "Dump", "exFAT"):
                name = f"{group} - {mirror}"
            else:
                name = mirror

            dedupe_key = direct
            if dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)
            out.append({"name": name, "url": direct})

    return out


def extract_metadata(
    soup: BeautifulSoup,
    groups: list[tuple[str, str]],
    page_url: str,
) -> dict:
    """Récupère titleId, version, taille, description, tags, poster."""
    # Concatène tout le texte décodé pour chercher titleId / version / size
    all_decoded_text = "\n".join(html for _, html in groups)
    # On enlève les balises pour la recherche textuelle
    decoded_plain = BeautifulSoup(all_decoded_text, "html.parser").get_text(" ", strip=True)

    # titleId
    title_id_match = PPSA_RE.search(decoded_plain)
    title_id = title_id_match.group(1) if title_id_match else None

    # version (première occurrence de vXX.YYY)
    version_match = VERSION_RE.search(decoded_plain)
    version = f"0{version_match.group(1)}" if version_match else "1.0"
    # On normalise au format "01.004" par ex.
    if version_match:
        parts = version_match.group(1).split(".")
        if len(parts) >= 2:
            version = f"{int(parts[0]):02d}.{parts[1].ljust(3, '0')[:3]}"
            if len(parts) >= 3:
                version += f".{parts[2].ljust(3, '0')[:3]}"

    # Taille : on cherche d'abord dans les spoilers décodés (taille compressée
    # généralement annoncée là). Repli : le contenu de l'article principal, car
    # certains jeux n'indiquent la taille que dans la description, hors spoiler.
    size_bytes, size_str = extract_size(decoded_plain)
    if not size_bytes:
        article = (
            soup.find("article")
            or soup.find(class_=re.compile(r"entry-content|post-content"))
        )
        if article:
            # On exclut les zones de commentaires qui parlent d'autres tailles.
            for junk in article.select("#comments, .comments, .comment-list"):
                junk.decompose()
            article_text = article.get_text(" ", strip=True)
            size_bytes, size_str = extract_size(article_text)

    # Tags : tous les PPSA + toutes les versions FW (4.xx etc.)
    tags: list[str] = []
    if title_id:
        tags.append(title_id)
    if version_match:
        tags.append(f"v{version_match.group(1)}")
    fw_matches = re.findall(r"\b\d\.xx\b", decoded_plain, re.I)
    for fw in fw_matches[:2]:
        if fw not in tags:
            tags.append(fw)

    # Région
    region_match = re.search(r"REGION\s*:\s*([A-Z]+)", decoded_plain)
    if region_match:
        tags.append(region_match.group(1))

    # Description : on assemble les infos importantes dans l'ordre attendu
    desc_lines: list[str] = []
    if tags:
        desc_lines.append(f"Tags: {', '.join(tags)}")
    if size_str:
        desc_lines.append(f"Size: {size_str}")
    # Crédits
    credits_match = re.search(r"BY\s*:\s*([^\n<]+)", decoded_plain, re.I)
    if credits_match:
        credit_text = credits_match.group(1).strip()
        # On retire les références parasites (Guide Download, Tool Download, parenthèses vides)
        credit_text = re.sub(r"\(\s*[^)]*(?:Guide|Tool)[^)]*\)", "", credit_text, flags=re.I).strip()
        # On coupe au "Thanks to" car ce sera capturé séparément
        credit_text = re.split(r"\bThanks to\b", credit_text, maxsplit=1, flags=re.I)[0].strip()
        # On retire le " ( ... )" final s'il reste
        credit_text = re.sub(r"\s*\(\s*\)\s*", "", credit_text).strip()
        credit_text = re.sub(r"\s+", " ", credit_text)
        if credit_text:
            desc_lines.append(f"Credits: {credit_text}")
    thanks_match = re.search(r"Thanks to ([^\n<]+)", decoded_plain, re.I)
    if thanks_match:
        thanks_text = thanks_match.group(1).strip()
        # On coupe au premier "." ou "( Guide" pour ne pas garder les références parasites
        thanks_text = re.split(r"\.\s|\(\s*Guide", thanks_text, maxsplit=1, flags=re.I)[0].strip()
        if thanks_text:
            desc_lines.append(f"Thanks: {thanks_text}")
    fw_req_match = re.search(r"FW\s*REQUIRED\s*:\s*([^\n<.]+?)(?:\.\s|\n|$)", decoded_plain, re.I)
    if fw_req_match:
        fw_text = fw_req_match.group(1).strip()
        # On coupe au premier mot-clé parasite (LINK:, SIZE:, BY:)
        fw_text = re.split(r"\b(?:LINK|SIZE|BY|REGION|VOICE|WORKS)\s*:", fw_text, maxsplit=1, flags=re.I)[0].strip()
        if fw_text:
            desc_lines.append(f"FW: {fw_text}")

    description = "\n".join(desc_lines)

    poster = extract_poster_url(soup, page_url)
    title = extract_main_title(soup)

    return {
        "titleId": title_id or "",
        "title": title,
        "version": version,
        "posterUrl": poster,
        "description": description,
        "sizeBytes": size_bytes,
        "tags": tags,
    }


# ---------------------------------------------------------------------------
# Scraping d'une page de jeu
# ---------------------------------------------------------------------------

def scrape_game_page(url: str, session=None) -> dict | None:
    log.info("  → %s", url)

    # 1. Vérifier le cache disque d'abord
    cached_html = _cache_get(url)
    if cached_html:
        log.debug("    (depuis cache disque)")
        html = cached_html
    else:
        # 2. Sinon, fetch via le backend HTTP
        # En mode FlareSolverr, on laisse un délai de rendu JS (waitInSeconds)
        # pour que les blocs .secure-data / .su-spoiler soient injectés dans le
        # DOM avant de récupérer le HTML.
        try:
            if _HTTP_BACKEND == "flaresolverr":
                # Retry spécifique pour les pages de jeu avec délai de rendu
                last_exc = None
                html = ""
                status = 0
                for attempt in range(1, HTTP_RETRIES + 1):
                    try:
                        resp = _flaresolverr_get(
                            url,
                            max_time=FS_REQUEST_TIMEOUT,
                            wait_seconds=FS_WAIT_SECONDS,
                        )
                        if resp.status_code in (403, 429, 503):
                            wait = [5, 15, 30, 60, 120][min(attempt - 1, 4)]
                            log.warning("    HTTP %d — pause %ds (tentative %d/%d)",
                                        resp.status_code, wait, attempt, HTTP_RETRIES)
                            time.sleep(wait)
                            continue
                        html = resp.text
                        status = resp.status_code
                        break
                    except Exception as exc:
                        last_exc = exc
                        wait = [5, 15, 30, 60, 120][min(attempt - 1, 4)]
                        log.warning("    tentative %d/%d échouée: %s — pause %ds",
                                    attempt, HTTP_RETRIES, exc, wait)
                        time.sleep(wait)
                else:
                    if last_exc:
                        log.warning("    inaccessible après %d tentatives: %s",
                                    HTTP_RETRIES, last_exc)
                    return None
                if status != 200:
                    log.warning("    HTTP %d", status)
                    return None
            else:
                resp = http_get(url, session)
                if resp.status_code != 200:
                    log.warning("    HTTP %d", resp.status_code)
                    return None
                html = resp.text
        except Exception as exc:
            log.warning("    inaccessible: %s", exc)
            return None

        # Vérification : si la page ne contient pas de secure-data, c'est
        # probablement que le JS n'a pas rendu les spoilers. On ne cache pas.
        if "secure-data" not in html and "su-spoiler" not in html:
            log.debug("    HTML sans secure-data ni su-spoiler — page non rendue?")
            # On ne retourne pas None tout de suite : on essaie quand même
            # de parser au cas où le format aurait changé
        else:
            # On ne met en cache que les pages valides
            _cache_set(url, html)

    soup = BeautifulSoup(html, "html.parser")
    groups = find_spoiler_groups(soup)
    if not groups:
        # Diagnostic ciblé pour distinguer les deux causes possibles :
        has_marker = ("secure-data" in html) or ("su-spoiler" in html)
        has_payload = "data-payload" in html
        if not has_marker:
            log.warning("    aucun spoiler trouvé — ni .secure-data ni .su-spoiler "
                        "dans le HTML (page probablement pas rendue : augmentez "
                        "FS_WAIT_SECONDS, ou la structure du site a changé)")
        elif not has_payload:
            log.warning("    bloc .secure-data présent mais SANS data-payload — "
                        "la structure du site a probablement changé")
        else:
            log.warning("    data-payload présent mais non décodable "
                        "(base64 invalide ou format modifié)")
        # Dump du HTML pour inspection (uniquement si demandé via DEBUG_DUMP_DIR)
        dump_dir = os.environ.get("DEBUG_DUMP_DIR")
        if dump_dir:
            try:
                Path(dump_dir).mkdir(parents=True, exist_ok=True)
                dump_path = Path(dump_dir) / _cache_key(url)
                dump_path.write_text(html, encoding="utf-8")
                log.warning("    HTML brut écrit dans %s", dump_path)
            except Exception as exc:
                log.debug("    échec du dump HTML : %s", exc)
        return None

    meta = extract_metadata(soup, groups, url)
    download_links = build_download_links(groups, url, session)

    if not download_links:
        log.debug("    aucun lien de téléchargement direct trouvé")
        return None

    # Si pas de titleId, on en génère un placeholder
    title_id = meta["titleId"] or f"GAME_{abs(hash(url)) % 100000:05d}"

    package = {
        "titleId": title_id,
        "title": meta["title"],
        "version": meta["version"],
        "category": "game",
        "posterUrl": meta["posterUrl"],
        "description": meta["description"],
        "downloadLinks": download_links,
        "downloadSource": url,
        "source": SITE_SOURCE,
        "fileFormat": detect_file_format(groups, download_links),
    }
    # On n'écrit sizeBytes QUE si la taille est connue : un champ à null fait
    # rejeter (skip) l'entrée par Pegasus DL, alors qu'un champ absent est ignoré.
    if meta.get("sizeBytes"):
        package["sizeBytes"] = meta["sizeBytes"]
    log.info("    ✓ %s — %d liens", meta["title"], len(download_links))
    return package


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def scrape_all(
    session,
    max_pages: int | None,
    max_games: int | None,
    concurrency: int,
    mode: str = "full",
    checkpoint_every: int = 0,
) -> tuple[list[dict], list[str]]:
    game_urls = discover_game_urls(session, max_pages)
    if max_games is not None:
        game_urls = game_urls[:max_games]

    # --- Mode incrémental : filtrer les URLs déjà scrapées ---
    urls_to_scrape = game_urls
    manifest = None
    if mode == "incremental":
        try:
            from scrape_manifest import ScrapeManifest
            manifest = ScrapeManifest()
            urls_to_scrape = manifest.filter_urls(game_urls, "incremental")
            known_urls = manifest._data.get("entries", {})
            new_urls = [u for u in game_urls if u not in known_urls]
            log.info("Mode incrémental : %d total, %d à scraper, %d nouveaux",
                     len(game_urls), len(urls_to_scrape), len(new_urls))
        except ImportError:
            log.warning("scrape_manifest.py non trouvé — mode full forcé")
            urls_to_scrape = game_urls

    packages: list[dict] = []
    warnings: list[str] = []
    failed_urls: list[str] = []

    if concurrency <= 1:
        for i, url in enumerate(urls_to_scrape, 1):
            log.info("[%d/%d] ", i, len(game_urls))
            pkg = scrape_game_page(url, session)
            if pkg:
                packages.append(pkg)
                if manifest:
                    manifest.record(url, "", pkg)
            else:
                warnings.append(url)
                failed_urls.append(url)
            # Checkpointing : sauvegarde partielle tous les N jeux
            if checkpoint_every > 0 and i % checkpoint_every == 0 and packages:
                _checkpoint = build_catalog(packages)
                _cp_path = Path("dlpsgame-ps5.checkpoint.json")
                _cp_path.write_text(json.dumps(_checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("  Checkpoint : %d jeux sauvegardés", len(packages))
            time.sleep(PAGE_DELAY)
    else:
        # Concurrency: on crée plusieurs sessions thread-local
        with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(scrape_game_page, url, session): url for url in urls_to_scrape}
            for fut in cf.as_completed(futures):
                url = futures[fut]
                try:
                    pkg = fut.result()
                except Exception as exc:
                    warnings.append(f"{url}: {exc}")
                    failed_urls.append(url)
                    continue
                if pkg:
                    packages.append(pkg)
                else:
                    warnings.append(url)
                    failed_urls.append(url)

    # === Phase 2 : retry des pages échouées ===
    # FlareSolverr peut avoir des timeouts aléatoires. On retry les échecs
    # en ignorant le cache (qui peut contenir une page incomplète).
    if failed_urls:
        log.info("=" * 60)
        log.info("Phase 2 : retry de %d page(s) échouée(s)", len(failed_urls))
        log.info("=" * 60)
        # On invalide le cache pour ces URLs
        global DISK_CACHE_ENABLED
        old_cache_flag = DISK_CACHE_ENABLED
        # On garde le cache activé mais on supprime les entrées échouées
        for url in failed_urls:
            cache_file = DISK_CACHE_DIR / _cache_key(url)
            if cache_file.exists():
                try:
                    cache_file.unlink()
                except Exception:
                    pass

        retry_success = 0
        still_failing = []
        for i, url in enumerate(failed_urls, 1):
            log.info("[retry %d/%d] ", i, len(failed_urls))
            pkg = scrape_game_page(url, session)
            if pkg:
                packages.append(pkg)
                retry_success += 1
            else:
                still_failing.append(url)
            # Délai plus long entre les retries
            time.sleep(PAGE_DELAY * 2)

        log.info("Phase 2 terminée : %d succès, %d encore en échec",
                 retry_success, len(still_failing))
        warnings = still_failing

    # Sauvegarder le manifest si mode incrémental
    if manifest:
        # Enregistrer les URLs pas scrapées (hors du scope) comme vues
        for url in game_urls:
            if url not in urls_to_scrape and url in manifest._entries:
                pass  # déjà dans le manifest
        manifest.save()

    # Tri final par titre
    packages.sort(key=lambda p: (p.get("title") or "").lower())
    return packages, warnings


def build_catalog(packages: list[dict]) -> dict:
    return {
        "name": "dlpsgame PS5",
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": PS5_CATEGORY_URL,
        "packages": packages,
    }


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--max-pages", type=int, default=None,
                   help="Limite le nombre de pages de catégorie parcourues")
    p.add_argument("--max-games", type=int, default=None,
                   help="Limite le nombre de jeux scraped (utile pour test)")
    p.add_argument("--out", type=Path, default=Path("dlpsgame-ps5.json"),
                   help="Chemin du fichier JSON de sortie")
    p.add_argument("--concurrency", type=int, default=2,
                   help="Nombre de threads pour scraper les pages de jeu "
                        "(défaut: 2 — downloadgameps3.net limite à ~2 req/s)")
    p.add_argument("--http-backend", choices=["curl", "flaresolverr"],
                   default="curl",
                   help="Backend HTTP : 'curl' (local, IP non bloquée) ou "
                        "'flaresolverr' (proxy Docker, requis sur GitHub Actions).")
    p.add_argument("--flaresolverr-url", default=None,
                   help="URL du FlareSolverr (défaut: http://localhost:8191/v1)")
    p.add_argument("--no-cache", action="store_true",
                   help="Désactive le cache disque (.scrape_cache/) — force le re-scrap complet")
    p.add_argument("--mode", choices=["full", "incremental"], default="full",
                   help="Mode de scraping : 'full' (tout rescrap) ou "
                        "'incremental' (seulement les jeux nouveaux/modifiés, défaut: full)")
    p.add_argument("--checkpoint-every", type=int, default=0,
                   help="Sauvegarder un checkpoint partiel tous les N jeux (0 = désactivé)")
    p.add_argument("--verbose", action="store_true", help="Logs debug")
    args = p.parse_args(argv)

    setup_logging(args.verbose)

    # Configuration du backend HTTP
    global _HTTP_BACKEND, FLARESOLVERR_URL, DISK_CACHE_ENABLED
    _HTTP_BACKEND = args.http_backend
    if args.no_cache:
        DISK_CACHE_ENABLED = False
    if args.flaresolverr_url:
        FLARESOLVERR_URL = args.flaresolverr_url

    # En mode FlareSolverr, on crée d'abord la session (ou le pool multi-instances)
    # pour réutiliser la/les instance(s) Chrome (beaucoup plus rapide).
    if _HTTP_BACKEND == "flaresolverr":
        init_flaresolverr_session()

    # FlareSolverr est mono-session par instance : une seule session ne supporte
    # pas le parallélisme (les requêtes concurrentes sont sérialisées). On force
    # donc concurrency=1 SAUF si un pool multi-instances est actif : dans ce cas
    # on autorise jusqu'à min(concurrency demandée, taille du pool) requêtes en
    # parallèle (round-robin sur les conteneurs).
    if _HTTP_BACKEND == "flaresolverr" and args.concurrency > 1:
        if _FS_POOL is not None and _FS_POOL.size > 1:
            new_conc = min(args.concurrency, _FS_POOL.size)
            if new_conc != args.concurrency:
                log.warning("Pool FlareSolverr de %d instance(s) — concurrency ramené à %d",
                            _FS_POOL.size, new_conc)
            args.concurrency = new_conc
        else:
            log.warning("FlareSolverr (session unique) ne supporte pas le parallélisme "
                        "— concurrency forcé à 1")
            args.concurrency = 1

    log.info("Démarrage scraper dlpsgame.com/category/ps5 (backend: %s, concurrency: %d)",
             _HTTP_BACKEND, args.concurrency)

    try:
        packages, warnings = scrape_all(
            session=None,
            max_pages=args.max_pages,
            max_games=args.max_games,
            concurrency=args.concurrency,
            mode=args.mode,
            checkpoint_every=args.checkpoint_every,
        )
    finally:
        # Nettoyage : détruire la session FlareSolverr
        if _HTTP_BACKEND == "flaresolverr":
            destroy_flaresolverr_session()

    catalog = build_catalog(packages)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("Écrit : %s  (%d jeux, %d warnings)",
             args.out, len(packages), len(warnings))

    # On écrit aussi un fichier de warnings à côté
    if warnings:
        warn_path = args.out.with_suffix(".warnings.txt")
        warn_path.write_text("\n".join(warnings), encoding="utf-8")
        log.info("Warnings écrits : %s", warn_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
