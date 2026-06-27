#!/usr/bin/env python3
"""
Fusionne plusieurs catalogues au format Pegasus DL en un seul.

Usage :
  python merge_catalogs.py sortie.json catalogue_a.json catalogue_b.json [...]

Règles de fusion :
  - Clé d'unicité : titleId réel (ex. PPSA01668) si présent, sinon le titre
    normalisé. Les titleId-placeholder "GAME_xxxxx" ne servent PAS de clé
    (ils ne sont pas stables d'un run à l'autre).
  - Doublons : on fusionne les downloadLinks (dédoublonnés par URL) et on
    garde, champ par champ, la métadonnée la plus riche (description la plus
    longue, posterUrl/sizeBytes/version non vides en priorité).
  - L'ordre des fichiers donne la priorité de base : le premier catalogue
    fournit la version "canonique", les suivants complètent.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Un vrai identifiant ressemble à PPSA01668 / CUSA12345 (4 lettres + chiffres).
REAL_TITLEID_RE = re.compile(r"^[A-Z]{4}\d{3,}$")


def merge_key(pkg: dict) -> str:
    """Clé d'unicité d'un package."""
    tid = (pkg.get("titleId") or "").strip().upper()
    if REAL_TITLEID_RE.match(tid):
        return f"id:{tid}"
    # sinon : titre normalisé (minuscules, sans ponctuation/espaces multiples)
    title = (pkg.get("title") or "").strip().lower()
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return f"title:{title}"


def merge_links(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Union des downloadLinks, dédoublonnée par URL (l'ordre est préservé)."""
    out = list(existing)
    seen = {(l.get("url") or "").strip() for l in out}
    for link in incoming:
        u = (link.get("url") or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(link)
    return out


def richer(a, b):
    """Retourne la valeur 'la plus riche' entre a (existant) et b (entrant)."""
    # chaînes : on garde la plus longue non vide
    if isinstance(a, str) or isinstance(b, str):
        a = a or ""
        b = b or ""
        return a if len(a) >= len(b) else b
    # nombres : on garde le plus grand non nul (taille, etc.)
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return a if (a or 0) >= (b or 0) else b
    return a if a else b


def union_list(a, b) -> list:
    """Union de deux valeurs liste-ou-scalaire, dédoublonnée, ordre préservé."""
    out: list = []
    for val in (a, b):
        if val is None:
            continue
        items = val if isinstance(val, list) else [val]
        for it in items:
            if it not in out:
                out.append(it)
    return out


# Champs dont on veut conserver la version la plus complète.
# (posterUrl est traité à part : on préfère une couverture enrichie RAWG.)
ENRICHABLE_FIELDS = ("version", "description", "sizeBytes", "downloadSource", "category")
# Champs liste qu'on unionne (provenance, formats) pour ne rien perdre.
UNION_FIELDS = ("source", "fileFormat")
# Champs d'enrichissement à préserver tels quels (cache RAWG + métadonnées).
# Indispensable : sans ça, la fusion effacerait _enrichedAt et RAWG serait
# ré-interrogé à chaque run, dépassant le quota.
PRESERVE_FIELDS = ("_enrichedAt", "_rawgMatched", "metadata")


def merge_package(base: dict, extra: dict) -> dict:
    """Fusionne deux packages représentant le même jeu."""
    merged = dict(base)
    merged["downloadLinks"] = merge_links(
        base.get("downloadLinks", []), extra.get("downloadLinks", [])
    )
    for field in ENRICHABLE_FIELDS:
        if field in base or field in extra:
            merged[field] = richer(base.get(field), extra.get(field))
    for field in UNION_FIELDS:
        if field in base or field in extra:
            merged[field] = union_list(base.get(field), extra.get(field))

    # posterUrl : on privilégie la couverture enrichie (RAWG) sur celle, brute,
    # fraîchement scrapée du site. Le côté "enrichi" est celui qui a _enrichedAt.
    base_enr = bool(base.get("_enrichedAt"))
    extra_enr = bool(extra.get("_enrichedAt"))
    if base_enr and not extra_enr:
        merged["posterUrl"] = base.get("posterUrl") or extra.get("posterUrl")
    elif extra_enr and not base_enr:
        merged["posterUrl"] = extra.get("posterUrl") or base.get("posterUrl")
    elif "posterUrl" in base or "posterUrl" in extra:
        merged["posterUrl"] = richer(base.get("posterUrl"), extra.get("posterUrl"))

    # Préservation des champs d'enrichissement (présents côté existant, absents
    # côté frais) : on garde celui qui existe, en privilégiant la base.
    for field in PRESERVE_FIELDS:
        val = base.get(field)
        if val is None:
            val = extra.get(field)
        if val is not None:
            merged[field] = val

    # titleId : on préfère un identifiant réel à un placeholder GAME_xxxxx
    for cand in (base.get("titleId"), extra.get("titleId")):
        if cand and REAL_TITLEID_RE.match(cand.strip().upper()):
            merged["titleId"] = cand
            break

    # sizeBytes : un champ à null/0 fait skipper l'entrée par Pegasus DL.
    # On omet le champ quand la taille est inconnue (champ absent = ignoré).
    if not merged.get("sizeBytes"):
        merged.pop("sizeBytes", None)
    return merged


def load_catalog(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "packages" not in data or not isinstance(data["packages"], list):
        raise ValueError(f"{path} : pas un catalogue Pegasus valide (clé 'packages' absente)")
    return data


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    out_path = Path(argv[1])
    in_paths = [Path(p) for p in argv[2:]]

    by_key: dict[str, dict] = {}
    order: list[str] = []
    stats = []
    catalog_name = "Catalogue fusionné"

    for idx, path in enumerate(in_paths):
        cat = load_catalog(path)
        if idx == 0 and cat.get("name"):
            catalog_name = cat["name"]
        added = updated = 0
        for pkg in cat["packages"]:
            key = merge_key(pkg)
            if key in by_key:
                by_key[key] = merge_package(by_key[key], pkg)
                updated += 1
            else:
                by_key[key] = pkg
                order.append(key)
                added += 1
        stats.append((path.name, len(cat["packages"]), added, updated))

    packages = [by_key[k] for k in order]
    # Filet de sécurité : aucun package ne doit sortir avec sizeBytes null/0,
    # sinon Pegasus DL skippe l'entrée. Un champ absent, lui, est ignoré.
    for pkg in packages:
        if not pkg.get("sizeBytes"):
            pkg.pop("sizeBytes", None)
    packages.sort(key=lambda p: (p.get("title") or "").lower())

    result = {
        "name": catalog_name,
        "version": 1,
        "packages": packages,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Fusion terminée :")
    for name, total, added, updated in stats:
        print(f"  - {name:32s} {total:5d} jeux  (+{added} nouveaux, {updated} fusionnés)")
    print(f"  => {out_path} : {len(packages)} jeux uniques")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
