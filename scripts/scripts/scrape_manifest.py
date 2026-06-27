#!/usr/bin/env python3
"""
Manifest tracking system for incremental scraping.
===================================================
Tracks which game URLs have been scraped, their content hashes, and when they
were last seen. Allows the scraper to skip unchanged games in incremental mode.

Manifest file format (``.scrape_manifest.json``):
    {
      "version": 1,
      "updated_at": "ISO-8601",
      "entries": {
        "https://dlpsgame.com/game-ps5/": {
          "titleId": "PPSA01968",
          "title": "Death Stranding",
          "content_hash": "abc123def4567890",
          "last_seen": "2026-06-24T04:00:00+00:00",
          "link_count": 11,
          "package": null
        }
      }
    }

Usage:
    python scrape_manifest.py stats [--manifest PATH]
    python scrape_manifest.py list [--manifest PATH]
    python scrape_manifest.py clean --max-age 30 [--manifest PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST_PATH = Path(".scrape_manifest.json")
MANIFEST_VERSION = 1
INCREMENTAL_MAX_AGE_DAYS = 7
CONTENT_HASH_LENGTH = 16  # first 16 hex chars of SHA-256

LOG = logging.getLogger("scrape_manifest")


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def content_hash(html: str) -> str:
    """Compute a short content hash of HTML.

    Normalizes whitespace (strip, collapse multiple spaces to one) then
    returns the first 16 hex characters of the SHA-256 digest.
    """
    normalized = re.sub(r"\s+", " ", html.strip())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:CONTENT_HASH_LENGTH]


# ---------------------------------------------------------------------------
# ScrapeManifest
# ---------------------------------------------------------------------------

class ScrapeManifest:
    """Manifest tracking system for incremental scraping.

    Persists scrape state to a JSON file so that subsequent runs can skip
    unchanged pages and only re-visit pages whose content has changed or
    whose last scrape is stale.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_MANIFEST_PATH
        self._data: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "updated_at": None,
            "entries": {},
        }
        self._load()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load manifest from disk (no-op if file missing or corrupt)."""
        if not self.path.exists():
            LOG.debug("Manifest file not found at %s — starting fresh", self.path)
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Cannot read manifest %s: %s — starting fresh", self.path, exc)
            return
        if not isinstance(data, dict) or "entries" not in data:
            LOG.warning("Manifest %s has unexpected format — starting fresh", self.path)
            return
        if data.get("version") != MANIFEST_VERSION:
            LOG.warning(
                "Manifest version mismatch (got %s, expected %s) — starting fresh",
                data.get("version"),
                MANIFEST_VERSION,
            )
            return
        self._data = data
        LOG.info("Loaded manifest with %d entries from %s", len(data["entries"]), self.path)

    def save(self) -> None:
        """Persist manifest to disk."""
        self._data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            LOG.debug("Manifest saved to %s (%d entries)", self.path, len(self._data["entries"]))
        except OSError as exc:
            LOG.error("Failed to save manifest to %s: %s", self.path, exc)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def needs_scrape(self, url: str, mode: str = "incremental") -> bool:
        """Determine whether *url* needs to be (re-)scraped.

        Args:
            url: The game page URL.
            mode: ``"full"`` forces re-scraping everything;
                  ``"incremental"`` skips unchanged recent pages.

        Returns:
            True if the page should be scraped.
        """
        if mode == "full":
            return True

        entry = self._data["entries"].get(url)
        if entry is None:
            # New URL — must scrape
            return True

        # Check staleness: older than INCREMENTAL_MAX_AGE_DAYS
        last_seen_str = entry.get("last_seen")
        if last_seen_str:
            try:
                last_seen = dt.datetime.fromisoformat(last_seen_str)
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=dt.timezone.utc)
                age = dt.datetime.now(dt.timezone.utc) - last_seen
                if age.days > INCREMENTAL_MAX_AGE_DAYS:
                    LOG.debug("Stale entry (%d days old): %s", age.days, url)
                    return True
            except (ValueError, TypeError):
                LOG.warning("Invalid last_seen timestamp for %s — will re-scrape", url)
                return True
        else:
            # No last_seen — re-scrape
            return True

        # Content hash change is detected by ``record()`` comparing
        # the new hash to the stored one.  At query time we only
        # check staleness; if the hash *has* changed, ``record()``
        # will update it and the next ``needs_scrape`` call within the
        # same run will already see the new hash.
        # However, if we *already know* the hash changed (record was
        # called previously in this session), we still return False
        # here because the data is fresh.
        return False

    def record(self, url: str, html: str, package: dict | None = None) -> None:
        """Record a scrape result for *url*.

        Args:
            url: The game page URL.
            html: The raw HTML content of the page.
            package: The parsed package dict (titleId, title, downloadLinks, …).
                     Stored as the cached ``package`` value so it can be
                     reused without re-parsing.
        """
        new_hash = content_hash(html)
        link_count = 0
        title_id = ""
        title = ""

        if package:
            link_count = len(package.get("downloadLinks", []))
            title_id = package.get("titleId", "")
            title = package.get("title", "")

        entry: dict[str, Any] = {
            "titleId": title_id,
            "title": title,
            "content_hash": new_hash,
            "last_seen": dt.datetime.now(dt.timezone.utc).isoformat(),
            "link_count": link_count,
            "package": package,
        }
        self._data["entries"][url] = entry
        LOG.debug("Recorded %s — hash=%s links=%d", url, new_hash, link_count)

    def filter_urls(self, urls: list[str], mode: str = "incremental") -> list[str]:
        """Filter *urls* to only those that need scraping under *mode*.

        Args:
            urls: List of game page URLs.
            mode: ``"full"`` or ``"incremental"``.

        Returns:
            Subset of *urls* that should be scraped.
        """
        result = [u for u in urls if self.needs_scrape(u, mode)]
        skipped = len(urls) - len(result)
        LOG.info(
            "filter_urls: %d total, %d need scrape, %d skipped (mode=%s)",
            len(urls),
            len(result),
            skipped,
            mode,
        )
        return result

    def get_cached_package(self, url: str) -> dict | None:
        """Return the cached parsed package for *url*, if available.

        This avoids re-parsing HTML for unchanged pages.
        """
        entry = self._data["entries"].get(url)
        if entry is None:
            return None
        return entry.get("package")

    # ------------------------------------------------------------------
    # Stats & maintenance
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return manifest statistics as a dict."""
        entries = self._data["entries"]
        total = len(entries)

        link_counts = [e.get("link_count", 0) for e in entries.values()]
        total_links = sum(link_counts)
        avg_links = round(total_links / total, 1) if total else 0.0

        # Age distribution
        now = dt.datetime.now(dt.timezone.utc)
        ages: list[int] = []
        for entry in entries.values():
            ls = entry.get("last_seen")
            if ls:
                try:
                    last_seen = dt.datetime.fromisoformat(ls)
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=dt.timezone.utc)
                    ages.append((now - last_seen).days)
                except (ValueError, TypeError):
                    ages.append(-1)
            else:
                ages.append(-1)

        valid_ages = [a for a in ages if a >= 0]
        stale_count = sum(1 for a in valid_ages if a > INCREMENTAL_MAX_AGE_DAYS)
        fresh_count = sum(1 for a in valid_ages if a <= INCREMENTAL_MAX_AGE_DAYS)

        with_package = sum(1 for e in entries.values() if e.get("package"))

        return {
            "total_entries": total,
            "fresh_entries": fresh_count,
            "stale_entries": stale_count,
            "entries_without_timestamp": sum(1 for a in ages if a < 0),
            "total_download_links": total_links,
            "avg_links_per_entry": avg_links,
            "cached_packages": with_package,
            "manifest_path": str(self.path),
            "updated_at": self._data.get("updated_at"),
        }

    def clean(self, max_age_days: int = 30) -> int:
        """Remove entries older than *max_age_days*.

        Returns:
            Number of entries removed.
        """
        now = dt.datetime.now(dt.timezone.utc)
        to_remove: list[str] = []

        for url, entry in self._data["entries"].items():
            ls = entry.get("last_seen")
            if not ls:
                to_remove.append(url)
                continue
            try:
                last_seen = dt.datetime.fromisoformat(ls)
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=dt.timezone.utc)
                if (now - last_seen).days > max_age_days:
                    to_remove.append(url)
            except (ValueError, TypeError):
                to_remove.append(url)

        for url in to_remove:
            del self._data["entries"][url]

        if to_remove:
            self.save()

        LOG.info("Cleaned %d entries older than %d days", len(to_remove), max_age_days)
        return len(to_remove)

    def list_entries(self) -> list[dict[str, Any]]:
        """Return all entries as a list of dicts with URL included."""
        result: list[dict[str, Any]] = []
        for url, entry in self._data["entries"].items():
            row = {"url": url}
            row.update(entry)
            # Omit the full package blob for readability
            row.pop("package", None)
            result.append(row)
        # Sort by title for readability
        result.sort(key=lambda e: (e.get("title") or "").lower())
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_stats(args: argparse.Namespace) -> int:
    manifest = ScrapeManifest(path=args.manifest)
    stats = manifest.get_stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    manifest = ScrapeManifest(path=args.manifest)
    entries = manifest.list_entries()
    for entry in entries:
        last_seen = entry.get("last_seen", "never")
        hash_val = entry.get("content_hash", "n/a")
        links = entry.get("link_count", 0)
        title_id = entry.get("titleId", "????")
        title = entry.get("title", "(untitled)")
        url = entry.get("url", "")
        print(f"  {title_id:16s}  {title:50s}  links={links:<4d}  hash={hash_val}  seen={last_seen}")
        if args.verbose:
            print(f"  {'':16s}  {url}")
    print(f"\nTotal: {len(entries)} entries")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    manifest = ScrapeManifest(path=args.manifest)
    removed = manifest.clean(max_age_days=args.max_age)
    print(f"Removed {removed} entries older than {args.max_age} days")
    if removed:
        stats = manifest.get_stats()
        print(f"Remaining: {stats['total_entries']} entries")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape manifest tracking utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python scrape_manifest.py stats
  python scrape_manifest.py list --verbose
  python scrape_manifest.py clean --max-age 30
""",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help=f"Path to manifest file (default: {DEFAULT_MANIFEST_PATH})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # stats
    sub_stats = subparsers.add_parser("stats", help="Show manifest statistics")
    sub_stats.set_defaults(func=_cmd_stats)

    # list
    sub_list = subparsers.add_parser("list", help="List all manifest entries")
    sub_list.set_defaults(func=_cmd_list)

    # clean
    sub_clean = subparsers.add_parser("clean", help="Remove old entries from manifest")
    sub_clean.add_argument(
        "--max-age",
        type=int,
        default=30,
        help="Remove entries older than N days (default: 30)",
    )
    sub_clean.set_defaults(func=_cmd_clean)

    args = parser.parse_args(argv)

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    if not args.command:
        parser.print_help()
        return 2

    try:
        return args.func(args)
    except Exception as exc:
        LOG.error("Command failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
