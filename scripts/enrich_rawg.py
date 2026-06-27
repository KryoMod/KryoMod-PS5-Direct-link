#!/usr/bin/env python3
"""
Enrichit un catalogue Pegasus avec les métadonnées RAWG (couverture, note,
Metacritic, genres, date de sortie).

Cache intégré : chaque package porte un champ `_enrichedAt` (timestamp ISO).
Un jeu n'est ré-interrogé que si cet enrichissement a plus de `--ttl-days`
jours (3 par défaut). Les jeux récemment enrichis sont sautés sans appel API,
ce qui garde la consommation bien sous le quota gratuit RAWG (20 000/mois).

Usage :
  RAWG_API_KEY=xxxx python enrich_rawg.py catalogue.json
  RAWG_API_KEY=xxxx python enrich_rawg.py in.json --out out.json --ttl-days 3 --max-calls 900

Si RAWG_API_KEY est absent, le script ne fait rien (sortie propre) : le
pipeline reste fonctionnel même sans clé configurée.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

RAWG_SEARCH = "https://api.rawg.io/api/games"
USER_AGENT = "dlpsgame-pegasus-enricher/1.0"


# ---------------------------------------------------------------------------
# Appel API isolé (facile à mocker pour les tests)
# ---------------------------------------------------------------------------
def fetch_rawg(title: str, api_key: str, *, timeout: int = 20) -> dict | None:
    """Interroge RAWG par titre et retourne le premier résultat, ou None."""
    params = urllib.parse.urlencode({
        "key": api_key,
        "search": title,
        "search_precise": "true",
        "page_size": 1,
    })
    req = urllib.request.Request(f"{RAWG_SEARCH}?{params}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results") or []
    return results[0] if results else None


# ---------------------------------------------------------------------------
# Logique d'enrichissement
# ---------------------------------------------------------------------------

# TTL dynamiques (configurables via CLI)
_TTL_MATCHED = 30     # Jeux matchés RAWG : rafraîchir tous les 30 jours
_TTL_UNMATCHED = 14  # Jeux non trouvés : réessayer tous les 14 jours


def get_ttl_for_package(pkg: dict, default_ttl: int = 3) -> int:
    """TTL dynamique basé sur le statut d'enrichissement du package.

    - Jamais enrichi : 0 (à enrichir immédiatement)
    - Matché RAWG avec succès : ttl_days_matched (défaut 30)
    - Non trouvé dans RAWG : ttl_days_unmatched (défaut 14)
    """
    ts = pkg.get("_enrichedAt")
    if not ts:
        return 0  # Jamais enrichi → prioritaire
    if pkg.get("_rawgMatched") is True:
        return _TTL_MATCHED  # Matché → long TTL
    if pkg.get("_rawgMatched") is False:
        return _TTL_UNMATCHED  # Non matché → TTL moyen
    return default_ttl  # Inconnu → TTL par défaut


def is_fresh(pkg: dict, ttl_days: int) -> bool:
    """Le package a-t-il été enrichi il y a moins de ttl_days jours ?

    Si ttl_days <= 0, le package n'est jamais considéré comme frais.
    Utilise get_ttl_for_package() pour un TTL dynamique."""
    ts = pkg.get("_enrichedAt")
    if not ts:
        return False
    try:
        when = dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - when
    return age < dt.timedelta(days=ttl_days)


def apply_rawg(pkg: dict, result: dict | None) -> None:
    """Applique les champs RAWG au package (sur place). Marque l'enrichissement."""
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    pkg["_enrichedAt"] = now_iso
    if not result:
        pkg["_rawgMatched"] = False
        return
    pkg["_rawgMatched"] = True
    # Couverture : on remplace par la jaquette RAWG si disponible (plus propre
    # que celle du site), en gardant l'ancienne en repli.
    cover = result.get("background_image")
    if cover:
        pkg["posterUrl"] = cover
    pkg["metadata"] = {
        "rawgSlug": result.get("slug"),
        "rawgName": result.get("name"),
        "rating": result.get("rating"),
        "metacritic": result.get("metacritic"),
        "released": result.get("released"),
        "genres": [g.get("name") for g in (result.get("genres") or []) if g.get("name")],
    }


def _prioritize_packages(packages: list[dict]) -> list[dict]:
    """Trie les packages pour prioriser les jeux jamais enrichis."""
    def priority(pkg):
        if not pkg.get("_enrichedAt"):
            return 0  # Jamais enrichi → priorité max
        if pkg.get("_rawgMatched") is False:
            return 2  # Non matché → basse priorité
        return 1  # Matché → priorité moyenne
    return sorted(packages, key=priority)


def enrich_catalog(catalog: dict, api_key: str, *, ttl_days: int,
                   max_calls: int, delay: float) -> dict:
    packages = catalog.get("packages", [])
    # Prioriser : jeux jamais enrichis en premier
    packages = _prioritize_packages(packages)
    stats = {"total": len(packages), "fresh": 0, "enriched": 0,
             "matched": 0, "unmatched": 0, "errors": 0, "calls": 0, "capped": 0}

    for pkg in packages:
        title = (pkg.get("title") or "").strip()
        if not title:
            continue
        # TTL dynamique : chaque jeu a son propre TTL
        effective_ttl = get_ttl_for_package(pkg, default_ttl=ttl_days)
        if is_fresh(pkg, effective_ttl):
            stats["fresh"] += 1
            continue
        if max_calls and stats["calls"] >= max_calls:
            stats["capped"] += 1
            continue  # plafond atteint : on laissera ce jeu au prochain run

        try:
            result = fetch_rawg(title, api_key)
            stats["calls"] += 1
            apply_rawg(pkg, result)
            stats["enriched"] += 1
            if pkg.get("_rawgMatched"):
                stats["matched"] += 1
            else:
                stats["unmatched"] += 1
        except Exception as exc:
            stats["errors"] += 1
            print(f"  [warn] {title}: {exc}", file=sys.stderr)
            # on n'écrit pas _enrichedAt => sera retenté au prochain run
        if delay:
            time.sleep(delay)

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("catalog", type=Path, help="Catalogue Pegasus à enrichir")
    ap.add_argument("--out", type=Path, default=None, help="Fichier de sortie (défaut: sur place)")
    ap.add_argument("--ttl-days", type=int, default=3, help="Âge max avant ré-enrichissement (défaut 3)")
    ap.add_argument("--max-calls", type=int, default=900,
                    help="Plafond d'appels API par run (sécurité quota, défaut 900 ; 0 = illimité)")
    ap.add_argument("--delay", type=float, default=0.2, help="Délai entre appels en secondes (défaut 0.2)")
    args = ap.parse_args(argv)

    api_key = os.environ.get("RAWG_API_KEY", "").strip()
    if not api_key:
        print("RAWG_API_KEY absente — enrichissement ignoré (le catalogue reste inchangé).")
        return 0

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if "packages" not in catalog:
        print("Fichier invalide : clé 'packages' absente.", file=sys.stderr)
        return 1

    stats = enrich_catalog(catalog, api_key, ttl_days=args.ttl_days,
                           max_calls=args.max_calls, delay=args.delay)

    out = args.out or args.catalog
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Enrichissement terminé : {stats['total']} jeux | "
        f"{stats['fresh']} déjà à jour (cache) | {stats['enriched']} enrichis "
        f"({stats['matched']} trouvés, {stats['unmatched']} non trouvés) | "
        f"{stats['errors']} erreurs | {stats['calls']} appels API"
        + (f" | {stats['capped']} reportés (plafond)" if stats['capped'] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
