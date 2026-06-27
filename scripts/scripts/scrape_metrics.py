#!/usr/bin/env python3
"""
Metrics and diff tracking utility for PS5 catalog JSON files.
================================================================
Generates detailed metrics reports, diffs between two catalog versions,
and health checks.

Usage:
    python scrape_metrics.py metrics catalog.json [--out metrics.json]
    python scrape_metrics.py diff old.json new.json [--out diff.json]
    python scrape_metrics.py health catalog.json [--min-games 500]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACEHOLDER_TITLEID_RE = re.compile(r"^GAME_[A-Z0-9]+$", re.IGNORECASE)
REAL_TITLEID_RE = re.compile(r"^[A-Z]{4}\d{3,}$")
BYTES_PER_GB = 1024 ** 3

LOG = logging.getLogger("scrape_metrics")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_catalog(path: Path) -> dict[str, Any]:
    """Load and validate a catalog JSON file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if "packages" not in data or not isinstance(data["packages"], list):
        raise SystemExit(f"{path}: not a valid Pegasus catalog (missing 'packages' key)")
    return data


def _bytes_to_gb(size_bytes: int | float | None) -> float | None:
    """Convert bytes to GB, return None for missing/zero values."""
    if size_bytes is None or size_bytes <= 0:
        return None
    return round(size_bytes / BYTES_PER_GB, 2)


def _median(values: list[float]) -> float | None:
    """Compute the median of a list of floats."""
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return round(sorted_vals[mid], 2)
    return round((sorted_vals[mid - 1] + sorted_vals[mid]) / 2, 2)


def _safe_lower(val: Any) -> str:
    """Return a safe lowercase string for comparison."""
    if isinstance(val, str):
        return val.strip().lower()
    return ""


def _game_key(pkg: dict) -> str:
    """Produce a unique key for a game package.

    Uses real titleId if available, otherwise normalized title.
    """
    tid = (pkg.get("titleId") or "").strip().upper()
    if REAL_TITLEID_RE.match(tid):
        return f"id:{tid}"
    title = (pkg.get("title") or "").strip().lower()
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return f"title:{title}"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def generate_metrics(catalog_path: str | Path, output_path: str | Path | None = None) -> dict[str, Any]:
    """Generate metrics for a catalog JSON file.

    Args:
        catalog_path: Path to the catalog JSON file.
        output_path: Optional path to write the metrics JSON. If None,
                     the metrics are only returned (not written to disk).

    Returns:
        A dict with the full metrics report.
    """
    path = Path(catalog_path)
    data = _load_catalog(path)
    packages: list[dict] = data["packages"]

    total_games = len(packages)
    if total_games == 0:
        metrics: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "catalog": path.name,
            "total_games": 0,
            "warnings": ["Catalog contains zero games"],
        }
        if output_path:
            _write_json(metrics, output_path)
        return metrics

    # --- Source distribution ---
    source_counter: Counter[str] = Counter()
    for pkg in packages:
        src = pkg.get("source")
        if isinstance(src, list):
            for s in src:
                source_counter[_safe_lower(s) or "unknown"] += 1
        elif isinstance(src, str) and src.strip():
            source_counter[_safe_lower(src)] += 1
        else:
            source_counter["unknown"] += 1

    # --- Size stats ---
    sizes_gb: list[float] = []
    games_with_size = 0
    games_without_size = 0
    for pkg in packages:
        sb = pkg.get("sizeBytes")
        if sb and isinstance(sb, (int, float)) and sb > 0:
            gb = _bytes_to_gb(sb)
            if gb is not None:
                sizes_gb.append(gb)
                games_with_size += 1
            else:
                games_without_size += 1
        else:
            games_without_size += 1

    size_stats: dict[str, float | None] = {
        "min_gb": min(sizes_gb) if sizes_gb else None,
        "max_gb": max(sizes_gb) if sizes_gb else None,
        "avg_gb": round(sum(sizes_gb) / len(sizes_gb), 1) if sizes_gb else None,
        "median_gb": _median(sizes_gb),
    }

    # --- Poster / titleId ---
    games_with_poster = sum(1 for p in packages if p.get("posterUrl"))
    games_with_titleid = sum(1 for p in packages if p.get("titleId"))
    placeholder_titleids = sum(
        1 for p in packages
        if p.get("titleId") and PLACEHOLDER_TITLEID_RE.match(str(p["titleId"]))
    )

    # --- Download links ---
    total_download_links = 0
    mirror_counter: Counter[str] = Counter()
    for pkg in packages:
        links = pkg.get("downloadLinks", [])
        total_download_links += len(links)
        for link in links:
            name = (link.get("name") or "unknown").strip()
            # Normalize: extract mirror name from compound names like "Backport - Akia"
            if " - " in name:
                parts = name.split(" - ")
                mirror_name = parts[-1].strip()
                mirror_counter[mirror_name] += 1
            else:
                mirror_counter[name] += 1

    avg_links = round(total_download_links / total_games, 1) if total_games else 0.0

    # --- Format distribution ---
    format_counter: Counter[str] = Counter()
    for pkg in packages:
        ff = pkg.get("fileFormat")
        if isinstance(ff, list):
            for f in ff:
                format_counter[str(f).strip()] += 1
        elif isinstance(ff, str) and ff.strip():
            format_counter[ff.strip()] += 1

    # --- Warnings ---
    warnings: list[str] = []
    if games_without_size > total_games * 0.5:
        warnings.append(f"More than 50% of games lack size info ({games_without_size}/{total_games})")
    if placeholder_titleids > 20:
        warnings.append(f"High number of placeholder titleIds: {placeholder_titleids}")
    if total_games < 100:
        warnings.append(f"Very low game count: {total_games}")

    metrics = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "catalog": path.name,
        "total_games": total_games,
        "sources": dict(source_counter.most_common()),
        "games_with_size": games_with_size,
        "games_without_size": games_without_size,
        "games_with_poster": games_with_poster,
        "games_with_titleid": games_with_titleid,
        "placeholder_titleids": placeholder_titleids,
        "avg_links_per_game": avg_links,
        "total_download_links": total_download_links,
        "mirror_distribution": dict(mirror_counter.most_common()),
        "format_distribution": dict(format_counter.most_common()),
        "size_stats": size_stats,
        "warnings_count": len(warnings),
        "warnings": warnings,
    }

    if output_path:
        _write_json(metrics, output_path)

    return metrics


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_catalogs(
    old_path: str | Path,
    new_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare two catalog versions and produce a diff report.

    Args:
        old_path: Path to the older catalog JSON.
        new_path: Path to the newer catalog JSON.
        output_path: Optional path to write the diff JSON.

    Returns:
        A dict describing added, removed, updated, and unchanged games.
    """
    old_data = _load_catalog(Path(old_path))
    new_data = _load_catalog(Path(new_path))

    old_pkgs: dict[str, dict] = {}
    for pkg in old_data["packages"]:
        key = _game_key(pkg)
        old_pkgs[key] = pkg

    new_pkgs: dict[str, dict] = {}
    for pkg in new_data["packages"]:
        key = _game_key(pkg)
        new_pkgs[key] = pkg

    old_keys = set(old_pkgs.keys())
    new_keys = set(new_pkgs.keys())

    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    common_keys = old_keys & new_keys

    # --- Detect updates within common keys ---
    updated_titles: list[dict[str, Any]] = []
    unchanged_count = 0

    for key in sorted(common_keys):
        old_pkg = old_pkgs[key]
        new_pkg = new_pkgs[key]
        changes: list[str] = []

        # Check link count change
        old_links = old_pkg.get("downloadLinks", [])
        new_links = new_pkg.get("downloadLinks", [])
        if len(old_links) != len(new_links):
            changes.append("link_count_changed")

        # Check for new/removed links by URL
        old_urls = {l.get("url", "") for l in old_links}
        new_urls = {l.get("url", "") for l in new_links}
        added_urls = new_urls - old_urls
        removed_urls = old_urls - new_urls
        if added_urls:
            changes.append("new_link")
        if removed_urls:
            changes.append("link_removed")

        # Check size change
        old_size = old_pkg.get("sizeBytes")
        new_size = new_pkg.get("sizeBytes")
        if old_size != new_size:
            if old_size and new_size:
                changes.append("size_changed")
            elif new_size and not old_size:
                changes.append("size_added")
            elif old_size and not new_size:
                changes.append("size_removed")

        # Check version change
        if old_pkg.get("version") != new_pkg.get("version"):
            changes.append("version_changed")

        # Check poster change
        if old_pkg.get("posterUrl") != new_pkg.get("posterUrl"):
            changes.append("poster_changed")

        # Check titleId change
        if old_pkg.get("titleId") != new_pkg.get("titleId"):
            changes.append("titleid_changed")

        if changes:
            updated_titles.append({
                "title": new_pkg.get("title", "(unknown)"),
                "titleId": new_pkg.get("titleId", ""),
                "changes": changes,
            })
        else:
            unchanged_count += 1

    # --- Link-level aggregate changes ---
    total_added_links = 0
    total_removed_links = 0
    for key in common_keys:
        old_urls = {l.get("url", "") for l in old_pkgs[key].get("downloadLinks", [])}
        new_urls = {l.get("url", "") for l in new_pkgs[key].get("downloadLinks", [])}
        total_added_links += len(new_urls - old_urls)
        total_removed_links += len(old_urls - new_urls)
    # Links from added games
    for key in added_keys:
        total_added_links += len(new_pkgs[key].get("downloadLinks", []))
    # Links from removed games
    for key in removed_keys:
        total_removed_links += len(old_pkgs[key].get("downloadLinks", []))

    added_titles = sorted(
        [new_pkgs[k].get("title", "(unknown)") for k in added_keys],
        key=str.lower,
    )
    removed_titles = sorted(
        [old_pkgs[k].get("title", "(unknown)") for k in removed_keys],
        key=str.lower,
    )

    diff = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "old_catalog": Path(old_path).name,
        "new_catalog": Path(new_path).name,
        "added": len(added_keys),
        "removed": len(removed_keys),
        "updated": len(updated_titles),
        "unchanged": unchanged_count,
        "added_titles": added_titles,
        "removed_titles": removed_titles,
        "updated_titles": updated_titles,
        "link_changes": {
            "added": total_added_links,
            "removed": total_removed_links,
        },
    }

    if output_path:
        _write_json(diff, output_path)

    return diff


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def health_check(catalog_path: str | Path, min_games: int = 500) -> dict[str, Any]:
    """Perform a health check on a catalog.

    Args:
        catalog_path: Path to the catalog JSON.
        min_games: Minimum expected game count for healthy status.

    Returns:
        A dict with ``status`` (``"healthy"`` / ``"degraded"`` / ``"critical"``),
        game count, and a list of warnings.
    """
    path = Path(catalog_path)
    data = _load_catalog(path)
    packages: list[dict] = data["packages"]
    total_games = len(packages)

    warnings: list[str] = []

    # --- Game count check ---
    if total_games == 0:
        status = "critical"
        warnings.append("Catalog is empty — zero games found")
    elif total_games < min_games * 0.5:
        status = "critical"
        warnings.append(
            f"Game count ({total_games}) is less than 50% of expected minimum ({min_games})"
        )
    elif total_games < min_games:
        status = "degraded"
        warnings.append(
            f"Game count ({total_games}) is below expected minimum ({min_games})"
        )
    else:
        status = "healthy"

    # --- Size coverage check ---
    no_size = sum(
        1 for p in packages
        if not p.get("sizeBytes") or not isinstance(p["sizeBytes"], (int, float)) or p["sizeBytes"] <= 0
    )
    if total_games > 0 and no_size > total_games * 0.7:
        warnings.append(
            f"Very low size coverage: {no_size}/{total_games} games lack size info"
        )
        if status == "healthy":
            status = "degraded"

    # --- Poster coverage check ---
    no_poster = sum(1 for p in packages if not p.get("posterUrl"))
    if total_games > 0 and no_poster > total_games * 0.5:
        warnings.append(
            f"Low poster coverage: {no_poster}/{total_games} games lack poster images"
        )

    # --- Placeholder titleId check ---
    placeholder_count = sum(
        1 for p in packages
        if p.get("titleId") and PLACEHOLDER_TITLEID_RE.match(str(p["titleId"]))
    )
    if placeholder_count > 50:
        warnings.append(
            f"High number of placeholder titleIds: {placeholder_count}"
        )
        if status == "healthy":
            status = "degraded"

    # --- Broken/empty links check ---
    empty_link_games = 0
    for pkg in packages:
        links = pkg.get("downloadLinks", [])
        if not links:
            empty_link_games += 1
        elif all(not l.get("url") for l in links):
            empty_link_games += 1
    if empty_link_games > 0:
        warnings.append(f"{empty_link_games} games have no valid download links")
        if empty_link_games > total_games * 0.1 and status == "healthy":
            status = "degraded"

    # --- Duplicate title check ---
    title_counter: Counter[str] = Counter()
    for pkg in packages:
        t = (pkg.get("title") or "").strip().lower()
        if t:
            title_counter[t] += 1
    duplicates = {t: c for t, c in title_counter.items() if c > 1}
    if duplicates:
        dup_count = len(duplicates)
        warnings.append(f"{dup_count} duplicate title(s) detected (e.g., {list(duplicates.keys())[:3]})")

    return {
        "status": status,
        "total_games": total_games,
        "expected_min": min_games,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# JSON writer
# ---------------------------------------------------------------------------

def _write_json(data: dict, output_path: str | Path) -> None:
    """Write a dict as pretty-printed JSON."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LOG.info("Written: %s", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_metrics(args: argparse.Namespace) -> int:
    metrics = generate_metrics(
        catalog_path=args.catalog,
        output_path=args.out,
    )
    if not args.out:
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
    else:
        # Print a summary to stderr, full JSON written to file
        LOG.info(
            "Metrics: %d games, %d links, %d warnings",
            metrics["total_games"],
            metrics["total_download_links"],
            metrics["warnings_count"],
        )
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    result = diff_catalogs(
        old_path=args.old,
        new_path=args.new,
        output_path=args.out,
    )
    if not args.out:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        LOG.info(
            "Diff: +%d added, -%d removed, ~%d updated, =%d unchanged",
            result["added"],
            result["removed"],
            result["updated"],
            result["unchanged"],
        )
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    result = health_check(
        catalog_path=args.catalog,
        min_games=args.min_games,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    # Return non-zero exit code for critical status
    if result["status"] == "critical":
        return 2
    if result["status"] == "degraded":
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PS5 catalog metrics, diff, and health-check utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python scrape_metrics.py metrics dlpsgame-ps5.json
  python scrape_metrics.py metrics dlpsgame-ps5.json --out metrics.json
  python scrape_metrics.py diff old.json new.json --out diff.json
  python scrape_metrics.py health dlpsgame-ps5.json --min-games 500
""",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # metrics
    sub_metrics = subparsers.add_parser("metrics", help="Generate catalog metrics")
    sub_metrics.add_argument("catalog", type=Path, help="Path to catalog JSON")
    sub_metrics.add_argument("--out", type=Path, default=None, help="Output metrics JSON path")
    sub_metrics.set_defaults(func=_cmd_metrics)

    # diff
    sub_diff = subparsers.add_parser("diff", help="Diff two catalog versions")
    sub_diff.add_argument("old", type=Path, help="Path to older catalog JSON")
    sub_diff.add_argument("new", type=Path, help="Path to newer catalog JSON")
    sub_diff.add_argument("--out", type=Path, default=None, help="Output diff JSON path")
    sub_diff.set_defaults(func=_cmd_diff)

    # health
    sub_health = subparsers.add_parser("health", help="Health check a catalog")
    sub_health.add_argument("catalog", type=Path, help="Path to catalog JSON")
    sub_health.add_argument(
        "--min-games",
        type=int,
        default=500,
        help="Minimum expected game count (default: 500)",
    )
    sub_health.set_defaults(func=_cmd_health)

    args = parser.parse_args(argv)

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    if not args.command:
        parser.print_help()
        return 2

    try:
        return args.func(args)
    except SystemExit as exc:
        # Re-raise SystemExit from _load_catalog
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        LOG.error("Command failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
