#!/usr/bin/env python3


from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────
#  Parsing des fichiers .gni
# ─────────────────────────────────────────────────────────────────────────

def _parse_gni(gni_file: str) -> dict[str, list[str]]:
    """
    Parse un fichier GNI et retourne un dict { liste_name → [fichiers] }.
    Ex : { "vm_sources" : ["file1.cc", "file2.cc"] }
    """
    if not os.path.isfile(gni_file):
        return {}

    text = Path(gni_file).read_text(encoding="utf-8", errors="replace")
    result: dict[str, list[str]] = {}

    # Cherche les listes GNI : name = [ ... ]
    for m in re.finditer(
        r'\b(\w+?)\s*=\s*\[\s*([\"\w\-\.\/ ,\n]+?),?\s*\]',
        text,
        re.DOTALL,
    ):
        name  = m.group(1)
        items = re.findall(r'"([\w\-\.]+)"', m.group(2))
        if items:
            result[name] = items

    return result


def _get_src_files_from_dir(path: str) -> list[str]:
    """
    Cherche <path>/<basename>_sources.gni et retourne la liste
    <basename>_sources.  Lève RuntimeError si le fichier est absent.
    """
    basename = os.path.basename(path)
    gni_file = os.path.join(path, f"{basename}_sources.gni")

    parsed = _parse_gni(gni_file)
    key    = f"{basename}_sources"

    if key not in parsed:
        raise RuntimeError(
            f"Clé '{key}' introuvable dans {gni_file}\n"
            f"  Clés disponibles : {', '.join(parsed.keys()) or '(aucune)'}"
        )
    return parsed[key]


def _get_cc_files_from_gni(gni_file: str) -> list[str]:
    """
    Retourne la liste se terminant par '_cc_files' dans un fichier GNI.
    Retourne [] si introuvable.
    """
    parsed = _parse_gni(gni_file)
    for key, values in parsed.items():
        if key.endswith("_cc_files"):
            return values
    return []


def _collect_cc_from_dir(path: str) -> list[str]:
    """Collecte tous les .cc d'un dossier (glob direct)."""
    return sorted(glob.glob(os.path.join(path, "*.cc")))


# ─────────────────────────────────────────────────────────────────────────
#  Construction de la liste de sources
# ─────────────────────────────────────────────────────────────────────────

def build_source_list(runtime_dir: str, sdk_dir: str) -> tuple[list[str], list[str]]:
    """
    Construit les listes de fichiers .cc et headers .h à partir du
    répertoire runtime du SDK Dart.

    Retourne (cc_sources, header_files).
    """
    cc_srcs: list[str] = []
    hdrs:    list[str] = []
    errors:  list[str] = []

    # ── Sources principales ────────────────────────────────────────────
    MAIN_PATHS = ("vm", "platform", "vm/heap", "vm/ffi", "vm/regexp")

    for sub in MAIN_PATHS:
        path = os.path.join(runtime_dir, sub)
        if not os.path.isdir(path):
            # Certains sous-dossiers n'existent pas dans toutes les versions
            continue
        try:
            sources = _get_src_files_from_dir(path)
        except RuntimeError as e:
            errors.append(f"  ⚠  {e}")
            continue

        for src in sources:
            cc_srcs.append(os.path.join(path, src))
            if src.endswith(".h"):
                hdrs.append(os.path.join(path, src))

    # ── Sources supplémentaires obligatoires ──────────────────────────
    EXTRA_FILES = (
        "vm/version.cc",
        "vm/dart_api_impl.cc",
        "vm/native_api_impl.cc",
        "vm/compiler/runtime_api.cc",
        "vm/compiler/jit/compiler.cc",
    )
    EXTRA_OPTIONAL = ("platform/no_tsan.cc",)

    for name in EXTRA_FILES:
        full = os.path.join(runtime_dir, name)
        cc_srcs.append(full)

    for name in EXTRA_OPTIONAL:
        full = os.path.join(runtime_dir, name)
        if os.path.isfile(full):
            cc_srcs.append(full)

    # Header public de version
    hdrs.append(os.path.join(runtime_dir, "vm", "version.h"))

    # ── Bibliothèques runtime Dart ─────────────────────────────────────
    RUNTIME_LIBS = (
        "async", "concurrent", "core", "developer", "ffi",
        "isolate", "math", "typed_data", "vmservice", "internal",
    )

    lib_dir = os.path.join(runtime_dir, "lib")
    for lib in RUNTIME_LIBS:
        gni = os.path.join(lib_dir, f"{lib}_sources.gni")
        if not os.path.isfile(gni):
            continue
        sources = _get_cc_files_from_gni(gni)
        for src in sources:
            if src.endswith(".cc"):
                cc_srcs.append(os.path.join(lib_dir, src))

    # ── double-conversion ─────────────────────────────────────────────
    # Depuis Dart 3.3, double-conversion est à la racine du SDK
    dc_dirs = [
        os.path.join(runtime_dir, "third_party", "double-conversion", "src"),
        os.path.join(sdk_dir,     "third_party", "double-conversion", "src"),
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

    # ── Rapport des erreurs non fatales ───────────────────────────────
    if errors:
        print("Avertissements lors de la collecte des sources :", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)

    return cc_srcs, hdrs


def _normalize_paths(paths: list[str]) -> list[str]:
    """Remplace les séparateurs Windows par des slashes (requis par CMake)."""
    if os.sep == "\\":
        return [p.replace("\\", "/") for p in paths]
    return paths


# ─────────────────────────────────────────────────────────────────────────
#  Écriture de sourcelist.cmake
# ─────────────────────────────────────────────────────────────────────────

def write_sourcelist_cmake(cc_srcs: list[str], output_dir: str):
    """Écrit sourcelist.cmake dans `output_dir`."""
    out_file = os.path.join(output_dir, "sourcelist.cmake")

    normalized = _normalize_paths(cc_srcs)

    content = "set(SRCS\n    "
    content += "\n    ".join(normalized)
    content += "\n)\n"

    Path(out_file).write_text(content, encoding="utf-8")
    print(f"  sourcelist.cmake écrit ({len(normalized)} sources)")


# ─────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Génère sourcelist.cmake pour la Dart VM"
    )
    parser.add_argument(
        "basedir",
        help="Répertoire runtime du SDK Dart cloné (ou son parent SDK)",
    )
    args = parser.parse_args()

    base = os.path.abspath(args.basedir)
    os.chdir(base)

    # Détecte si on passe le dossier SDK ou le dossier runtime
    if os.path.isdir(os.path.join(base, "runtime")):
        sdk_dir     = base
        runtime_dir = os.path.join(base, "runtime")
    else:
        runtime_dir = base
        sdk_dir     = os.path.dirname(base)

    if not os.path.isdir(runtime_dir):
        print(f"[ERREUR] Répertoire runtime introuvable : {runtime_dir}", file=sys.stderr)
        sys.exit(1)

    cc_srcs, _hdrs = build_source_list(runtime_dir, sdk_dir)
    write_sourcelist_cmake(cc_srcs, base)


if __name__ == "__main__":
    main()
