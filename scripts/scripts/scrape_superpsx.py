#!/usr/bin/env python3
"""
Scraper SuperPSX - PS5 Games Catalog
-------------------------------------
Crawls https://www.superpsx.com/category/ps5/ps5-games/ (pagination /page/N/),
visits each game page to extract metadata from the info table, then visits each
DLL page (/dll-*) to extract download links, firmware requirements, and format
tags. Produces a JSON catalog in the SAME format as dlpsgame-ps5.json.

HTML structure (SuperPSX-specific):
  - Category page: articles in `article.item.hentry` → `h2.penci-entry-title a[href]`
  - Game page: info table `table.has-fixed-layout` with key/value `<td>` pairs;
    DLL page link found via `a[href*="/dll-"]`
  - DLL page: `table.has-fixed-layout` data tables with ⇛ (U+21DB) row separators;
    separator tables (no has-fixed-layout, single td with PPSA ID) mark new sections;
    download links: `a[data-penci-link="external"][rel="nofollow"]`
    Text-only hosts (FileK) appear as plain text between " – " separators

Usage:
    # Full scrape (all pages)
    python scrape_superpsx.py

    # Limited scrape for testing
    python scrape_superpsx.py --max-pages 2 --max-games 10

    # Custom output path
    python scrape_superpsx.py --out superpsx-ps5.json

    # Verbose mode
    python scrape_superpsx.py --verbose

    # With concurrency and custom delay
    python scrape_superpsx.py --concurrency 2 --delay 0.5
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
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
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.superpsx.com"
PS5_CATEGORY_URL = f"{BASE_URL}/category/ps5/ps5-games/"
SITE_SOURCE = "superpsx.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30       # seconds per curl request
PAGE_DELAY = 1.0           # delay between requests (seconds)
HTTP_RETRIES = 5           # retries on 429/503/timeout

# FlareSolverr configuration (placeholder for future use)
FS_REQUEST_TIMEOUT = 90
FS_MAX_TIMEOUT = 90000
FS_WAIT_SECONDS = 5

# Disk cache for resuming after interruption
DISK_CACHE_DIR = Path(".scrape_cache_superpsx")
DISK_CACHE_ENABLED = True

# curl binary path
CURL_BIN = shutil.which("curl") or "curl"

# HTTP backend: "curl" (default) or "flaresolverr" (placeholder)
_HTTP_BACKEND: str = "curl"

# FlareSolverr URL
FLARESOLVERR_URL = "http://localhost:8191/v1"
_FS_SESSION_ID: str | None = None

# ---------------------------------------------------------------------------
# Mirror name mapping (consistent with dlpsgame scraper)
# ---------------------------------------------------------------------------

MIRROR_PATTERNS = [
    # (pattern in URL, display name)
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

# SuperPSX-specific host name normalization
# The site uses short host abbreviations in table text
HOST_ALIASES = {
    "akr": "Akia",
    "viki": "Viki",
    "rootz": "Rootz",
    "onefile": "1File",
    "1file": "1File",
    "gofile": "Gofile",
    "mediafire": "Mediafire",
    "mega": "Mega",
    "filek": "Filek",
    "data": "Data",
    "buzz": "Buzz",
    "vault": "Vault",
}

# Non-host domains to exclude from download links
NON_HOST_HOSTS = {
    "superpsx.com",
    "www.superpsx.com",
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
}

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

PPSA_RE = re.compile(r"\b(PPSA\d{5})\b", re.I)
# Matches version like "v01.024", "v1.002", "01.024" (after PPSA in version row)
VERSION_RE = re.compile(r"v(?:ersion)?\s*0?(\d+\.\d+(?:\.\d+)?)", re.I)
# Matches version in DLL page Version row: "PPSA02182 – USA" (no v prefix sometimes)
VERSION_FROM_ROW_RE = re.compile(r"0?(\d+\.\d{3,})", re.I)
# Size: "117 GB", "85GB", "1.5 GB", "60GB"
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KMGT])[\s]?(?:B|O|b|o)\b", re.I)
# DLL page link on game page
DLL_LINK_RE = re.compile(r'href="(https://(?:www\.)?superpsx\.com/dll-[^/"]+/)"')
# Separator character used in DLL page tables
SEPARATOR = "\u21db"  # ⇛
# Download link selector pattern on DLL pages
DLL_DOWNLOAD_A_RE = re.compile(r"external", re.I)
# Game format tags: [APR-EMU], [FPKG], etc.
FORMAT_TAG_RE = re.compile(r"\[([A-Z0-9\-]+)\]")
# Firmware compatibility from Note rows: "Working 10.xx – 5.xx"
FW_COMPAT_RE = re.compile(
    r"(?:Working|Works?)\s+(\d+\.(?:\d+|xx))\s*[–\-\u2013]\s*(\d+\.(?:\d+|xx))", re.I
)
# Firmware from Fix/Backport rows: "Fix 4.xx", "Backport 7.xx"
FW_FIX_RE = re.compile(r"(?:Fix|Backport)\s+(\d+\.(?:\d+|xx))", re.I)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("superpsx-scraper")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# HTTP helpers (mirrors scrape_dlpsgame.py patterns)
# ---------------------------------------------------------------------------


class CurlResponse:
    """Lightweight wrapper mimicking requests.Response."""

    def __init__(self, status_code: int, text: str, final_url: str):
        self.status_code = status_code
        self.text = text
        self.url = final_url


def _curl_get(
    url: str,
    *,
    follow_redirects: bool = True,
    max_time: int | None = None,
) -> CurlResponse:
    """Execute curl as subprocess and return (status, body, final_url).

    curl is used because its native TLS fingerprint (OpenSSL) passes
    Cloudflare JA3 checks when the IP is not blocklisted."""
    timeout = max_time or REQUEST_TIMEOUT
    cmd = [
        CURL_BIN,
        "--silent", "--show-error",
        "--compressed",
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
        raise RuntimeError(f"curl failed (code {proc.returncode}): {err.strip()}")

    return CurlResponse(
        status_code=status or proc.returncode,
        text=body,
        final_url=final_url,
    )


def _cache_key(url: str) -> str:
    """Generate a safe cache filename from a URL."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^a-z0-9]+", "_", url.lower())[-60:]
    return f"{slug}_{h}.html"


def _cache_get(url: str) -> str | None:
    """Retrieve cached HTML for this URL, or None."""
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
    """Store HTML in disk cache."""
    if not DISK_CACHE_ENABLED:
        return
    try:
        DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = DISK_CACHE_DIR / _cache_key(url)
        cache_file.write_text(html, encoding="utf-8")
    except Exception as exc:
        log.debug("  cache write failed for %s: %s", url, exc)


def _fetch(url: str, *, follow_redirects: bool = True) -> CurlResponse:
    """Dispatch to the configured HTTP backend."""
    if _HTTP_BACKEND == "flaresolverr":
        # Placeholder: FlareSolverr not yet implemented for SuperPSX
        raise NotImplementedError("FlareSolverr backend not yet implemented for SuperPSX")
    else:
        return _curl_get(url, follow_redirects=follow_redirects)


def http_get(
    url: str,
    *,
    follow_redirects: bool = True,
) -> CurlResponse:
    """GET with retry and exponential backoff on 429/503.

    Backoff: 5s → 15s → 30s → 60s → 120s."""
    last_exc: Exception | None = None
    backoff_table = [5, 15, 30, 60, 120]
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = _fetch(url, follow_redirects=follow_redirects)
            if resp.status_code in (403, 429, 503):
                wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
                log.warning(
                    "  HTTP %s on %s — pause %ds (attempt %d/%d)",
                    resp.status_code, url, wait, attempt, HTTP_RETRIES,
                )
                time.sleep(wait)
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
            log.warning(
                "  attempt %d/%d failed (%s): %s",
                attempt, HTTP_RETRIES, url, exc,
            )
            time.sleep(wait)
    raise RuntimeError(f"HTTP failed after {HTTP_RETRIES} attempts: {url}") from last_exc


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def get_hostname(url: str) -> str:
    """Extract lowercase hostname (without port) from URL."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_non_host_url(url: str) -> bool:
    """True if URL points to a non-host domain (social, etc.)."""
    host = get_hostname(url)
    if not host:
        return True
    return host in NON_HOST_HOSTS


def parse_size_bytes(size_str: str | None) -> int | None:
    """Convert '117 GB', '85GB', '1.5 GB', '60GB' → bytes. None if unrecognized."""
    if not size_str:
        return None
    m = SIZE_RE.search(size_str.replace("\xa0", " "))
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    multipliers = {"K": 1024, "M": 1048576, "G": 1073741824, "T": 1099511627776}
    return int(value * multipliers.get(unit, 1))


def normalize_version(raw: str | None) -> str:
    """Normalize version string to '01.024' format.

    Handles: 'v01.024', '01.024', '1.024', '1.02', etc."""
    if not raw:
        return "01.000"
    m = VERSION_RE.search(raw)
    if m:
        parts = m.group(1).split(".")
        major = int(parts[0])
        minor = parts[1] if len(parts) > 1 else "000"
        # Pad minor to at least 3 digits
        minor = minor.ljust(3, "0")[:3]
        if len(parts) >= 3:
            patch = parts[2].ljust(3, "0")[:3]
            return f"{major:02d}.{minor}.{patch}"
        return f"{major:02d}.{minor}"
    # Try matching just a number like "01.024" without the v prefix
    m2 = VERSION_FROM_ROW_RE.search(raw)
    if m2:
        parts = m2.group(1).split(".")
        major = int(parts[0])
        minor = parts[1] if len(parts) > 1 else "000"
        minor = minor.ljust(3, "0")[:3]
        return f"{major:02d}.{minor}"
    return "01.000"


def extract_mirror_name(url: str, link_text: str = "") -> str:
    """Detect mirror name from URL (priority) or link text.

    Uses the MIRROR_PATTERNS list for URL matching and HOST_ALIASES
    for SuperPSX-specific abbreviations found in link text."""
    url_lower = url.lower()
    for pattern, name in MIRROR_PATTERNS:
        if pattern in url_lower:
            return name

    # Check link text against host aliases
    txt_lower = (link_text or "").strip().lower()
    if txt_lower in HOST_ALIASES:
        return HOST_ALIASES[txt_lower]

    # Fallback: capitalize first letter of link text
    txt = (link_text or "").strip()
    return txt.capitalize() if txt else "Mirror"


def detect_group(label_text: str) -> str:
    """Detect download group from a row label.

    Groups: Standard, Backport, DLC, Fix, Dump, exFAT"""
    t = label_text.lower()
    if "exfat" in t:
        return "exFAT"
    if "backport" in t:
        return "Backport"
    if "dlc" in t:
        return "DLC"
    if "fix" in t:
        return "Fix"
    if "dump" in t:
        return "Dump"
    return "Standard"


# ---------------------------------------------------------------------------
# Phase 1: Discover game URLs from category pages
# ---------------------------------------------------------------------------


def discover_game_urls(max_pages: int | None) -> list[str]:
    """Crawl all category pages and collect game page URLs.

    Category pages follow /category/ps5/ps5-games/page/N/ with ~20 games per
    page. Games are in `article.item.hentry` → `h2.penci-entry-title a[href]`.
    Pagination continues while `a.next.page-numbers[href]` exists."""
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

        log.info("Discovering page %d: %s", page, url)
        try:
            resp = http_get(url)
        except Exception as exc:
            log.error("  page %d unreachable: %s — stopping pagination", page, exc)
            break

        if resp.status_code != 200:
            log.info("  page %d → HTTP %d — end of pagination", page, resp.status_code)
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract game links from article entries
        page_links: list[str] = []
        for article in soup.select("article.item.hentry"):
            link_tag = article.select_one("h2.penci-entry-title a[href]")
            if link_tag:
                href = link_tag["href"].strip()
                # Only accept SuperPSX game pages (not dll- pages or category pages)
                if (
                    href.startswith(BASE_URL)
                    and "/dll-" not in href
                    and "/category/" not in href
                    and "/page/" not in href
                    and href not in seen
                ):
                    seen.add(href)
                    page_links.append(href)
                    game_urls.append(href)

        # Fallback: also look for any a[href] that looks like a game page
        # (in case the article selector misses some entries)
        if not page_links:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if (
                    href.startswith(BASE_URL)
                    and "/dll-" not in href
                    and "/category/" not in href
                    and "/page/" not in href
                    and "/ps5-games" not in href.replace("/category/ps5/ps5-games", "")
                    and href != BASE_URL + "/"
                    and href != BASE_URL
                    and href.endswith("/")
                    and href not in seen
                    and re.match(r"^https://www\.superpsx\.com/[a-z0-9][\w\-]*\/?$", href)
                ):
                    seen.add(href)
                    page_links.append(href)
                    game_urls.append(href)

        if not page_links:
            log.info("  page %d: 0 game links found — end of pagination", page)
            break

        log.info("  page %d: %d games", page, len(page_links))

        # Check for next page
        next_link = soup.select_one("a.next.page-numbers[href]")
        if not next_link:
            log.info("  no 'next' link found — end of pagination")
            break

        page += 1
        time.sleep(PAGE_DELAY)

    log.info("Total: %d games discovered across %d pages", len(game_urls), page)
    return game_urls


# ---------------------------------------------------------------------------
# Phase 2: Parse game page (extract metadata + DLL page link)
# ---------------------------------------------------------------------------


def extract_poster_url(soup: BeautifulSoup, page_url: str) -> str | None:
    """Extract the cover/poster image URL.

    Priority:
      1. og:image / og:image:secure_url meta tags
      2. a.penci-image-holder[data-bgset]
      3. First <img> in the article body
    """
    # OpenGraph image
    for prop in ("og:image", "og:image:secure_url"):
        og = soup.find("meta", property=prop)
        if og and og.get("content"):
            return og["content"]

    # Penci image holder (used on category/game pages)
    holder = soup.select_one("a.penci-image-holder[data-bgset]")
    if holder:
        bgset = holder.get("data-bgset", "")
        if bgset:
            # data-bgset may contain multiple URLs separated by commas
            # Take the last (highest resolution) one
            urls = [u.strip().split()[0] for u in bgset.split(",") if u.strip()]
            if urls:
                return urls[-1]

    # First image in article
    article = soup.find("article") or soup
    img = article.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = urljoin(page_url, src)
            return src

    return None


def parse_info_table(soup: BeautifulSoup) -> dict[str, str]:
    """Parse the game info table (table.has-fixed-layout) on the game page.

    Extracts key/value pairs from <td> cells. Keys include:
    Game Name, Platform, Genre, Mode, Release Date, Size, Version, Update
    """
    info: dict[str, str] = {}

    # Find the first has-fixed-layout table on the game page
    table = soup.select_one("table.has-fixed-layout")
    if not table:
        return info

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True).rstrip(":")
            value = cells[1].get_text(strip=True)
            if key and value:
                info[key] = value

    return info


def find_dll_page_url(soup: BeautifulSoup, page_url: str) -> str | None:
    """Find the DLL page link on the game page.

    Looks for links matching /dll-* pattern, either via href selector or regex.
    """
    # Method 1: Direct link search
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/dll-" in href and "superpsx.com" in href:
            if not href.startswith("http"):
                href = urljoin(page_url, href)
            return href

    # Method 2: Regex search in HTML source
    html_str = str(soup)
    m = DLL_LINK_RE.search(html_str)
    if m:
        return m.group(1)

    return None


def parse_game_page(url: str) -> dict | None:
    """Parse a game page to extract metadata and the DLL page URL.

    Returns a dict with: title, poster_url, info_table, dll_url, or None on failure.
    """
    log.info("  → Game page: %s", url)

    cached_html = _cache_get(url)
    if cached_html:
        log.debug("    (from disk cache)")
        html = cached_html
    else:
        try:
            resp = http_get(url)
        except Exception as exc:
            log.warning("    unreachable: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("    HTTP %d", resp.status_code)
            return None
        html = resp.text
        _cache_set(url, html)

    soup = BeautifulSoup(html, "html.parser")

    # Title: prefer og:title, strip " - SuperPSX" suffix
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"]
        title = re.sub(r"\s*[-–]\s*(SuperPSX|Download|PS5).*$", "", title, flags=re.I).strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # Poster URL
    poster_url = extract_poster_url(soup, url)

    # Info table
    info = parse_info_table(soup)

    # DLL page URL
    dll_url = find_dll_page_url(soup, url)

    if not dll_url:
        log.debug("    no DLL page link found on game page")
        return None

    return {
        "title": title,
        "poster_url": poster_url,
        "info": info,
        "dll_url": dll_url,
        "game_url": url,
    }


# ---------------------------------------------------------------------------
# Phase 3: Parse DLL page (extract download links, firmware info, formats)
# ---------------------------------------------------------------------------


def _split_on_separator(text: str) -> list[str]:
    """Split text on the ⇛ separator, returning (label, value) parts."""
    if SEPARATOR in text:
        parts = text.split(SEPARATOR, 1)
        return [p.strip() for p in parts]
    return [text.strip()]


def _extract_download_links_from_cell(cell: Tag) -> list[dict[str, str]]:
    """Extract download links from a table cell.

    Looks for <a> tags with data-penci-link="external" and rel="nofollow".
    Also handles text-only hosts (FileK) that appear as plain text between " – " separators.
    """
    links: list[dict[str, str]] = []

    # Method 1: <a data-penci-link="external" rel="nofollow">
    for a in cell.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        penci_link = a.get("data-penci-link", "")
        rel = a.get("rel", [])

        # Accept links that are external or have nofollow rel
        if (
            penci_link == "external"
            or "nofollow" in (rel if isinstance(rel, list) else [rel])
            or (href.startswith("http") and "superpsx.com" not in href)
        ):
            if href and not is_non_host_url(href):
                name = extract_mirror_name(href, text)
                links.append({"name": name, "url": href})

    # Method 2: Any other <a href> pointing to known host patterns
    for a in cell.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and not is_non_host_url(href):
            # Check if it matches a known mirror pattern
            already_found = any(l["url"] == href for l in links)
            if not already_found:
                name = extract_mirror_name(href, a.get_text(strip=True))
                links.append({"name": name, "url": href})

    # Method 3: Text-only hosts (e.g., "FileK") that appear as plain text
    # These are separated by " – " in the cell text
    if not links:
        cell_text = cell.get_text(strip=True)
        if " – " in cell_text or " - " in cell_text:
            parts = re.split(r"\s*[–\-]\s*", cell_text)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                part_lower = part.lower()
                # Check if this part is a known host alias
                if part_lower in HOST_ALIASES:
                    # Text-only host: we include it with empty URL
                    # (or skip it, depending on configuration)
                    links.append({"name": HOST_ALIASES[part_lower], "url": ""})

    return links


def _detect_section_label(table: Tag) -> str | None:
    """Detect if a table is a section separator.

    Separator tables have no has-fixed-layout class, contain a single <td>
    with a PPSA ID and possibly a variant label (e.g., 'exFAT')."""
    if table.get("class") and "has-fixed-layout" in " ".join(table.get("class", [])):
        return None

    cells = table.find_all("td")
    if len(cells) == 1:
        text = cells[0].get_text(strip=True)
        if PPSA_RE.search(text):
            return text
    return None


def parse_dll_page(url: str) -> dict | None:
    """Parse a DLL page to extract download links, firmware, and format info.

    DLL pages contain:
    - Header tables (no has-fixed-layout): host info like "(AKR, Viki, and OneFile)"
    - Data tables (has-fixed-layout): rows with ⇛ separators
    - Separator tables: single-cell tables with PPSA ID marking new sections

    Returns a dict with:
      - links: list of {name, url, group} download links
      - title_id: PPSA ID
      - version: normalized version string
      - region: region code (USA, EUR, etc.)
      - firmware: firmware requirement
      - firmware_compat: firmware compatibility range
      - password: always "SuperPSX"
      - voices: voice languages
      - screen_languages: subtitle languages
      - file_formats: list of detected format tags
      - notes: additional notes
      - sections: list of section info (for multi-section pages)
    """
    log.info("  → DLL page: %s", url)

    cached_html = _cache_get(url)
    if cached_html:
        log.debug("    (from disk cache)")
        html = cached_html
    else:
        try:
            resp = http_get(url)
        except Exception as exc:
            log.warning("    unreachable: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("    HTTP %d", resp.status_code)
            return None
        html = resp.text
        _cache_set(url, html)

    soup = BeautifulSoup(html, "html.parser")

    # Collect all tables on the page
    all_tables = soup.find_all("table")

    # Track current section (for multi-section pages)
    current_section = ""
    current_section_label = ""

    # Results
    all_links: list[dict[str, str]] = []
    title_id: str | None = None
    version: str | None = None
    region: str | None = None
    firmware: str | None = None
    firmware_compat: str | None = None
    password: str | None = None
    voices: str | None = None
    screen_languages: str | None = None
    notes: list[str] = []
    file_formats: list[str] = []
    sections: list[dict] = []

    for table in all_tables:
        table_classes = " ".join(table.get("class", []))

        # Check if this is a separator table (marks new section)
        section_label = _detect_section_label(table)
        if section_label is not None:
            current_section = section_label
            # Try to extract variant label (e.g., "exFAT")
            section_lower = section_label.lower()
            if "exfat" in section_lower:
                current_section_label = "exFAT"
            elif "backport" in section_lower:
                current_section_label = "Backport"
            else:
                current_section_label = ""

            sections.append({
                "label": section_label,
                "variant": current_section_label,
            })
            log.debug("    section: %s", section_label)
            continue

        # Only process data tables (has-fixed-layout)
        if "has-fixed-layout" not in table_classes:
            # Header table: skip (contains host info)
            continue

        # Parse rows in the data table
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            label_cell = cells[0]
            value_cell = cells[1] if len(cells) >= 2 else cells[0]

            label_text = label_cell.get_text(strip=True)
            value_text = value_cell.get_text(strip=True)

            # Check for ⇛ separator in the label or value cell.
            # SuperPSX DLL tables have two common layouts:
            #   1. <td>Version ⇛</td><td>PPSA02182 – USA</td>
            #      → ⇛ is at end of label cell, value is in second cell
            #   2. <td>Version</td><td>⇛ PPSA02182 – USA</td>
            #      → ⇛ is at start of value cell
            #   3. <td>Game (v01.024) [APR-EMU] ⇛</td><td>download links</td>
            #      → ⇛ at end of label, download links in value cell
            #
            # row_label: the key (before ⇛)
            # row_info: any text after ⇛ in the same cell (usually empty)
            # value_text: full text of the second cell
            # full_info: combined row_info + value_text for searching
            if SEPARATOR in label_text:
                parts = label_text.split(SEPARATOR, 1)
                row_label = parts[0].strip()
                row_info = parts[1].strip() if len(parts) > 1 else ""
            elif SEPARATOR in value_text:
                # Separator is in the value cell: "⇛ PPSA02182 – USA"
                row_label = label_text.rstrip(":")
                val_parts = value_text.split(SEPARATOR, 1)
                row_info = val_parts[-1].strip() if val_parts else ""
            else:
                # No separator found — treat as key/value pair
                row_label = label_text.rstrip(":")
                row_info = ""

            # full_info combines any text after ⇛ in the label cell
            # with the full value cell text — used for extracting PPSA IDs,
            # firmware, region, etc.
            full_info = f"{row_info} {value_text}".strip()

            # Process based on row label
            row_label_lower = row_label.lower()

            # Version row: "Version ⇛ PPSA02182 – USA [Thank @CREDITS]"
            # The PPSA ID and region are in the value cell (full_info)
            if row_label_lower == "version":
                # Extract title ID from full_info (combines label-after-⇛ + value cell)
                ppsa_match = PPSA_RE.search(full_info)
                if ppsa_match:
                    title_id = ppsa_match.group(1).upper()

                # Extract region (USA, EUR, ASIA, JPN, etc.)
                region_match = re.search(
                    r"[–\-]\s*([A-Z]{2,5})\b", full_info,
                )
                if region_match:
                    region = region_match.group(1)

            # Game row: "Game (vXX.XXX) [TAG] ⇛ download links"
            elif row_label_lower.startswith("game") or row_label_lower.startswith("update"):
                # Extract version from the label
                ver_match = VERSION_RE.search(row_label)
                if ver_match:
                    version = normalize_version(row_label)

                # Extract format tags [APR-EMU], [FPKG], etc.
                for tag_match in FORMAT_TAG_RE.finditer(row_label):
                    tag = tag_match.group(1)
                    if tag not in file_formats:
                        file_formats.append(tag)

                # Extract download links from value cell
                dl_links = _extract_download_links_from_cell(value_cell)
                group = detect_group(row_label)
                if current_section_label:
                    group = current_section_label

                for link in dl_links:
                    link_name = link["name"]
                    # Prefix with group name for non-Standard groups
                    if group in ("Backport", "DLC", "Fix", "Dump", "exFAT"):
                        link_name = f"{group} - {link_name}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": group,
                    })

            # Fix row: "Fix X.xx (@USER) ⇛ download links"
            elif row_label_lower.startswith("fix"):
                fw_match = FW_FIX_RE.search(row_label)
                if fw_match:
                    notes.append(f"Fix {fw_match.group(1)}")

                dl_links = _extract_download_links_from_cell(value_cell)
                for link in dl_links:
                    link_name = f"Fix - {link['name']}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": "Fix",
                    })

            # Backport row: "Backport X.xx (@USER) ⇛ download links"
            elif row_label_lower.startswith("backport"):
                fw_match = FW_FIX_RE.search(row_label)
                if fw_match:
                    notes.append(f"Backport {fw_match.group(1)}")

                dl_links = _extract_download_links_from_cell(value_cell)
                for link in dl_links:
                    link_name = f"Backport - {link['name']}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": "Backport",
                    })

            # DLC row: "DLC (@USER) ⇛ download links"
            elif row_label_lower.startswith("dlc"):
                dl_links = _extract_download_links_from_cell(value_cell)
                for link in dl_links:
                    link_name = f"DLC - {link['name']}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": "DLC",
                    })

            # FW REQUIRED row
            elif row_label_lower.startswith("fw") and "required" in row_label_lower:
                firmware = full_info.strip()

            # Note row: "Working 10.xx – 5.xx"
            elif row_label_lower == "note":
                compat_match = FW_COMPAT_RE.search(full_info)
                if compat_match:
                    firmware_compat = f"{compat_match.group(1)} – {compat_match.group(2)}"
                if full_info.strip():
                    notes.append(full_info.strip())

            # Password row
            elif row_label_lower == "password":
                password = full_info.strip() or "SuperPSX"

            # Voices / Voice row
            elif row_label_lower in ("voices", "voice"):
                voices = full_info.strip()

            # Screen Languages row
            elif row_label_lower in ("screen languages", "screenlanguages"):
                screen_languages = full_info.strip()

    # Default password
    if not password:
        password = "SuperPSX"

    # Default version from info table or section label
    if not version:
        # Try to extract from section label or table text
        full_text = soup.get_text(" ", strip=True)
        ver_match = VERSION_RE.search(full_text)
        if ver_match:
            version = normalize_version(full_text)

    # Detect file formats from page text if not already found
    if not file_formats:
        full_text = soup.get_text(" ", strip=True)
        for tag_match in FORMAT_TAG_RE.finditer(full_text):
            tag = tag_match.group(1)
            if tag not in file_formats and tag not in ("PS5", "PS4", "PKG"):
                file_formats.append(tag)

        # Fallback: check for format keywords
        if "apr-emu" in full_text.lower() or "ampr-emu" in full_text.lower():
            if "APR-EMU" not in file_formats:
                file_formats.append("APR-EMU")
        if "fpkg" in full_text.lower():
            if "FPKG" not in file_formats:
                file_formats.append("FPKG")
        if "pkg" in full_text.lower() and "PKG" not in file_formats:
            file_formats.append("PKG")

    if not file_formats:
        file_formats = ["unknown"]

    return {
        "links": all_links,
        "title_id": title_id,
        "version": version,
        "region": region,
        "firmware": firmware,
        "firmware_compat": firmware_compat,
        "password": password,
        "voices": voices,
        "screen_languages": screen_languages,
        "file_formats": file_formats,
        "notes": notes,
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Phase 4: Combine game page + DLL page data into a package entry
# ---------------------------------------------------------------------------


def scrape_game(game_url: str) -> dict | None:
    """Scrape a single game: game page → DLL page → combined package.

    Returns a package dict matching the dlpsgame-ps5.json format, or None on failure.
    """
    # Step 1: Parse game page
    game_data = parse_game_page(game_url)
    if not game_data:
        log.warning("  ✗ No game data for %s", game_url)
        return None

    dll_url = game_data.get("dll_url")
    if not dll_url:
        log.warning("  ✗ No DLL page link found for %s", game_url)
        return None

    # Step 2: Parse DLL page
    dll_data = parse_dll_page(dll_url)
    if not dll_data:
        log.warning("  ✗ No DLL data for %s", dll_url)
        return None

    # Step 3: Combine data into package format
    info = game_data.get("info", {})
    title = game_data.get("title", "")

    # Title ID: prefer from DLL page (most reliable), then info table, then game URL
    title_id = dll_data.get("title_id")
    if not title_id:
        # Try from info table Version field
        version_str = info.get("Version", "")
        ppsa_match = PPSA_RE.search(version_str)
        if ppsa_match:
            title_id = ppsa_match.group(1).upper()
    if not title_id:
        # Try from game page HTML
        title_id = ""

    # Version: prefer from DLL page, then info table
    version = dll_data.get("version")
    if not version:
        update_str = info.get("Update", "")
        if update_str:
            version = normalize_version(update_str)
        else:
            version_str = info.get("Version", "")
            if version_str:
                version = normalize_version(version_str)
    if not version:
        version = "01.000"

    # Size: from info table or DLL page notes
    size_str = info.get("Size", "")
    size_bytes = parse_size_bytes(size_str)
    if not size_bytes:
        # Try from DLL page notes
        for note in dll_data.get("notes", []):
            sb = parse_size_bytes(note)
            if sb:
                size_bytes = sb
                break

    # Region: from DLL page or info table
    region = dll_data.get("region")
    if not region:
        version_str = info.get("Version", "")
        region_match = re.search(r"[–\-]\s*([A-Z]{2,5})\b", version_str)
        if region_match:
            region = region_match.group(1)

    # Build description
    tags: list[str] = []
    if title_id:
        tags.append(title_id)
    # Version tag
    ver_tag_match = VERSION_RE.search(version) if version else None
    if ver_tag_match:
        tags.append(f"v{ver_tag_match.group(1)}")
    else:
        tags.append(f"v{version}")
    # Firmware compatibility
    firmware = dll_data.get("firmware")
    firmware_compat = dll_data.get("firmware_compat")
    if firmware:
        tags.append(firmware)
    if firmware_compat:
        tags.append(firmware_compat)
    if region:
        tags.append(region)

    desc_lines: list[str] = []
    if tags:
        desc_lines.append(f"Tags: {', '.join(tags)}")
    if size_str:
        desc_lines.append(f"Size: {size_str}")
    # Password
    password = dll_data.get("password", "SuperPSX")
    desc_lines.append(f"Password: {password}")
    # Firmware requirement
    if firmware:
        desc_lines.append(f"FW Required: {firmware}")
    if firmware_compat:
        desc_lines.append(f"FW Compat: {firmware_compat}")
    # Voices
    voices = dll_data.get("voices")
    if voices:
        desc_lines.append(f"Voices: {voices}")
    # Notes
    for note in dll_data.get("notes", []):
        if note and note not in (firmware, firmware_compat):
            desc_lines.append(note)

    description = "\n".join(desc_lines)

    # Download links (deduplicated by URL)
    seen_urls: set[str] = set()
    download_links: list[dict[str, str]] = []
    for link in dll_data.get("links", []):
        url = link.get("url", "")
        name = link.get("name", "Mirror")
        # Skip empty-URL text-only hosts (FileK etc.)
        if not url:
            log.debug("    skip text-only host: %s", name)
            continue
        # Skip non-host URLs
        if is_non_host_url(url):
            log.debug("    skip non-host: %s — %s", name, url)
            continue
        # Deduplicate
        if url in seen_urls:
            continue
        seen_urls.add(url)
        download_links.append({"name": name, "url": url})

    if not download_links:
        log.warning("  ✗ No download links for %s", game_url)
        return None

    # Build package
    package: dict = {
        "titleId": title_id,
        "title": title,
        "version": version,
        "category": "game",
        "posterUrl": game_data.get("poster_url"),
        "description": description,
        "downloadLinks": download_links,
        "sizeBytes": size_bytes,
        "downloadSource": dll_url,
        "source": SITE_SOURCE,
        "fileFormat": dll_data.get("file_formats", ["unknown"]),
    }

    # Only include sizeBytes if known (null can cause issues in some consumers)
    if not size_bytes:
        del package["sizeBytes"]

    log.info("    ✓ %s — %d links", title, len(download_links))
    return package


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def scrape_all(
    max_pages: int | None,
    max_games: int | None,
    concurrency: int,
) -> tuple[list[dict], list[str]]:
    """Main scraping pipeline.

    1. Discover game URLs from category pages
    2. For each game, scrape game page + DLL page
    3. Return (packages, warnings)
    """
    game_urls = discover_game_urls(max_pages)
    if max_games is not None:
        game_urls = game_urls[:max_games]

    log.info("Scraping %d games (concurrency=%d)...", len(game_urls), concurrency)

    packages: list[dict] = []
    warnings: list[str] = []
    failed_urls: list[str] = []

    if concurrency <= 1:
        for i, url in enumerate(game_urls, 1):
            log.info("[%d/%d]", i, len(game_urls))
            try:
                pkg = scrape_game(url)
                if pkg:
                    packages.append(pkg)
                else:
                    warnings.append(url)
                    failed_urls.append(url)
            except Exception as exc:
                log.warning("  ✗ %s: %s", url, exc)
                warnings.append(f"{url}: {exc}")
                failed_urls.append(url)
            time.sleep(PAGE_DELAY)
    else:
        with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(scrape_game, url): url for url in game_urls}
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

    # === Phase 2: Retry failed pages ===
    if failed_urls:
        log.info("=" * 60)
        log.info("Phase 2: retrying %d failed page(s)", len(failed_urls))
        log.info("=" * 60)

        # Invalidate cache for failed URLs
        for url in failed_urls:
            for cache_key_fn in [_cache_key]:
                cache_file = DISK_CACHE_DIR / cache_key_fn(url)
                if cache_file.exists():
                    try:
                        cache_file.unlink()
                    except Exception:
                        pass

        retry_success = 0
        still_failing: list[str] = []
        for i, url in enumerate(failed_urls, 1):
            log.info("[retry %d/%d]", i, len(failed_urls))
            try:
                pkg = scrape_game(url)
                if pkg:
                    packages.append(pkg)
                    retry_success += 1
                else:
                    still_failing.append(url)
            except Exception as exc:
                still_failing.append(url)
            time.sleep(PAGE_DELAY * 2)

        log.info(
            "Phase 2 complete: %d success, %d still failing",
            retry_success,
            len(still_failing),
        )
        warnings = still_failing

    # Sort by title
    packages.sort(key=lambda p: (p.get("title") or "").lower())
    return packages, warnings


def build_catalog(packages: list[dict]) -> dict:
    """Build the final JSON catalog structure."""
    return {
        "name": "SuperPSX PS5",
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": PS5_CATEGORY_URL,
        "packages": packages,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SuperPSX PS5 Games Scraper — produces a JSON catalog "
                    "compatible with dlpsgame-ps5.json format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Full scrape (all pages)
  python scrape_superpsx.py

  # Limited scrape for testing
  python scrape_superpsx.py --max-pages 2 --max-games 10

  # Custom output path
  python scrape_superpsx.py --out superpsx-ps5.json

  # Verbose mode with single-threaded scraping
  python scrape_superpsx.py --verbose --concurrency 1

  # With FlareSolverr backend (placeholder)
  python scrape_superpsx.py --http-backend flaresolverr
""",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Limit the number of category pages to crawl",
    )
    parser.add_argument(
        "--max-games", type=int, default=None,
        help="Limit the number of games to scrape (useful for testing)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("superpsx-ps5.json"),
        help="Output JSON file path (default: superpsx-ps5.json)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Number of threads for scraping game pages (default: 1)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay in seconds between requests (default: 1.0)",
    )
    parser.add_argument(
        "--http-backend", choices=["curl", "flaresolverr"],
        default="curl",
        help="HTTP backend: 'curl' (default) or 'flaresolverr' (placeholder)",
    )
    parser.add_argument(
        "--flaresolverr-url", default=None,
        help="FlareSolverr URL (default: http://localhost:8191/v1)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable disk cache (.scrape_cache_superpsx/) — force full re-scrape",
    )
    parser.add_argument(
        "--mode", choices=["full", "incremental"], default="full",
        help="Scrape mode: 'full' (re-scrape everything) or 'incremental' "
             "(only new/updated games). Default: full",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    # Apply configuration
    global _HTTP_BACKEND, FLARESOLVERR_URL, DISK_CACHE_ENABLED, PAGE_DELAY
    _HTTP_BACKEND = args.http_backend
    PAGE_DELAY = args.delay

    if args.no_cache:
        DISK_CACHE_ENABLED = False
    if args.flaresolverr_url:
        FLARESOLVERR_URL = args.flaresolverr_url

    if _HTTP_BACKEND == "flaresolverr" and args.concurrency > 1:
        log.warning("FlareSolverr does not support concurrency — forcing concurrency=1")
        args.concurrency = 1

    log.info(
        "Starting SuperPSX scraper (backend: %s, concurrency: %d, delay: %.1fs, mode: %s)",
        _HTTP_BACKEND, args.concurrency, PAGE_DELAY, args.mode,
    )

    # Load manifest for incremental mode
    manifest = None
    if args.mode == "incremental":
        try:
            from scrape_manifest import ScrapeManifest
        except ImportError:
            ScrapeManifest = None  # type: ignore[assignment,misc]
        if ScrapeManifest:
            manifest = ScrapeManifest(path=".scrape_manifest_superpsx.json")
            log.info("Incremental mode: manifest has %d entries",
                     len(manifest._data.get("entries", {})))
        else:
            log.warning("scrape_manifest module not found — running in full mode")

    try:
        packages, warnings = scrape_all(
            max_pages=args.max_pages,
            max_games=args.max_games,
            concurrency=args.concurrency,
        )
    except KeyboardInterrupt:
        log.info("Interrupted by user — saving partial results")
        packages, warnings = [], []
    except Exception as exc:
        log.error("Fatal error: %s", exc)
        return 1

    # Save manifest with scraped packages
    if manifest:
        for pkg in packages:
            source_url = pkg.get("downloadSource", "")
            if source_url:
                manifest.record(source_url, json.dumps(pkg, ensure_ascii=False), package=pkg)
        manifest.save()
        log.info("Manifest saved with %d entries", len(manifest._data.get("entries", {})))

    catalog = build_catalog(packages)

    # Write output
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "Written: %s  (%d games, %d warnings)",
        args.out, len(packages), len(warnings),
    )

    # Write warnings file
    if warnings:
        warn_path = args.out.with_suffix(".warnings.txt")
        warn_path.write_text("\n".join(str(w) for w in warnings), encoding="utf-8")
        log.info("Warnings written: %s", warn_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
