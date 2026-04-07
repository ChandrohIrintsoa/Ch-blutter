#!/usr/bin/env python3


from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────
#  Lecture du fichier tools/VERSION
# ─────────────────────────────────────────────────────────────────────────

def parse_tools_version(version_file: str) -> dict[str, str]:
    """
    Parse tools/VERSION au format clé-valeur (une par ligne).
    Ignore les lignes vides et les commentaires (#).

    Ex :
      CHANNEL stable
      MAJOR 3
      MINOR 4
    """
    if not os.path.isfile(version_file):
        raise FileNotFoundError(
            f"Fichier tools/VERSION introuvable : {version_file}\n"
            "  → Le clone du SDK Dart est peut-être incomplet."
        )

    vals: dict[str, str] = {}
    with open(version_file, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                print(
                    f"  [AVERTISSEMENT] tools/VERSION ligne {lineno} ignorée : {line!r}",
                    file=sys.stderr,
                )
                continue
            vals[parts[0]] = parts[1]

    required = {"MAJOR", "MINOR", "PATCH", "CHANNEL"}
    missing  = required - vals.keys()
    if missing:
        raise RuntimeError(
            f"Clés manquantes dans tools/VERSION : {', '.join(sorted(missing))}"
        )

    return vals


# ─────────────────────────────────────────────────────────────────────────
#  Git helpers
# ─────────────────────────────────────────────────────────────────────────

def _git_output(args: list[str], cwd: str) -> str:
    """Lance une commande git et retourne stdout.strip()."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        raise RuntimeError(
            "git introuvable — impossible de générer version.cc.\n"
            "  → Installez git : pkg install git"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout git lors de la génération de version.cc")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Commande git échouée : git {' '.join(args)}\n"
            f"  Stderr : {stderr}"
        )


def get_short_git_hash(cwd: str) -> str:
    return _git_output(["rev-parse", "--short=10", "HEAD"], cwd)


def get_git_timestamp(cwd: str) -> str:
    return _git_output(["log", "-n", "1", "--pretty=format:%cd"], cwd)


# ─────────────────────────────────────────────────────────────────────────
#  Génération de version.cc
# ─────────────────────────────────────────────────────────────────────────

def generate_version_cc(
    sdk_dir: str,
    snapshot_hash: str,
    dry_run: bool = False,
) -> str:
    """
    Lit version_in.cc, substitue les {{VARIABLES}}, écrit version.cc.
    Retourne le contenu généré.

    sdk_dir       – racine du SDK Dart cloné
    snapshot_hash – hash du snapshot Dart (32 chars hex)
    dry_run       – si True, affiche le résultat sans écrire le fichier
    """
    sdk_path       = Path(sdk_dir)
    version_in     = sdk_path / "runtime" / "vm" / "version_in.cc"
    version_out    = sdk_path / "runtime" / "vm" / "version.cc"
    tools_ver_file = sdk_path / "tools" / "VERSION"

    # ── Validation des entrées ─────────────────────────────────────────
    if not version_in.is_file():
        raise FileNotFoundError(
            f"version_in.cc introuvable : {version_in}\n"
            "  → Le clone du SDK Dart est incomplet."
        )

    if not snapshot_hash:
        raise ValueError("snapshot_hash est vide ou None")

    if len(snapshot_hash) != 32 or not snapshot_hash.isalnum():
        print(
            f"  [AVERTISSEMENT] Snapshot hash inhabituel : '{snapshot_hash}'",
            file=sys.stderr,
        )

    # ── Collecte des variables ─────────────────────────────────────────
    version_info = parse_tools_version(str(tools_ver_file))

    version_str = (
        f"{version_info['MAJOR']}.{version_info['MINOR']}.{version_info['PATCH']}"
    )
    version_info["VERSION_STR"]   = version_str
    version_info["SNAPSHOT_HASH"] = snapshot_hash

    # Git hash et timestamp (non fatals)
    try:
        version_info["GIT_HASH"]    = get_short_git_hash(str(sdk_path))
        version_info["COMMIT_TIME"] = get_git_timestamp(str(sdk_path))
    except RuntimeError as e:
        print(f"  [AVERTISSEMENT] {e}", file=sys.stderr)
        version_info.setdefault("GIT_HASH",    "unknown")
        version_info.setdefault("COMMIT_TIME", "unknown")

    # ── Substitution dans version_in.cc ──────────────────────────────
    template = version_in.read_text(encoding="utf-8")
    result   = template

    for key, value in version_info.items():
        placeholder = "{{" + key + "}}"
        result = result.replace(placeholder, value)

    # Détecte les placeholders non résolus
    remaining = set(
        m.group(0)
        for m in __import__("re").finditer(r"\{\{[A-Z_]+\}\}", result)
    )
    if remaining:
        print(
            f"  [AVERTISSEMENT] Placeholders non résolus dans version_in.cc : "
            f"{', '.join(sorted(remaining))}",
            file=sys.stderr,
        )

    # ── Écriture ──────────────────────────────────────────────────────
    if dry_run:
        print("  [DRY-RUN] version.cc (non écrit) :")
        print(result)
    else:
        version_out.write_text(result, encoding="utf-8")
        print(f"  version.cc généré : {version_out}")

    return result


# ─────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Génère runtime/vm/version.cc pour la Dart VM"
    )
    parser.add_argument("sdk_dir",
                        help="Répertoire racine du SDK Dart cloné")
    parser.add_argument("snapshot_hash",
                        help="Hash snapshot Dart (32 chars hex)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le résultat sans écrire le fichier")
    args = parser.parse_args()

    try:
        generate_version_cc(
            sdk_dir=args.sdk_dir,
            snapshot_hash=args.snapshot_hash,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
