#!/usr/bin/env python3
"""
dartvm_create_srclist.py — Génère sourcelist.cmake pour la Dart VM.

Usage :
  python dartvm_create_srclist.py /path/to/dart-sdk
  python dartvm_create_srclist.py /path/to/dart-sdk/runtime
"""

from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path


# ── Parsing GNI ───────────────────────────────────────────────────────────────

def _parse_gni(gni_file: str) -> dict[str, list[str]]:
    """
    Parse un fichier GNI et retourne un dict { nom_liste → [fichiers] }.
    Supporte les commentaires # et les listes multi-lignes.
    """
    if not os.path.isfile(gni_file):
        return {}

    text = Path(gni_file).read_text(encoding="utf-8", errors="replace")

    # Supprime les commentaires de ligne
    text = re.sub(r"#[^\n]*", "", text)

    result: dict[str, list[str]] = {}

    for m in re.finditer(
        r'\b(\w+)\s*=\s*\[\s*([\s\S]*?)\s*\]',
        text,
    ):
        name  = m.group(1)
        body  = m.group(2)
        items = re.findall(r'"([^"]+)"', body)
        if items:
            result[name] = items

    return result


def _get_src_files_from_dir(path: str) -> list[str]:
    """
    Cherche <path>/<basename>_sources.gni et retourne la liste correspondante.
    """
    basename = os.path.basename(path)
    gni_file = os.path.join(path, f"{basename}_sources.gni")
    parsed   = _parse_gni(gni_file)
    key      = f"{basename}_sources"

    if key in parsed:
        return parsed[key]

    # Tentative avec variantes
    for candidate_key in parsed:
        if candidate_key.endswith("_sources"):
            return parsed[candidate_key]

    raise RuntimeError(
        f"Clé '{key}' introuvable dans {gni_file}\n"
        f"  Clés disponibles : {', '.join(parsed.keys()) or '(aucune)'}"
    )


def _get_cc_files_from_gni(gni_file: str) -> list[str]:
    """Retourne la liste *_cc_files d'un fichier GNI. Retourne [] si absent."""
    parsed = _parse_gni(gni_file)
    for key, values in parsed.items():
        if key.endswith("_cc_files"):
            return values
    return []


def _collect_cc_from_dir(path: str) -> list[str]:
    """Collecte tous les .cc d'un dossier (glob direct)."""
    return sorted(glob.glob(os.path.join(path, "*.cc")))


# ── Construction de la liste de sources ──────────────────────────────────────

def build_source_list(runtime_dir: str, sdk_dir: str) -> tuple[list[str], list[str]]:
    """
    Construit les listes de fichiers .cc et headers .h.
    Retourne (cc_sources, header_files).
    """
    cc_srcs: list[str] = []
    hdrs:    list[str] = []
    errors:  list[str] = []

    # ── Sources principales ──────────────────────────────────────────────
    MAIN_PATHS = ("vm", "platform", "vm/heap", "vm/ffi", "vm/regexp")

    for sub in MAIN_PATHS:
        path = os.path.join(runtime_dir, sub)
        if not os.path.isdir(path):
            continue
        try:
            sources = _get_src_files_from_dir(path)
        except RuntimeError as e:
            errors.append(f"  ⚠  {e}")
            continue

        for src in sources:
            full = os.path.join(path, src)
            cc_srcs.append(full)
            if src.endswith(".h"):
                hdrs.append(full)

    # ── Fichiers supplémentaires obligatoires ────────────────────────────
    EXTRA_FILES = (
        "vm/version.cc",
        "vm/dart_api_impl.cc",
        "vm/native_api_impl.cc",
        "vm/compiler/runtime_api.cc",
        "vm/compiler/jit/compiler.cc",
    )
    EXTRA_OPTIONAL = (
        "platform/no_tsan.cc",
        "vm/compiler/aot/precompiler.cc",
    )

    for name in EXTRA_FILES:
        full = os.path.join(runtime_dir, name)
        cc_srcs.append(full)

    for name in EXTRA_OPTIONAL:
        full = os.path.join(runtime_dir, name)
        if os.path.isfile(full):
            cc_srcs.append(full)

    hdrs.append(os.path.join(runtime_dir, "vm", "version.h"))

    # ── Bibliothèques runtime Dart ────────────────────────────────────────
    RUNTIME_LIBS = (
        "async", "concurrent", "core", "developer", "ffi",
        "isolate", "math", "typed_data", "vmservice", "internal",
    )
    lib_dir = os.path.join(runtime_dir, "lib")
    for lib in RUNTIME_LIBS:
        gni = os.path.join(lib_dir, f"{lib}_sources.gni")
        if not os.path.isfile(gni):
            continue
        for src in _get_cc_files_from_gni(gni):
            if src.endswith(".cc"):
                cc_srcs.append(os.path.join(lib_dir, src))

    # ── double-conversion ─────────────────────────────────────────────────
    dc_dirs = [
        os.path.join(runtime_dir, "third_party", "double-conversion", "src"),
        os.path.join(sdk_dir, "third_party", "double-conversion", "src"),
    ]
    dc_found = False
    for dc_dir in dc_dirs:
        if os.path.isdir(dc_dir):
            cc_srcs.extend(_collect_cc_from_dir(dc_dir))
            dc_found = True
            break

    if not dc_found:
        errors.append(
            "  ⚠  double-conversion introuvable.\n"
            f"     Chemins testés : {dc_dirs}"
        )

    if errors:
        print("Avertissements lors de la collecte des sources :", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)

    # Déduplique en préservant l'ordre
    seen: set[str] = set()
    unique_cc: list[str] = []
    for p in cc_srcs:
        if p not in seen:
            seen.add(p)
            unique_cc.append(p)

    return unique_cc, hdrs


def _normalize_paths(paths: list[str]) -> list[str]:
    """Normalise les séparateurs de chemin pour CMake (slashes Unix)."""
    if os.sep == "\\":
        return [p.replace("\\", "/") for p in paths]
    return paths


# ── Écriture sourcelist.cmake ─────────────────────────────────────────────────

def write_sourcelist_cmake(cc_srcs: list[str], output_dir: str):
    """Écrit sourcelist.cmake dans `output_dir`."""
    out_file  = os.path.join(output_dir, "sourcelist.cmake")
    normalized = _normalize_paths(cc_srcs)

    content = "set(SRCS\n"
    for src in normalized:
        content += f"    {src}\n"
    content += ")\n"

    Path(out_file).write_text(content, encoding="utf-8")
    print(f"  sourcelist.cmake écrit ({len(normalized)} sources → {out_file})")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Génère sourcelist.cmake pour la Dart VM"
    )
    parser.add_argument(
        "basedir",
        help="Répertoire racine du SDK Dart cloné (ou son sous-dossier runtime)",
    )
    args = parser.parse_args()

    base = os.path.abspath(args.basedir)

    if os.path.isdir(os.path.join(base, "runtime")):
        sdk_dir     = base
        runtime_dir = os.path.join(base, "runtime")
    elif os.path.isdir(os.path.join(base, "vm")):
        runtime_dir = base
        sdk_dir     = os.path.dirname(base)
    else:
        print(f"[ERREUR] Répertoire runtime introuvable dans : {base}", file=sys.stderr)
        sys.exit(1)

    cc_srcs, _hdrs = build_source_list(runtime_dir, sdk_dir)
    write_sourcelist_cmake(cc_srcs, base)


if __name__ == "__main__":
    main()
