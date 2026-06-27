#!/usr/bin/env python3
"""
Scraper via l'API WordPress REST de dlpsgame.com
=================================================
Utilise l'endpoint /wp-json/wp/v2/posts pour récupérer les métadonnées
des jeux PS5 sans avoir besoin de FlareSolverr ni de scraper le HTML.

BEAUCOUP plus rapide que le scraper HTML classique :
  - 8 requêtes API (per_page=100) vs 719+ requêtes HTML
  - Pas de Cloudflare challenge sur l'API
  - JSON structuré (pas de parsing HTML pour les métadonnées de base)

Le contenu base64 (data-payload) dans content.rendered est décodé pour
extraire titleId, version, size, download links — identique au scraper HTML.

Usage:
    # Full scrape via API
    python scrape_wp_api.py --out dlpsgame-ps5.api.json

    # Discovery only (just list game URLs)
    python scrape_wp_api.py --discover-only

    # Limited test
    python scrape_wp_api.py --max-pages 1 --verbose

    # Incremental mode (only new/changed games)
    python scrape_wp_api.py --mode incremental --out dlpsgame-ps5.api.json
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse, parse_qs

# ---------------------------------------------------------------------------
# Conditional imports for shared logic
# ---------------------------------------------------------------------------
try:
    from scrape_manifest import ScrapeManifest
except ImportError:
    ScrapeManifest = None  # type: ignore[assignment,misc]

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment]

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://dlpsgame.com"
WP_API_URL = f"{BASE_URL}/wp-json/wp/v2/posts"
PS5_CATEGORY_ID = 63019
SITE_SOURCE = "dlpsgame.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

API_PER_PAGE = 100  # max per page for WP REST API
API_DELAY = 0.5     # seconds between API page requests
API_TIMEOUT = 30    # seconds per API request
API_RETRIES = 5     # retries on 429/503/timeout

REDIRECT_DELAY = 0.3  # seconds between redirect resolution requests
REDIRECT_TIMEOUT = 15
REDIRECT_RETRIES = 3

# Disk cache for API responses and redirect resolutions
DISK_CACHE_DIR = Path(".scrape_cache_api")
DISK_CACHE_ENABLED = True

# ---------------------------------------------------------------------------
# Mirror patterns — same as scrape_dlpsgame.py
# ---------------------------------------------------------------------------

MIRROR_PATTERNS = [
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

REDIRECT_HOSTS = (
    "downloadgameps3.net",
    "downloadgameps3.com",
    "dlpsgame.com",
)

NON_HOST_HOSTS = {
    "api.predb.net", "predb.org", "predb.me",
    "www.google.com", "google.com",
    "www.facebook.com", "facebook.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "discord.gg", "discord.com", "www.discord.com",
    "t.me", "reddit.com", "www.reddit.com",
    "youtube.com", "www.youtube.com", "youtu.be",
    "wikipedia.org",
    "dlpsgame.com", "downloadgameps3.com", "downloadgameps3.net",
    "ad.a-ads.com", "pagead2.googlesyndication.com",
}

IGNORED_LINK_TEXTS = {
    "guide download", "tool download", "guide download game",
    "tool download°", "guide download°", "dmca",
    "guide", "tool", "download", "here", "click", "click here",
    "link", "this link", "download here", "more info", "read more",
    "pre-db", "predb",
}

# Regex patterns — same as scrape_dlpsgame.py
PPSA_RE = re.compile(r"\b([A-Z]{4}\d{5})\b")
VERSION_RE = re.compile(r"v(?:ersion)?\s*0?(\d+\.\d+(?:\.\d+)?)", re.I)
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KMGT])[\s]?(?:B|O|b|o)\b", re.I)

# Secure-lnk pattern from downloadgameps3.net redirect pages
SECURE_LNK_RE = re.compile(
    r'<a[^>]*class=["\'][^"\']*secure-lnk[^"\']*["\'][^>]*'
    r'data-domain=["\']([^"\']+)["\'][^>]*'
    r'data-path=["\']([^"\']+)["\'][^>]*>([^<]+)</a>',
    re.IGNORECASE | re.DOTALL,
)

# data-payload extraction from content.rendered
DATA_PAYLOAD_RE = re.compile(
    r'data-payload=["\']([A-Za-z0-9+/=]+)["\']',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("wp-api-scraper")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# HTTP helpers (using urllib — no Cloudflare on API endpoints)
# ---------------------------------------------------------------------------

def _urllib_get(url: str, *, timeout: int = API_TIMEOUT, headers: dict | None = None) -> tuple[int, str, dict]:
    """Simple GET via urllib. Returns (status, body, response_headers).

    No Cloudflare challenge on the WP API endpoint, so we can use plain urllib.
    """
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return status, body, resp_headers
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return exc.code, body, {}
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"HTTP request failed for {url}: {exc}") from exc


def api_get(url: str, *, timeout: int = API_TIMEOUT) -> tuple[int, str, dict]:
    """GET with retry and exponential backoff on 429/503."""
    backoff_table = [2, 5, 15, 30, 60]
    last_exc: Exception | None = None

    for attempt in range(1, API_RETRIES + 1):
        try:
            status, body, headers = _urllib_get(url, timeout=timeout)
            if status in (429, 503):
                wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
                log.warning("  HTTP %d on %s — wait %ds (attempt %d/%d)",
                            status, url, wait, attempt, API_RETRIES)
                time.sleep(wait)
                continue
            return status, body, headers
        except RuntimeError as exc:
            last_exc = exc
            wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
            log.warning("  attempt %d/%d failed (%s): %s", attempt, API_RETRIES, url, exc)
            time.sleep(wait)

    raise RuntimeError(f"API request failed after {API_RETRIES} attempts: {url}") from last_exc


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _cache_key(key: str) -> str:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^a-z0-9]+", "_", key.lower())[-60:]
    return f"{slug}_{h}.json"


def _cache_get(key: str) -> str | None:
    if not DISK_CACHE_ENABLED:
        return None
    cache_file = DISK_CACHE_DIR / _cache_key(key)
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _cache_set(key: str, data: str) -> None:
    if not DISK_CACHE_ENABLED:
        return
    try:
        DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = DISK_CACHE_DIR / _cache_key(key)
        cache_file.write_text(data, encoding="utf-8")
    except Exception as exc:
        log.debug("  cache write failed for %s: %s", key, exc)


# ---------------------------------------------------------------------------
# Payload decoding — same logic as scrape_dlpsgame.py
# ---------------------------------------------------------------------------

def decode_payload(payload: str) -> str:
    """Decode base64 data-payload (standard or URL-safe)."""
    payload = payload.strip()
    try:
        return base64.b64decode(payload).decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        return base64.urlsafe_b64decode(payload + "===").decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Mirror / link helpers — same as scrape_dlpsgame.py
# ---------------------------------------------------------------------------

def get_hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_non_host_url(url: str) -> bool:
    host = get_hostname(url)
    if not host:
        return True
    return host in NON_HOST_HOSTS


def extract_mirror_name(url: str, link_text: str) -> str:
    url_lower = url.lower()
    for pattern, name in MIRROR_PATTERNS:
        if pattern in url_lower:
            return name
    txt = (link_text or "").strip().capitalize()
    return txt or "Mirror"


def is_ignored_link(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in IGNORED_LINK_TEXTS


def parse_size_bytes(size_str: str | None) -> int | None:
    if not size_str:
        return None
    m = SIZE_RE.search(size_str.replace("\xa0", " "))
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    multipliers = {"K": 1024, "M": 1048576, "G": 1073741824, "T": 1099511627776}
    return int(value * multipliers.get(unit, 1))


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


# ---------------------------------------------------------------------------
# URL shortener unwrapping
# ---------------------------------------------------------------------------

def _unwrap_shortener(url: str) -> str:
    """Unwrap monetized shortener links (shrinkearn, etc.)."""
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return url
    raw = params.get("url", [None])[0]
    if not raw:
        return url

    candidate = raw
    if not candidate.lower().startswith("http"):
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

    if any(h in candidate for h in REDIRECT_HOSTS) or any(p in candidate for p, _ in MIRROR_PATTERNS):
        return candidate
    return url


# ---------------------------------------------------------------------------
# Redirect resolution
# ---------------------------------------------------------------------------

# Global cache for resolved redirect URLs
_RESOLVE_CACHE: dict[str, str | None] = {}


def _assemble_secure_url(domain: str, path: str) -> str | None:
    """Reconstruct URL from data-domain + data-path on downloadgameps3.net."""
    if not domain or not path:
        return None
    domain = domain.strip()
    path = path.strip()
    if not domain.endswith("."):
        return domain + path if not path.startswith("/") else domain + path
    return domain + path


def resolve_redirect(url: str, mirror_hint: str | None = None) -> str | None:
    """Resolve a downloadgameps3.net redirect URL to a direct hoster URL.

    Uses simple urllib since the redirector typically doesn't have Cloudflare.
    Falls back to parsing secure-lnk JavaScript obfuscation if needed.
    """
    url = _unwrap_shortener(url)

    if not any(host in url for host in REDIRECT_HOSTS):
        return url

    cache_key = f"{url}#{mirror_hint or ''}"
    if cache_key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[cache_key]

    # Try disk cache
    cached = _cache_get(f"redirect:{cache_key}")
    if cached is not None:
        try:
            result = json.loads(cached)
            _RESOLVE_CACHE[cache_key] = result
            return result
        except Exception:
            pass

    backoff = [1, 3, 8]
    last_exc: Exception | None = None

    for attempt in range(1, REDIRECT_RETRIES + 1):
        try:
            # Use urllib with follow_redirects (HTTP redirects handled by urllib)
            req_headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            req = urllib.request.Request(url, headers=req_headers, method="GET")

            # We need to check the final URL after redirects
            # urllib follows redirects automatically but doesn't expose the final URL easily
            # So we use a custom opener that tracks redirects
            final_url = url
            body = ""

            class RedirectTracker(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    nonlocal final_url
                    final_url = newurl
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            opener = urllib.request.build_opener(RedirectTracker)
            try:
                with opener.open(req, timeout=REDIRECT_TIMEOUT) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    if hasattr(resp, 'url') and resp.url:
                        final_url = resp.url
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429, 503):
                    wait = backoff[min(attempt - 1, len(backoff) - 1)]
                    log.debug("    redirect HTTP %d for %s — wait %ds", exc.code, url, wait)
                    time.sleep(wait)
                    continue
                # For other HTTP errors, try to read the body anyway
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                wait = backoff[min(attempt - 1, len(backoff) - 1)]
                log.debug("    redirect attempt %d failed for %s: %s", attempt, url, exc)
                time.sleep(wait)
                continue

            # 1) If the final URL after redirects points to a known hoster
            if final_url and not is_non_host_url(final_url) and not any(host in final_url for host in REDIRECT_HOSTS):
                if any(p in final_url for p, _ in MIRROR_PATTERNS):
                    _RESOLVE_CACHE[cache_key] = final_url
                    _cache_set(f"redirect:{cache_key}", json.dumps(final_url))
                    return final_url

            # 2) Parse the HTML for secure-lnk (data-domain + data-path)
            if body:
                # Try BeautifulSoup first
                if BeautifulSoup:
                    soup = BeautifulSoup(body, "html.parser")
                    secure_links: list[tuple[str, str, str]] = []
                    for a in soup.find_all("a", class_="secure-lnk"):
                        domain = a.get("data-domain", "")
                        path_val = a.get("data-path", "")
                        text = a.get_text(strip=True)
                        if domain and path_val:
                            secure_links.append((domain, path_val, text))

                    # Try to match mirror_hint
                    if mirror_hint and secure_links:
                        hint_lower = mirror_hint.lower()
                        mirror_to_pattern = {
                            "akia": "akirabox", "viki": "vikingfile",
                            "data": "datanodes", "filek": "filekeeper",
                            "vault": "datavaults", "buzz": "buzzheavier",
                            "1file": "1fichier", "mediafire": "mediafire",
                            "rootz": "rootz",
                        }
                        target_pattern = mirror_to_pattern.get(hint_lower, hint_lower)
                        for domain, path_val, _text in secure_links:
                            if target_pattern in domain.lower() or target_pattern in path_val.lower():
                                resolved = _assemble_secure_url(domain, path_val)
                                if resolved:
                                    _RESOLVE_CACHE[cache_key] = resolved
                                    _cache_set(f"redirect:{cache_key}", json.dumps(resolved))
                                    return resolved

                    # Take the first secure link pointing to a known hoster
                    for domain, path_val, _text in secure_links:
                        full = _assemble_secure_url(domain, path_val)
                        if full and any(p in full for p, _ in MIRROR_PATTERNS):
                            _RESOLVE_CACHE[cache_key] = full
                            _cache_set(f"redirect:{cache_key}", json.dumps(full))
                            return full

                    # Fallback: look for direct <a href> to known hosters
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if href.startswith("http") and not any(h in href for h in REDIRECT_HOSTS):
                            if any(p in href for p, _ in MIRROR_PATTERNS):
                                _RESOLVE_CACHE[cache_key] = href
                                _cache_set(f"redirect:{cache_key}", json.dumps(href))
                                return href

                    # Check meta refresh
                    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
                    if meta and meta.get("content"):
                        m = re.search(r"url\s*=\s*([^\s;]+)", meta["content"], re.I)
                        if m:
                            target = m.group(1)
                            if not target.startswith("http"):
                                target = urljoin(final_url or url, target)
                            if not any(h in target for h in REDIRECT_HOSTS):
                                if any(p in target for p, _ in MIRROR_PATTERNS):
                                    _RESOLVE_CACHE[cache_key] = target
                                    _cache_set(f"redirect:{cache_key}", json.dumps(target))
                                    return target
                else:
                    # Fallback without BeautifulSoup: regex for secure-lnk
                    for match in SECURE_LNK_RE.finditer(body):
                        domain, path_val, text = match.group(1), match.group(2), match.group(3)
                        full = _assemble_secure_url(domain, path_val)
                        if full and any(p in full for p, _ in MIRROR_PATTERNS):
                            _RESOLVE_CACHE[cache_key] = full
                            _cache_set(f"redirect:{cache_key}", json.dumps(full))
                            return full

            # Could not resolve
            break

        except Exception as exc:
            last_exc = exc
            log.debug("    resolve_redirect exception for %s: %s", url, exc)

    _RESOLVE_CACHE[cache_key] = None
    log.debug("    could not resolve redirect: %s", url)
    return None


# ---------------------------------------------------------------------------
# WP API: Fetch paginated posts
# ---------------------------------------------------------------------------

def fetch_api_page(page: int, *, per_page: int = API_PER_PAGE, fields: str = "full") -> tuple[list[dict], int, int]:
    """Fetch one page of posts from the WP REST API.

    Args:
        page: Page number (1-indexed).
        per_page: Number of posts per page (max 100).
        fields: "full" to include content.rendered, "light" for excerpt only.

    Returns:
        (posts, total_posts, total_pages)
    """
    # Build the _fields parameter to minimize response size
    if fields == "light":
        api_fields = "id,date,slug,link,title,excerpt,categories,tags"
    else:
        api_fields = "id,date,slug,link,title,content,excerpt,categories,tags,yoast_head_json"

    params = f"?categories={PS5_CATEGORY_ID}&per_page={per_page}&page={page}&_fields={api_fields}"
    url = WP_API_URL + params

    log.debug("  API request: page %d — %s", page, url)
    status, body, headers = api_get(url)

    if status != 200:
        log.warning("  API page %d returned HTTP %d", page, status)
        return [], 0, 0

    try:
        posts = json.loads(body)
    except json.JSONDecodeError as exc:
        log.error("  API page %d: invalid JSON — %s", page, exc)
        return [], 0, 0

    total_posts = int(headers.get("x-wp-total", 0))
    total_pages = int(headers.get("x-wp-totalpages", 0))

    log.debug("  API page %d: %d posts (total: %d, pages: %d)", page, len(posts), total_posts, total_pages)
    return posts, total_posts, total_pages


def fetch_all_posts(
    *,
    max_pages: int | None = None,
    max_games: int | None = None,
    fields: str = "full",
) -> list[dict]:
    """Fetch all PS5 posts via the WP REST API (paginated).

    Args:
        max_pages: Limit the number of API pages to fetch.
        max_games: Limit total number of games.
        fields: "full" for content.rendered, "light" for excerpt only.

    Returns:
        List of WP post objects.
    """
    all_posts: list[dict] = []
    total_posts = 0
    total_pages = 0
    page = 1

    while True:
        if max_pages is not None and page > max_pages:
            break

        posts, total_posts, total_pages = fetch_api_page(page, fields=fields)

        if not posts:
            break

        all_posts.extend(posts)

        # Respect max_games limit
        if max_games is not None and len(all_posts) >= max_games:
            all_posts = all_posts[:max_games]
            break

        # If we've fetched all pages, stop
        if page >= total_pages:
            break

        page += 1
        time.sleep(API_DELAY)

    log.info("Fetched %d posts across %d page(s) (total on site: %d)",
             len(all_posts), min(page, total_pages or page), total_posts)
    return all_posts


# ---------------------------------------------------------------------------
# Extract data-payload base64 from content.rendered
# ---------------------------------------------------------------------------

def extract_payloads_from_content(content_rendered: str) -> list[str]:
    """Extract all base64 data-payload strings from content.rendered HTML.

    The content contains <div class="secure-data" data-payload="..."> elements.
    """
    if not content_rendered:
        return []
    return DATA_PAYLOAD_RE.findall(content_rendered)


# ---------------------------------------------------------------------------
# Extract links from decoded HTML fragment
# ---------------------------------------------------------------------------

def extract_links_from_html(html_fragment: str, base_url: str) -> list[tuple[str, str]]:
    """Extract all (text, href) from a decoded HTML fragment.

    Same logic as scrape_dlpsgame.py's extract_links_from_html.
    """
    out: list[tuple[str, str]] = []

    if BeautifulSoup:
        soup = BeautifulSoup(html_fragment, "html.parser")
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
    else:
        # Fallback regex-based extraction
        for match in re.finditer(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', html_fragment, re.I):
            href = match.group(1).strip()
            text = match.group(2).strip()
            if not href or href.startswith("#"):
                continue
            if is_ignored_link(text):
                continue
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            out.append((text, href))

    return out


# ---------------------------------------------------------------------------
# Build download links from decoded payloads
# ---------------------------------------------------------------------------

def build_download_links_from_payloads(
    payloads: list[str],
    page_url: str,
    *,
    resolve_redirects: bool = True,
) -> list[dict]:
    """Build the downloadLinks list from decoded base64 payloads.

    Args:
        payloads: List of base64-encoded data-payload strings.
        page_url: The game page URL (for resolving relative links).
        resolve_redirects: Whether to resolve downloadgameps3.net redirects.

    Returns:
        List of {"name": "...", "url": "..."} dicts.
    """
    seen_urls: set[str] = set()
    out: list[dict] = []

    for payload_b64 in payloads:
        decoded = decode_payload(payload_b64)
        if not decoded or not decoded.strip():
            continue

        # Extract links from the decoded HTML
        links = extract_links_from_html(decoded, page_url)

        for text, href in links:
            # Determine mirror hint from link text
            mirror_hint = extract_mirror_name(href, text).lower()

            if resolve_redirects:
                direct = resolve_redirect(href, mirror_hint=mirror_hint)
            else:
                # Skip redirect resolution — keep the original URL
                if any(host in href for host in REDIRECT_HOSTS):
                    # Mark for HTML scraper fallback
                    continue
                direct = href

            if direct is None:
                log.debug("    skip (unresolved): %s — %s", text, href)
                continue
            if is_non_host_url(direct):
                log.debug("    skip (non-host): %s — %s", text, direct)
                continue

            mirror = extract_mirror_name(direct, text)

            dedupe_key = direct
            if dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)
            out.append({"name": mirror, "url": direct})

    return out


# ---------------------------------------------------------------------------
# Extract metadata from post + decoded payloads
# ---------------------------------------------------------------------------

def extract_metadata_from_post(
    post: dict,
    decoded_texts: list[str],
) -> dict:
    """Extract titleId, version, size, description, poster from a WP post.

    Args:
        post: The WP REST API post object.
        decoded_texts: List of decoded base64 payload plain texts.

    Returns:
        Dict with titleId, title, version, posterUrl, description, sizeBytes, tags.
    """
    # Combine all decoded text for regex searches
    all_decoded = "\n".join(decoded_texts)

    # Also parse excerpt for NAME/LANGUAGE/RELEASE/GENRE
    excerpt_rendered = (post.get("excerpt") or {}).get("rendered", "")
    excerpt_text = ""
    if BeautifulSoup and excerpt_rendered:
        excerpt_text = BeautifulSoup(excerpt_rendered, "html.parser").get_text(" ", strip=True)
    elif excerpt_rendered:
        excerpt_text = re.sub(r"<[^>]+>", " ", excerpt_rendered).strip()

    # titleId from decoded content
    title_id_match = PPSA_RE.search(all_decoded)
    title_id = title_id_match.group(1) if title_id_match else None

    # Try excerpt if not found in decoded content
    if not title_id and excerpt_text:
        title_id_match = PPSA_RE.search(excerpt_text)
        title_id = title_id_match.group(1) if title_id_match else None

    # Version
    version_match = VERSION_RE.search(all_decoded)
    version = "1.0"
    if version_match:
        parts = version_match.group(1).split(".")
        if len(parts) >= 2:
            version = f"{int(parts[0]):02d}.{parts[1].ljust(3, '0')[:3]}"
            if len(parts) >= 3:
                version += f".{parts[2].ljust(3, '0')[:3]}"

    # Size
    size_match = SIZE_RE.search(all_decoded.replace("\xa0", " "))
    size_str = size_match.group(0) if size_match else None
    size_bytes = parse_size_bytes(size_str)

    # Title
    title_rendered = (post.get("title") or {}).get("rendered", "")
    if BeautifulSoup and title_rendered:
        title = BeautifulSoup(title_rendered, "html.parser").get_text(strip=True)
    elif title_rendered:
        title = re.sub(r"<[^>]+>", "", title_rendered).strip()
    else:
        title = post.get("slug", "").replace("-", " ").title()

    # Remove the suffix "- Download Game PSX..."
    title = re.sub(r"\s*-\s*Download Game PSX.*$", "", title, flags=re.I).strip()

    # Poster URL from yoast_head_json
    poster_url = None
    yoast = post.get("yoast_head_json") or {}
    og_images = yoast.get("og_image") or []
    if og_images and isinstance(og_images, list):
        # Pick the first image with a reasonable size
        for img in og_images:
            if isinstance(img, dict) and img.get("url"):
                poster_url = img["url"]
                break
        if not poster_url and og_images:
            # Fallback: first element might be a string URL
            first = og_images[0]
            if isinstance(first, str):
                poster_url = first
            elif isinstance(first, dict) and first.get("url"):
                poster_url = first["url"]

    # If no poster from yoast, try to find one in content.rendered
    if not poster_url:
        content_rendered = (post.get("content") or {}).get("rendered", "")
        if content_rendered:
            img_match = re.search(
                r'<img[^>]+src=["\']([^"\']+)["\']',
                content_rendered,
                re.I,
            )
            if img_match:
                poster_url = img_match.group(1)

    # Tags
    tags: list[str] = []
    if title_id:
        tags.append(title_id)
    if version_match:
        tags.append(f"v{version_match.group(1)}")
    fw_matches = re.findall(r"\b\d\.xx\b", all_decoded, re.I)
    for fw in fw_matches[:2]:
        if fw not in tags:
            tags.append(fw)

    # Region
    region_match = re.search(r"REGION\s*:\s*([A-Z]+)", all_decoded)
    if region_match:
        tags.append(region_match.group(1))

    # Description
    desc_lines: list[str] = []
    if tags:
        desc_lines.append(f"Tags: {', '.join(tags)}")
    if size_str:
        desc_lines.append(f"Size: {size_str}")

    # Credits
    credits_match = re.search(r"BY\s*:\s*([^\n<]+)", all_decoded, re.I)
    if credits_match:
        credit_text = credits_match.group(1).strip()
        credit_text = re.sub(r"\(\s*[^)]*(?:Guide|Tool)[^)]*\)", "", credit_text, flags=re.I).strip()
        credit_text = re.split(r"\bThanks to\b", credit_text, maxsplit=1, flags=re.I)[0].strip()
        credit_text = re.sub(r"\s*\(\s*\)\s*", "", credit_text).strip()
        credit_text = re.sub(r"\s+", " ", credit_text)
        if credit_text:
            desc_lines.append(f"Credits: {credit_text}")

    thanks_match = re.search(r"Thanks to ([^\n<]+)", all_decoded, re.I)
    if thanks_match:
        thanks_text = thanks_match.group(1).strip()
        thanks_text = re.split(r"\.\s|\(\s*Guide", thanks_text, maxsplit=1, flags=re.I)[0].strip()
        if thanks_text:
            desc_lines.append(f"Thanks: {thanks_text}")

    fw_req_match = re.search(r"FW\s*REQUIRED\s*:\s*([^\n<.]+?)(?:\.\s|\n|$)", all_decoded, re.I)
    if fw_req_match:
        fw_text = fw_req_match.group(1).strip()
        fw_text = re.split(r"\b(?:LINK|SIZE|BY|REGION|VOICE|WORKS)\s*:", fw_text, maxsplit=1, flags=re.I)[0].strip()
        if fw_text:
            desc_lines.append(f"FW: {fw_text}")

    # Language from excerpt
    lang_match = re.search(r"LANGUAGE\s*[:\-]?\s*([A-Za-z\s,/&]+?)(?:\s+RELEASE|\s+GENRE|$)", excerpt_text, re.I)
    if lang_match:
        lang = lang_match.group(1).strip()
        if lang and lang.lower() not in ("multi", ""):
            desc_lines.append(f"Language: {lang}")

    description = "\n".join(desc_lines)

    return {
        "titleId": title_id or "",
        "title": title,
        "version": version,
        "posterUrl": poster_url,
        "description": description,
        "sizeBytes": size_bytes,
        "tags": tags,
    }


# ---------------------------------------------------------------------------
# Detect file format — same as scrape_dlpsgame.py
# ---------------------------------------------------------------------------

def detect_file_format(decoded_texts: list[str], download_links: list[dict]) -> list[str]:
    """Detect file format/distribution type from decoded text and download URLs."""
    text = " ".join(decoded_texts)
    found: list[str] = []

    def add(marker: str) -> None:
        if marker not in found:
            found.append(marker)

    text_markers = [
        (r"\bexfat\b", "exFAT"),
        (r"\bffpkg\b", "FFPKG"),
        (r"\bffpfsc\b", "FFPFSC"),
        (r"\bffpfs\b", "FFPFS"),
        (r"\bfpkg\b", "FPKG"),
        (r"\bpkg\b", "PKG"),
        (r"apr[\s-]*emu", "APR-EMU"),
    ]
    for pattern, marker in text_markers:
        if re.search(pattern, text, re.I):
            add(marker)

    for fw in re.findall(r"backport\s*(\d+\.xx)", text, re.I):
        add(f"Backport {fw.lower()}")
    if "backport" in text.lower() and not any(f.startswith("Backport") for f in found):
        add("Backport")

    url_ext = [
        (".pkg", "PKG"), (".rar", "RAR"), (".7z", "7z"), (".zip", "ZIP"),
        (".exfat", "exFAT"), (".ffpkg", "FFPKG"), (".ffpfsc", "FFPFSC"),
    ]
    for link in download_links:
        url = (link.get("url") or "").lower()
        for ext, marker in url_ext:
            if ext in url:
                add(marker)

    return found or ["unknown"]


# ---------------------------------------------------------------------------
# Process a single WP API post into a package
# ---------------------------------------------------------------------------

def process_post(
    post: dict,
    *,
    resolve_redirects: bool = True,
) -> dict | None:
    """Process a single WP REST API post into a catalog package.

    Args:
        post: WP REST API post object.
        resolve_redirects: Whether to resolve downloadgameps3.net redirects.

    Returns:
        Package dict or None if no download links could be extracted.
    """
    post_id = post.get("id", "?")
    slug = post.get("slug", "?")
    page_url = post.get("link", f"{BASE_URL}/{slug}/")

    log.debug("  Processing post %s: %s", post_id, slug)

    # Extract base64 payloads from content.rendered
    content_rendered = (post.get("content") or {}).get("rendered", "")
    payloads = extract_payloads_from_content(content_rendered)

    if not payloads:
        log.debug("    no data-payload found in post %s", post_id)
        # Still try to extract basic metadata for discovery purposes
        meta = extract_metadata_from_post(post, [])
        if not meta["titleId"]:
            log.debug("    skipping post %s: no payload and no titleId", post_id)
            return None
        # Return a minimal package (will be marked for HTML fallback)
        return {
            "titleId": meta["titleId"],
            "title": meta["title"],
            "version": meta["version"],
            "category": "game",
            "posterUrl": meta["posterUrl"],
            "description": meta["description"],
            "downloadLinks": [],
            "downloadSource": page_url,
            "source": SITE_SOURCE,
            "needsHtmlFallback": True,
            "fileFormat": ["unknown"],
        }

    # Decode all payloads
    decoded_texts: list[str] = []
    for payload_b64 in payloads:
        decoded = decode_payload(payload_b64)
        if decoded and decoded.strip():
            decoded_texts.append(decoded)

    if not decoded_texts:
        log.debug("    all payloads empty/undecodable for post %s", post_id)
        return None

    # Extract metadata
    meta = extract_metadata_from_post(post, decoded_texts)

    # Build download links
    download_links = build_download_links_from_payloads(
        payloads, page_url, resolve_redirects=resolve_redirects
    )

    # Detect file format
    file_format = detect_file_format(decoded_texts, download_links)

    # If no titleId, generate a placeholder
    title_id = meta["titleId"] or f"GAME_{abs(hash(page_url)) % 100000:05d}"

    needs_fallback = len(download_links) == 0

    package = {
        "titleId": title_id,
        "title": meta["title"],
        "version": meta["version"],
        "category": "game",
        "posterUrl": meta["posterUrl"],
        "description": meta["description"],
        "downloadLinks": download_links,
        "downloadSource": page_url,
        "source": SITE_SOURCE,
        "fileFormat": file_format,
    }

    if needs_fallback:
        package["needsHtmlFallback"] = True

    if meta.get("sizeBytes"):
        package["sizeBytes"] = meta["sizeBytes"]

    # Store post metadata useful for incremental tracking
    package["_wpMeta"] = {
        "postId": post_id,
        "date": post.get("date", ""),
        "slug": slug,
        "link": page_url,
    }

    log.info("    ✓ %s — %d links%s", meta["title"], len(download_links),
             " [NEEDS FALLBACK]" if needs_fallback else "")

    return package


# ---------------------------------------------------------------------------
# Discovery mode — just list game URLs
# ---------------------------------------------------------------------------

def discover_games(
    *,
    max_pages: int | None = None,
    max_games: int | None = None,
) -> list[dict]:
    """Discover games via the WP API (light mode, no content).

    Returns:
        List of dicts with id, slug, link, title, date.
    """
    posts = fetch_all_posts(max_pages=max_pages, max_games=max_games, fields="light")
    games: list[dict] = []

    for post in posts:
        title_rendered = (post.get("title") or {}).get("rendered", "")
        if BeautifulSoup and title_rendered:
            title = BeautifulSoup(title_rendered, "html.parser").get_text(strip=True)
        elif title_rendered:
            title = re.sub(r"<[^>]+>", "", title_rendered).strip()
        else:
            title = post.get("slug", "").replace("-", " ").title()

        games.append({
            "id": post.get("id"),
            "slug": post.get("slug", ""),
            "link": post.get("link", ""),
            "title": title,
            "date": post.get("date", ""),
        })

    return games


# ---------------------------------------------------------------------------
# Full scrape pipeline
# ---------------------------------------------------------------------------

def scrape_all_via_api(
    *,
    max_pages: int | None = None,
    max_games: int | None = None,
    mode: str = "full",
    resolve_redirects: bool = True,
    manifest_path: Path | None = None,
) -> tuple[list[dict], list[str]]:
    """Main pipeline: fetch all posts, process them, return packages.

    Args:
        max_pages: Limit number of API pages.
        max_games: Limit total number of games.
        mode: "full" or "incremental".
        resolve_redirects: Whether to resolve redirect URLs.
        manifest_path: Path to manifest file for incremental mode.

    Returns:
        (packages, warnings) where warnings are URLs that need HTML fallback.
    """
    # Fetch all posts from API (with content for metadata extraction)
    posts = fetch_all_posts(max_pages=max_pages, max_games=max_games, fields="full")

    # Initialize manifest for incremental mode
    manifest = None
    if mode == "incremental" and ScrapeManifest:
        manifest = ScrapeManifest(path=manifest_path or ".scrape_manifest_api.json")
        log.info("Incremental mode: manifest has %d entries", len(manifest._data.get("entries", {})))

    packages: list[dict] = []
    warnings: list[str] = []  # URLs needing HTML scraper fallback
    fallback_count = 0

    for i, post in enumerate(posts, 1):
        page_url = post.get("link", "")
        slug = post.get("slug", "?")
        post_date = post.get("date", "")

        # Incremental mode: check if we need to process this post
        if manifest and mode == "incremental":
            if not manifest.needs_scrape(page_url, mode="incremental"):
                # Use cached package if available
                cached = manifest.get_cached_package(page_url)
                if cached:
                    packages.append(cached)
                    log.debug("  [%d/%d] (cached) %s", i, len(posts), slug)
                    continue
                # No cache but not needing scrape either — skip
                log.debug("  [%d/%d] (skipped, fresh) %s", i, len(posts), slug)
                continue

        log.info("[%d/%d] %s", i, len(posts), slug)

        # Process the post
        pkg = process_post(post, resolve_redirects=resolve_redirects)

        if pkg:
            # Remove internal metadata before adding to catalog
            needs_fallback = pkg.pop("needsHtmlFallback", False)
            wp_meta = pkg.pop("_wpMeta", {})

            packages.append(pkg)

            # Record in manifest
            if manifest:
                # Use content hash based on post date + id (lightweight)
                content_for_hash = f"{post.get('id', '')}:{post_date}:{slug}"
                manifest.record(page_url, content_for_hash, package=pkg)

            if needs_fallback:
                fallback_count += 1
                warnings.append(page_url)
        else:
            warnings.append(page_url)

        # Small delay between processing posts (if resolving redirects)
        if resolve_redirects and i < len(posts):
            time.sleep(0.05)

    # Sort by title
    packages.sort(key=lambda p: (p.get("title") or "").lower())

    # Save manifest
    if manifest:
        manifest.save()

    log.info("Processed %d posts: %d packages, %d need HTML fallback",
             len(posts), len(packages), fallback_count)

    return packages, warnings


# ---------------------------------------------------------------------------
# Build catalog JSON
# ---------------------------------------------------------------------------

def build_catalog(packages: list[dict]) -> dict:
    return {
        "name": "dlpsgame PS5",
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": f"{BASE_URL}/wp-json/wp/v2/posts?categories={PS5_CATEGORY_ID}",
        "scrapeMethod": "wp-rest-api",
        "packages": packages,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", type=Path, default=Path("dlpsgame-ps5.api.json"),
        help="Output JSON path (default: dlpsgame-ps5.api.json)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Limit number of API pages (default: all)",
    )
    parser.add_argument(
        "--max-games", type=int, default=None,
        help="Limit total number of games",
    )
    parser.add_argument(
        "--discover-only", action="store_true",
        help="Only output game URLs, don't extract metadata",
    )
    parser.add_argument(
        "--mode", choices=["full", "incremental"], default="full",
        help="Scrape mode: 'full' or 'incremental' (uses ScrapeManifest)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable disk cache",
    )
    parser.add_argument(
        "--fields", choices=["full", "light"], default="full",
        help="API fields: 'full' (content.rendered with base64) or "
             "'light' (excerpt only, much smaller response)",
    )
    parser.add_argument(
        "--no-resolve-redirects", action="store_true",
        help="Don't resolve downloadgameps3.net redirects (faster but "
             "links will be missing or point to redirectors)",
    )
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="Path to manifest file for incremental mode "
             "(default: .scrape_manifest_api.json)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Debug logging",
    )

    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    # Configure disk cache
    global DISK_CACHE_ENABLED
    if args.no_cache:
        DISK_CACHE_ENABLED = False
        log.info("Disk cache disabled")

    log.info("Starting WP REST API scraper for dlpsgame.com PS5 catalog")
    log.info("  API endpoint: %s", WP_API_URL)
    log.info("  Category ID:  %d", PS5_CATEGORY_ID)
    log.info("  Mode:          %s", args.mode)
    log.info("  Fields:        %s", args.fields)

    # ---- Discovery-only mode ----
    if args.discover_only:
        log.info("Discovery mode: listing game URLs only")
        games = discover_games(max_pages=args.max_pages, max_games=args.max_games)

        output = {
            "name": "dlpsgame PS5 — Discovery",
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": f"{BASE_URL}/wp-json/wp/v2/posts?categories={PS5_CATEGORY_ID}",
            "totalGames": len(games),
            "games": games,
        }

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        log.info("Written: %s (%d games discovered)", args.out, len(games))
        # Also print a summary
        for game in games[:20]:
            log.info("  %s — %s", game.get("title", "?"), game.get("link", ""))
        if len(games) > 20:
            log.info("  ... and %d more", len(games) - 20)

        return 0

    # ---- Light fields mode (no content.rendered) ----
    if args.fields == "light":
        log.info("Light mode: using excerpt only (no base64 payload decoding)")
        games = discover_games(max_pages=args.max_pages, max_games=args.max_games)

        # Build minimal packages from excerpt data
        packages: list[dict] = []
        for game in games:
            # Try to extract titleId from the slug
            slug = game.get("slug", "")
            title_id_match = PPSA_RE.search(slug.upper())
            title_id = title_id_match.group(1) if title_id_match else ""

            packages.append({
                "titleId": title_id,
                "title": game.get("title", ""),
                "version": "1.0",
                "category": "game",
                "posterUrl": None,
                "description": "",
                "downloadLinks": [],
                "downloadSource": game.get("link", ""),
                "source": SITE_SOURCE,
                "needsHtmlFallback": True,
                "fileFormat": ["unknown"],
            })

        catalog = build_catalog(packages)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        log.info("Written: %s (%d games, all need HTML fallback for full data)",
                 args.out, len(packages))
        return 0

    # ---- Full scrape mode ----
    resolve_redirects = not args.no_resolve_redirects

    packages, warnings = scrape_all_via_api(
        max_pages=args.max_pages,
        max_games=args.max_games,
        mode=args.mode,
        resolve_redirects=resolve_redirects,
        manifest_path=args.manifest,
    )

    catalog = build_catalog(packages)

    # Write catalog JSON
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info("Written: %s (%d games, %d warnings)", args.out, len(packages), len(warnings))

    # Write warnings file (URLs needing HTML scraper fallback)
    if warnings:
        warn_path = args.out.with_suffix(".warnings.txt")
        warn_path.write_text("\n".join(warnings), encoding="utf-8")
        log.info("Warnings (need HTML fallback): %s (%d URLs)", warn_path, len(warnings))

    # Print summary statistics
    total_links = sum(len(p.get("downloadLinks", [])) for p in packages)
    with_links = sum(1 for p in packages if p.get("downloadLinks"))
    without_links = len(packages) - with_links
    with_size = sum(1 for p in packages if p.get("sizeBytes"))

    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info("  Total packages:     %d", len(packages))
    log.info("  With download links: %d", with_links)
    log.info("  Without links:       %d (need HTML fallback)", without_links)
    log.info("  Total links:         %d", total_links)
    log.info("  With size info:      %d", with_size)
    log.info("  Warnings file:       %s",
             args.out.with_suffix(".warnings.txt") if warnings else "(none)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
