#!/usr/bin/env python3
"""
scripts/generate_thread_offsets_cpp.py
───────────────────────────────────────
Parse runtime/vm/thread.h et génère les lignes C++
  threadOffsetNames[dart::Thread::X_offset()] = "X";

Améliorations vs l'original :
  - Vérification que le fichier existe avant de l'ouvrir
  - Support de plusieurs patterns (pas seulement OFFSET_OF)
  - Dédoublonnage des noms extraits
  - Mode verbose
  - CLI avec argparse
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


VERBOSE = os.environ.get("BLUTTER_VERBOSE", "0") == "1"

# Patterns reconnus dans thread.h
PATTERNS = [
    # OFFSET_OF(Thread, field_)
    re.compile(r'\bOFFSET_OF\s*\(\s*Thread\s*,\s*(\w+?)_\s*\)'),
]


def _strip_prefix(name: str) -> tuple[str, str]:
    """
    Retourne (method_name, display_name).
    Ex : 'ffi_callback' → method='callback', display='ffi_callback'
         'thread_id'    → method='id',        display='id'
         'stack_limit'  → method='stack_limit', display='stack_limit'
    """
    if name.startswith("ffi_"):
        return name[4:], name
    if name.startswith("thread_"):
        stripped = name[7:]
        return stripped, stripped
    return name, name


def extract_offset_names(header_file: str) -> list[tuple[str, str]]:
    """
    Extrait la liste de (method_name, display_name) depuis thread.h.
    Retourne une liste ordonnée, sans doublons.
    """
    if not os.path.isfile(header_file):
        raise FileNotFoundError(
            f"thread.h introuvable : {header_file}\n"
            "  → Vérifiez que le SDK Dart est bien cloné."
        )

    content = Path(header_file).read_text(encoding="utf-8", errors="replace")

    seen:   set[str]           = set()
    result: list[tuple[str, str]] = []

    for pattern in PATTERNS:
        for m in pattern.finditer(content):
            raw_name = m.group(1)
            method, display = _strip_prefix(raw_name)

            key = (method, display)
            if key not in seen:
                seen.add(key)
                result.append(key)
                if VERBOSE:
                    print(f"  [DBG] found offset: raw={raw_name!r} → method={method!r}, display={display!r}",
                          file=sys.stderr)

    if not result:
        print(
            f"[AVERTISSEMENT] Aucun OFFSET_OF(Thread, ...) trouvé dans {header_file}",
            file=sys.stderr,
        )

    return result


def generate_cpp_lines(entries: list[tuple[str, str]]) -> list[str]:
    """Génère les lignes C++ à partir des (method, display) extraits."""
    lines = []
    for method, display in entries:
        lines.append(
            f'threadOffsetNames[dart::Thread::{method}_offset()] = "{display}";'
        )
    return lines


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Génère les lignes C++ threadOffsetNames depuis thread.h"
    )
    parser.add_argument("header_file", help="Chemin vers runtime/vm/thread.h")
    parser.add_argument("-o", "--output",
                        help="Fichier de sortie (défaut : stdout)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        os.environ["BLUTTER_VERBOSE"] = "1"
        global VERBOSE
        VERBOSE = True

    try:
        entries = extract_offset_names(args.header_file)
        lines   = generate_cpp_lines(entries)
        output  = "\n".join(lines) + "\n"

        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"  {len(lines)} lignes écrites dans {args.output}")
        else:
            print(output, end="")

    except FileNotFoundError as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
