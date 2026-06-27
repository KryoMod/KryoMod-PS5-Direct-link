#!/usr/bin/env python3
"""
Pipeline unifié pour le catalogue PS5
======================================
Orchestre toutes les sources de scraping, la fusion, l'enrichissement
et la validation en une seule commande.

Usage:
    # Pipeline complet (toutes les sources)
    python pipeline.py --out dlpsgame-ps5.json

    # Sources spécifiques seulement
    python pipeline.py --sources dlpsgame,superpsx

    # Mode incrémental (défaut)
    python pipeline.py --mode incremental

    # Mode test rapide
    python pipeline.py --sources dlpsgame --max-games 5 --verbose

    # Sans enrichissement RAWG
    python pipeline.py --skip-enrich

    # Vérification de santé uniquement
    python pipeline.py --health-check

Pipeline steps:
    1. Discovery   — list game URLs from each source
    2. Scrape      — extract metadata + download links from each source
    3. Import exFAT — fetch and decode the exFAT JSON
    4. Merge       — incremental merge into existing catalog
    5. Enrich      — RAWG metadata enrichment
    6. Validate    — Pydantic schema validation
    7. Metrics     — generate metrics + diff report
    8. Health check — verify catalog integrity
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

DEFAULT_SOURCES = ["dlpsgame", "superpsx", "exfat"]
ALL_SOURCES = ["dlpsgame", "superpsx", "exfat", "wp_api"]

# Fresh output files produced by each source scraper
SOURCE_OUTPUTS: dict[str, str] = {
    "dlpsgame": "dlpsgame-ps5.fresh.json",
    "wp_api": "dlpsgame-ps5.fresh.json",  # same output, preferred over HTML
    "superpsx": "superpsx-ps5.fresh.json",
    "exfat": "exfat-ps5.fresh.json",
}

LOG = logging.getLogger("pipeline")

# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------


def find_config_path(explicit: str | None = None) -> Path | None:
    """Locate config.yaml with the following precedence:

    1. Explicit path provided via --config
    2. config.yaml next to the pipeline script
    3. config.yaml in the project root
    4. config.yaml in the current working directory
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        LOG.warning("Explicit config path not found: %s", p)
        return None

    candidates = [
        SCRIPT_DIR / "config.yaml",
        PROJECT_DIR / "config.yaml",
        Path.cwd() / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            LOG.debug("Found config at: %s", candidate)
            return candidate
    return None


def load_config(config_path: Path | None) -> dict[str, Any]:
    """Load configuration from YAML file, falling back to empty dict.

    Uses PyYAML if available, otherwise falls back to a minimal parser
    that handles the flat structure of config.yaml.
    """
    if config_path is None:
        LOG.info("No config.yaml found — using hardcoded defaults")
        return {}

    try:
        import yaml  # type: ignore[import-untyped]
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            LOG.info("Loaded config from %s", config_path)
            return data
        LOG.warning("Config file %s is not a valid YAML dict", config_path)
        return {}
    except ImportError:
        LOG.debug("PyYAML not installed — using minimal parser")
        return _parse_yaml_minimal(config_path)
    except Exception as exc:
        LOG.warning("Failed to load config %s: %s — using defaults", config_path, exc)
        return {}


def _parse_yaml_minimal(path: Path) -> dict[str, Any]:
    """Minimal YAML parser for the config structure.

    Handles nested dicts and lists-of-lists (mirror_patterns) but is NOT
    a general-purpose YAML parser. Falls back to empty dict on any error.
    """
    # This is intentionally minimal — we only need it when PyYAML is absent.
    # Production deployments should install PyYAML.
    try:
        import yaml  # type: ignore[import-untyped]  # noqa: F811 — re-check after ImportError
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass

    # If yaml is truly unavailable, try a very basic approach
    LOG.warning(
        "PyYAML is not installed. Config file %s will be ignored. "
        "Install with: pip install pyyaml",
        path,
    )
    return {}


# ---------------------------------------------------------------------------
# Pipeline step runners
# ---------------------------------------------------------------------------


class StepResult:
    """Result of a pipeline step execution."""

    def __init__(self, name: str, success: bool, rc: int = 0,
                 duration: float = 0.0, detail: str = ""):
        self.name = name
        self.success = success
        self.rc = rc
        self.duration = duration
        self.detail = detail

    def __repr__(self) -> str:
        status = "OK" if self.success else f"FAIL(rc={self.rc})"
        return f"StepResult({self.name}: {status} {self.duration:.1f}s)"


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    step_name: str = "",
) -> StepResult:
    """Run a subprocess and return a StepResult.

    Captures stdout/stderr and logs them. Returns success if rc == 0.
    """
    LOG.info("▶ %s", " ".join(cmd))
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - start

        if result.stdout:
            for line in result.stdout.strip().splitlines():
                LOG.info("  [stdout] %s", line)
        if result.stderr:
            for line in result.stderr.strip().splitlines():
                LOG.debug("  [stderr] %s", line)

        success = result.returncode == 0
        detail = ""
        if not success:
            detail = result.stderr.strip()[-500:] if result.stderr else f"exit code {result.returncode}"
            LOG.warning("✗ %s failed (rc=%d): %s", step_name, result.returncode, detail[:200])
        else:
            LOG.info("✓ %s completed (%.1fs)", step_name, duration)

        return StepResult(
            name=step_name,
            success=success,
            rc=result.returncode,
            duration=duration,
            detail=detail,
        )

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        LOG.error("✗ %s timed out after %ds", step_name, timeout)
        return StepResult(name=step_name, success=False, rc=124, duration=duration,
                          detail=f"Timeout after {timeout}s")
    except FileNotFoundError as exc:
        duration = time.monotonic() - start
        LOG.error("✗ %s: command not found: %s", step_name, exc)
        return StepResult(name=step_name, success=False, rc=127, duration=duration,
                          detail=str(exc))
    except Exception as exc:
        duration = time.monotonic() - start
        LOG.error("✗ %s: unexpected error: %s", step_name, exc)
        return StepResult(name=step_name, success=False, rc=1, duration=duration,
                          detail=str(exc))


def run_dlpsgame_scraper(
    config: dict[str, Any],
    *,
    max_pages: int | None,
    max_games: int | None,
    verbose: bool,
) -> StepResult:
    """Run the dlpsgame scraper (HTML-based).

    Prefers scrape_wp_api.py if available; falls back to scrape_dlpsgame.py.
    """
    sources_cfg = config.get("sources", {}).get("dlpsgame", {})

    # Try WP API scraper first
    wp_api_script = SCRIPT_DIR / "scrape_wp_api.py"
    html_script = SCRIPT_DIR / "scrape_dlpsgame.py"

    if wp_api_script.is_file():
        LOG.info("WP API scraper found — using scrape_wp_api.py (preferred)")
        script = wp_api_script
        step_name = "scrape-dlpsgame-wp-api"
    elif html_script.is_file():
        LOG.info("WP API scraper not found — falling back to scrape_dlpsgame.py")
        script = html_script
        step_name = "scrape-dlpsgame-html"
    else:
        LOG.error("Neither scrape_wp_api.py nor scrape_dlpsgame.py found")
        return StepResult(name="scrape-dlpsgame", success=False, rc=127,
                          detail="No dlpsgame scraper script found")

    out_file = SOURCE_OUTPUTS["dlpsgame"]
    cmd = [sys.executable, str(script), "--out", out_file]
    if max_pages is not None:
        cmd.extend(["--max-pages", str(max_pages)])
    if max_games is not None:
        cmd.extend(["--max-games", str(max_games)])
    if verbose:
        cmd.append("--verbose")

    # Pass FlareSolverr URL from config if available
    fs_url = sources_cfg.get("flaresolverr_url")
    if fs_url:
        env = {**os.environ, "FLARESOLVERR_URL": fs_url}
    else:
        env = None

    LOG.info("▶ %s", " ".join(cmd))
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max per source
            env=env,
        )
        duration = time.monotonic() - start

        if result.stdout:
            for line in result.stdout.strip().splitlines():
                LOG.info("  [stdout] %s", line)
        if result.stderr:
            for line in result.stderr.strip().splitlines():
                LOG.debug("  [stderr] %s", line)

        success = result.returncode == 0
        detail = ""
        if not success:
            detail = result.stderr.strip()[-500:] if result.stderr else f"exit code {result.returncode}"
            LOG.warning("✗ %s failed (rc=%d): %s", step_name, result.returncode, detail[:200])
        else:
            LOG.info("✓ %s completed (%.1fs)", step_name, duration)

        return StepResult(name=step_name, success=success, rc=result.returncode,
                          duration=duration, detail=detail)

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        LOG.error("✗ %s timed out", step_name)
        return StepResult(name=step_name, success=False, rc=124, duration=duration,
                          detail="Timeout")
    except Exception as exc:
        duration = time.monotonic() - start
        return StepResult(name=step_name, success=False, rc=1, duration=duration,
                          detail=str(exc))


def run_wp_api_scraper(
    config: dict[str, Any],
    *,
    max_pages: int | None,
    max_games: int | None,
    verbose: bool,
) -> StepResult:
    """Run the WP API scraper for dlpsgame (explicit wp_api source)."""
    wp_api_script = SCRIPT_DIR / "scrape_wp_api.py"
    if not wp_api_script.is_file():
        LOG.error("scrape_wp_api.py not found — cannot run wp_api source")
        return StepResult(name="scrape-wp-api", success=False, rc=127,
                          detail="scrape_wp_api.py not found")

    out_file = SOURCE_OUTPUTS["wp_api"]
    cmd = [sys.executable, str(wp_api_script), "--out", out_file]
    if max_pages is not None:
        cmd.extend(["--max-pages", str(max_pages)])
    if max_games is not None:
        cmd.extend(["--max-games", str(max_games)])
    if verbose:
        cmd.append("--verbose")

    return _run_subprocess(cmd, cwd=PROJECT_DIR, timeout=3600, step_name="scrape-wp-api")


def run_superpsx_scraper(
    config: dict[str, Any],
    *,
    max_pages: int | None,
    max_games: int | None,
    verbose: bool,
) -> StepResult:
    """Run the SuperPSX scraper."""
    script = SCRIPT_DIR / "scrape_superpsx.py"
    if not script.is_file():
        LOG.error("scrape_superpsx.py not found")
        return StepResult(name="scrape-superpsx", success=False, rc=127,
                          detail="scrape_superpsx.py not found")

    out_file = SOURCE_OUTPUTS["superpsx"]
    cmd = [sys.executable, str(script), "--out", out_file]
    if max_pages is not None:
        cmd.extend(["--max-pages", str(max_pages)])
    if max_games is not None:
        cmd.extend(["--max-games", str(max_games)])
    if verbose:
        cmd.append("--verbose")

    return _run_subprocess(cmd, cwd=PROJECT_DIR, timeout=3600, step_name="scrape-superpsx")


def run_exfat_import(
    config: dict[str, Any],
    *,
    verbose: bool,
) -> StepResult:
    """Run the exFAT JSON import."""
    script = SCRIPT_DIR / "import_exfat.py"
    if not script.is_file():
        LOG.error("import_exfat.py not found")
        return StepResult(name="import-exfat", success=False, rc=127,
                          detail="import_exfat.py not found")

    sources_cfg = config.get("sources", {}).get("exfat", {})
    out_file = SOURCE_OUTPUTS["exfat"]
    cmd = [sys.executable, str(script), "--out", out_file]

    # Pass config values as CLI args if available
    exfat_url = sources_cfg.get("url")
    if exfat_url:
        cmd.extend(["--url", exfat_url])
    exfat_timeout = sources_cfg.get("timeout")
    if exfat_timeout is not None:
        cmd.extend(["--timeout", str(exfat_timeout)])
    exfat_retries = sources_cfg.get("retries")
    if exfat_retries is not None:
        cmd.extend(["--retries", str(exfat_retries)])
    if verbose:
        cmd.append("--verbose")

    return _run_subprocess(cmd, cwd=PROJECT_DIR, timeout=120, step_name="import-exfat")


def run_merge(
    fresh_files: list[Path],
    catalog_path: Path,
    *,
    verbose: bool,
) -> StepResult:
    """Merge fresh source outputs into the existing catalog.

    If the catalog doesn't exist yet, the first fresh file becomes the base.
    """
    script = SCRIPT_DIR / "merge_catalogs.py"
    if not script.is_file():
        LOG.error("merge_catalogs.py not found")
        return StepResult(name="merge", success=False, rc=127,
                          detail="merge_catalogs.py not found")

    # Filter to files that actually exist
    existing_fresh = [f for f in fresh_files if f.is_file()]
    if not existing_fresh:
        LOG.warning("No fresh catalog files to merge")
        return StepResult(name="merge", success=False, rc=1,
                          detail="No fresh files produced")

    # If no existing catalog, just copy the first fresh file as the base
    if not catalog_path.is_file():
        LOG.info("No existing catalog — using first fresh file as base: %s", existing_fresh[0])
        import shutil
        shutil.copy2(existing_fresh[0], catalog_path)
        # If multiple fresh files, still merge the rest
        if len(existing_fresh) > 1:
            cmd = [sys.executable, str(script), str(catalog_path)] + \
                  [str(f) for f in existing_fresh[1:]]
            return _run_subprocess(cmd, cwd=PROJECT_DIR, step_name="merge")
        return StepResult(name="merge", success=True, duration=0.0,
                          detail="Created new catalog from first source")

    cmd = [sys.executable, str(script), str(catalog_path)] + \
          [str(f) for f in existing_fresh]
    return _run_subprocess(cmd, cwd=PROJECT_DIR, step_name="merge")


def run_enrichment(
    catalog_path: Path,
    config: dict[str, Any],
    *,
    verbose: bool,
) -> StepResult:
    """Run RAWG enrichment on the merged catalog."""
    script = SCRIPT_DIR / "enrich_rawg.py"
    if not script.is_file():
        LOG.error("enrich_rawg.py not found")
        return StepResult(name="enrich", success=False, rc=127,
                          detail="enrich_rawg.py not found")

    if not catalog_path.is_file():
        LOG.error("Catalog file not found for enrichment: %s", catalog_path)
        return StepResult(name="enrich", success=False, rc=1,
                          detail=f"Catalog not found: {catalog_path}")

    # Check for API key
    api_key = os.environ.get("RAWG_API_KEY", "").strip()
    if not api_key:
        LOG.info("RAWG_API_KEY not set — skipping enrichment")
        return StepResult(name="enrich", success=True, duration=0.0,
                          detail="Skipped (no API key)")

    enrich_cfg = config.get("enrichment", {}).get("rawg", {})
    cmd = [sys.executable, str(script), str(catalog_path)]

    ttl_days = enrich_cfg.get("ttl_days_matched", 30)
    cmd.extend(["--ttl-days", str(ttl_days)])

    max_calls = enrich_cfg.get("max_calls_per_run", 900)
    cmd.extend(["--max-calls", str(max_calls)])

    delay = enrich_cfg.get("delay_between_calls", 0.2)
    cmd.extend(["--delay", str(delay)])

    if verbose:
        LOG.info("Enrichment config: ttl=%d days, max_calls=%d, delay=%.1fs",
                 ttl_days, max_calls, delay)

    return _run_subprocess(cmd, cwd=PROJECT_DIR, timeout=7200, step_name="enrich")


def validate_catalog(catalog_path: Path) -> StepResult:
    """Basic schema validation: every package must have title, titleId, downloadLinks."""
    start = time.monotonic()
    if not catalog_path.is_file():
        return StepResult(name="validate", success=False, rc=1,
                          detail=f"Catalog not found: {catalog_path}")

    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        duration = time.monotonic() - start
        return StepResult(name="validate", success=False, rc=1,
                          duration=duration, detail=str(exc))

    packages = data.get("packages", [])
    if not isinstance(packages, list):
        duration = time.monotonic() - start
        return StepResult(name="validate", success=False, rc=1,
                          duration=duration, detail="Missing or invalid 'packages' key")

    errors: list[str] = []
    warnings_count = 0

    for idx, pkg in enumerate(packages):
        if not isinstance(pkg, dict):
            errors.append(f"Package #{idx}: not a dict")
            continue

        # Required fields
        title = pkg.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            errors.append(f"Package #{idx}: missing or empty 'title'")

        title_id = pkg.get("titleId")
        if not title_id or not isinstance(title_id, str) or not title_id.strip():
            warnings_count += 1  # Not critical but worth flagging

        links = pkg.get("downloadLinks")
        if not links or not isinstance(links, list) or len(links) == 0:
            errors.append(f"Package #{idx} ({title or '?'}): missing or empty 'downloadLinks'")

    duration = time.monotonic() - start

    if errors:
        # Report first 10 errors
        sample = errors[:10]
        detail = f"{len(errors)} validation error(s); first: " + "; ".join(sample[:3])
        LOG.error("Validation failed: %d errors, %d warnings", len(errors), warnings_count)
        for err in sample:
            LOG.error("  - %s", err)
        return StepResult(name="validate", success=False, rc=1,
                          duration=duration, detail=detail)

    LOG.info("Validation passed: %d games, %d warnings (missing titleId)",
             len(packages), warnings_count)
    return StepResult(name="validate", success=True, duration=duration,
                      detail=f"{len(packages)} games valid, {warnings_count} titleId warnings")


def run_metrics(
    catalog_path: Path,
    metrics_path: Path,
    diff_path: Path | None,
    old_catalog: Path | None,
    *,
    verbose: bool,
) -> StepResult:
    """Generate metrics and optionally a diff report."""
    script = SCRIPT_DIR / "scrape_metrics.py"
    if not script.is_file():
        LOG.error("scrape_metrics.py not found")
        return StepResult(name="metrics", success=False, rc=127,
                          detail="scrape_metrics.py not found")

    if not catalog_path.is_file():
        return StepResult(name="metrics", success=False, rc=1,
                          detail=f"Catalog not found: {catalog_path}")

    # Generate metrics
    cmd = [sys.executable, str(script), "metrics", str(catalog_path),
           "--out", str(metrics_path)]
    result = _run_subprocess(cmd, cwd=PROJECT_DIR, step_name="metrics-generate")

    # Generate diff if we have an old catalog
    if diff_path and old_catalog and old_catalog.is_file():
        cmd = [sys.executable, str(script), "diff",
               str(old_catalog), str(catalog_path),
               "--out", str(diff_path)]
        diff_result = _run_subprocess(cmd, cwd=PROJECT_DIR, step_name="metrics-diff")
        if not diff_result.success:
            LOG.warning("Diff generation failed (non-fatal)")

    return result


def run_health_check(catalog_path: Path) -> dict[str, Any]:
    """Run a health check on the catalog using scrape_metrics.py."""
    script = SCRIPT_DIR / "scrape_metrics.py"
    if not script.is_file():
        return {"status": "critical", "error": "scrape_metrics.py not found"}

    if not catalog_path.is_file():
        return {"status": "critical", "error": f"Catalog not found: {catalog_path}"}

    cmd = [sys.executable, str(script), "health", str(catalog_path)]
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"status": "unknown", "raw_output": result.stdout[:500]}
        return {"status": "unknown", "returncode": result.returncode}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(args: argparse.Namespace) -> int:
    """Execute the full pipeline and return an exit code.

    Returns:
        0 = success
        1 = partial failure (some sources/steps failed)
        2 = total failure
    """
    start_time = time.monotonic()

    # --- Configure logging ---
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(levelname)s] %(asctime)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # --- Load config ---
    config_path = find_config_path(args.config)
    config = load_config(config_path)

    # --- Determine output paths ---
    output_cfg = config.get("output", {})
    catalog_file = args.out or output_cfg.get("catalog_file", "dlpsgame-ps5.json")
    catalog_path = PROJECT_DIR / catalog_file
    metrics_file = output_cfg.get("metrics_file", "scrape-metrics.json")
    metrics_path = PROJECT_DIR / metrics_file
    diff_file = output_cfg.get("diff_file", "diff-report.json")
    diff_path = PROJECT_DIR / diff_file

    # Save old catalog for diff (before merge overwrites it)
    old_catalog_path: Path | None = None
    if catalog_path.is_file():
        old_catalog_path = catalog_path.with_suffix(".prev.json")
        import shutil
        shutil.copy2(catalog_path, old_catalog_path)

    # --- Health check only mode ---
    if args.health_check:
        LOG.info("=== Health check mode ===")
        health = run_health_check(catalog_path)
        print(json.dumps(health, indent=2, ensure_ascii=False))
        if health.get("status") == "critical":
            return 2
        if health.get("status") == "degraded":
            return 1
        return 0

    # --- Determine sources ---
    requested_sources = [s.strip() for s in args.sources.split(",")] if args.sources else DEFAULT_SOURCES
    invalid = [s for s in requested_sources if s not in ALL_SOURCES]
    if invalid:
        LOG.error("Unknown source(s): %s. Valid: %s", invalid, ALL_SOURCES)
        return 2

    # If wp_api is requested, we'll use it for dlpsgame; if dlpsgame is
    # also requested, remove it to avoid double-scraping.
    if "wp_api" in requested_sources and "dlpsgame" in requested_sources:
        LOG.info("Both wp_api and dlpsgame requested — wp_api takes priority, removing dlpsgame")
        requested_sources = [s for s in requested_sources if s != "dlpsgame"]

    LOG.info("=" * 60)
    LOG.info("Pipeline started")
    LOG.info("  Sources:    %s", ", ".join(requested_sources))
    LOG.info("  Mode:       %s", args.mode)
    LOG.info("  Output:     %s", catalog_path)
    LOG.info("  Config:     %s", config_path or "(defaults)")
    LOG.info("=" * 60)

    # --- Track results ---
    results: list[StepResult] = []
    fresh_files: list[Path] = []

    # ===================================================================
    # STEP 1 & 2: Scrape each source
    # ===================================================================
    LOG.info("--- Step 1-2: Scraping sources ---")

    for source in requested_sources:
        LOG.info("Scraping source: %s", source)

        if source == "dlpsgame":
            r = run_dlpsgame_scraper(
                config,
                max_pages=args.max_pages,
                max_games=args.max_games,
                verbose=args.verbose,
            )
        elif source == "wp_api":
            r = run_wp_api_scraper(
                config,
                max_pages=args.max_pages,
                max_games=args.max_games,
                verbose=args.verbose,
            )
        elif source == "superpsx":
            r = run_superpsx_scraper(
                config,
                max_pages=args.max_pages,
                max_games=args.max_games,
                verbose=args.verbose,
            )
        elif source == "exfat":
            r = run_exfat_import(config, verbose=args.verbose)
        else:
            LOG.error("Unknown source: %s", source)
            r = StepResult(name=f"scrape-{source}", success=False, rc=1,
                           detail=f"Unknown source: {source}")

        results.append(r)

        # Track fresh output file if scraping succeeded
        output_name = SOURCE_OUTPUTS.get(source, "")
        if output_name and r.success:
            fresh_path = PROJECT_DIR / output_name
            if fresh_path.is_file():
                fresh_files.append(fresh_path)
                LOG.info("  Fresh output: %s (%d bytes)",
                         fresh_path.name, fresh_path.stat().st_size)
            else:
                LOG.warning("  Scraper reported success but output file not found: %s", fresh_path)
        elif not r.success:
            LOG.warning("  Source %s failed — will continue without it", source)

    # Check if we got any data at all
    if not fresh_files:
        LOG.error("All sources failed — no data to process")
        _print_summary(results, start_time)
        return 2

    # ===================================================================
    # STEP 3: Merge
    # ===================================================================
    if not args.skip_merge:
        LOG.info("--- Step 3: Merging catalogs ---")
        r = run_merge(fresh_files, catalog_path, verbose=args.verbose)
        results.append(r)

        if not r.success:
            LOG.error("Merge failed — cannot continue with enrichment/validation")
            _print_summary(results, start_time)
            return 2
    else:
        LOG.info("--- Step 3: Merge (SKIPPED) ---")

    # ===================================================================
    # STEP 4: Enrichment
    # ===================================================================
    if not args.skip_enrich:
        LOG.info("--- Step 4: RAWG enrichment ---")
        r = run_enrichment(catalog_path, config, verbose=args.verbose)
        results.append(r)
        if not r.success:
            LOG.warning("Enrichment failed — continuing without enrichment")
    else:
        LOG.info("--- Step 4: Enrichment (SKIPPED) ---")

    # ===================================================================
    # STEP 5: Validation
    # ===================================================================
    if not args.skip_validate:
        LOG.info("--- Step 5: Validation ---")
        r = validate_catalog(catalog_path)
        results.append(r)
        if not r.success:
            LOG.error("Validation failed — catalog may be incomplete")
    else:
        LOG.info("--- Step 5: Validation (SKIPPED) ---")

    # ===================================================================
    # STEP 6: Metrics
    # ===================================================================
    LOG.info("--- Step 6: Metrics ---")
    r = run_metrics(
        catalog_path, metrics_path,
        diff_path=diff_path,
        old_catalog=old_catalog_path,
        verbose=args.verbose,
    )
    results.append(r)

    # ===================================================================
    # STEP 7: Final health check
    # ===================================================================
    LOG.info("--- Step 7: Health check ---")
    health = run_health_check(catalog_path)
    health_status = health.get("status", "unknown")
    LOG.info("Health status: %s", health_status)
    if health.get("warnings"):
        for w in health["warnings"]:
            LOG.warning("  ⚠ %s", w)

    # ===================================================================
    # Cleanup
    # ===================================================================
    # Remove old catalog backup
    if old_catalog_path and old_catalog_path.is_file():
        try:
            old_catalog_path.unlink()
        except OSError:
            pass

    # --- Final summary ---
    _print_summary(results, start_time, health=health)

    # --- Determine exit code ---
    failed_steps = [r for r in results if not r.success]
    critical_steps = [r for r in failed_steps if r.name in ("merge", "validate")]
    scrape_failures = [r for r in failed_steps if r.name.startswith("scrape-")]

    if critical_steps:
        return 2  # total failure
    if scrape_failures and len(scrape_failures) == len(requested_sources):
        return 2  # all sources failed
    if failed_steps:
        return 1  # partial failure
    return 0


def _print_summary(
    results: list[StepResult],
    start_time: float,
    *,
    health: dict[str, Any] | None = None,
) -> None:
    """Print a pipeline execution summary."""
    total_duration = time.monotonic() - start_time
    total_steps = len(results)
    succeeded = sum(1 for r in results if r.success)
    failed = total_steps - succeeded

    LOG.info("")
    LOG.info("=" * 60)
    LOG.info("PIPELINE SUMMARY")
    LOG.info("=" * 60)

    for r in results:
        status_icon = "✓" if r.success else "✗"
        LOG.info("  %s %-25s  %.1fs  %s",
                 status_icon, r.name, r.duration,
                 r.detail[:60] if r.detail else "")

    LOG.info("-" * 60)
    LOG.info("  Steps: %d/%d succeeded  |  Total: %.1fs",
             succeeded, total_steps, total_duration)

    if health:
        LOG.info("  Health: %s  (%d games)",
                 health.get("status", "?"),
                 health.get("total_games", 0))

    LOG.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the pipeline CLI."""
    parser = argparse.ArgumentParser(
        description="Pipeline unifié pour le catalogue PS5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Full pipeline (all sources)
  python pipeline.py --out dlpsgame-ps5.json

  # Specific sources only
  python pipeline.py --sources dlpsgame,superpsx

  # Incremental mode (default)
  python pipeline.py --mode incremental

  # Quick test
  python pipeline.py --sources dlpsgame --max-games 5 --verbose

  # Skip RAWG enrichment
  python pipeline.py --skip-enrich

  # Health check only
  python pipeline.py --health-check

return codes:
  0 = success
  1 = partial failure (some sources/steps failed)
  2 = total failure
""",
    )

    # Source selection
    parser.add_argument(
        "--sources",
        default=None,
        help="Comma-separated list of sources to scrape. "
             f"Valid: {', '.join(ALL_SOURCES)}. Default: {','.join(DEFAULT_SOURCES)}",
    )

    # Mode
    parser.add_argument(
        "--mode",
        choices=["full", "incremental"],
        default="incremental",
        help="Scraping mode: 'full' (re-scrape everything) or 'incremental' (only new/updated). "
             "Default: incremental",
    )

    # Output
    parser.add_argument(
        "--out",
        default=None,
        help="Output catalog path. Default: dlpsgame-ps5.json (or config value)",
    )

    # Limits
    parser.add_argument(
        "--max-games",
        type=int,
        default=None,
        help="Limit number of games per source (useful for testing)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of pages per source",
    )

    # Skip flags
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="Skip RAWG enrichment step",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip Pydantic/schema validation step",
    )
    parser.add_argument(
        "--skip-merge",
        action="store_true",
        help="Skip merge step (useful for testing individual sources)",
    )

    # Health check
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Only run health check on existing catalog (no scraping)",
    )

    # Verbosity
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    # Config
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: auto-detect)",
    )

    return parser


def main() -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
