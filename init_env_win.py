#!/usr/bin/env python3
"""
init_env_win.py — Initialisation de l'environnement Windows pour Ch-blutter.

Télécharge et installe ICU4C et Capstone (DLLs) dans le répertoire bin/.

Usage :
  python init_env_win.py
  python init_env_win.py --force    # Réinstalle même si déjà présent
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Optional

# ── Vérification OS ───────────────────────────────────────────────────────────
if sys.platform != "win32":
    print("[ERREUR] Ce script est uniquement pour Windows.", file=sys.stderr)
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[ERREUR] Module 'requests' requis : pip install requests", file=sys.stderr)
    sys.exit(1)

# ── Constantes ────────────────────────────────────────────────────────────────
ICU_VERSION      = "73_2"
ICU_VERSION_SHORT = ICU_VERSION.replace("_", "")[:2]   # "73"
CAPSTONE_VERSION = "4.0.2"

ICU_LIB_URL = (
    f"https://github.com/unicode-org/icu/releases/download/"
    f"release-73-2/icu4c-{ICU_VERSION}-Win64-MSVC2019.zip"
)
CAPSTONE_LIB_URL = (
    f"https://github.com/capstone-engine/capstone/releases/download/"
    f"{CAPSTONE_VERSION}/capstone-{CAPSTONE_VERSION}-win64.zip"
)

# SHA-256 attendus (laisser "" pour désactiver la vérification)
ICU_SHA256      = ""
CAPSTONE_SHA256 = ""

SCRIPT_DIR   = Path(__file__).resolve().parent
BASE_DIR     = SCRIPT_DIR
BIN_DIR      = BASE_DIR / "bin"
EXTERNAL_DIR = BASE_DIR / "external"

ICU_WINDOWS_DIR = EXTERNAL_DIR / "icu-windows"
CAPSTONE_DIR    = EXTERNAL_DIR / "capstone"

# DLLs à copier → destination dans bin/
NEEDED_DLLS: dict[Path, Path] = {
    CAPSTONE_DIR / "capstone.dll":
        BIN_DIR / "capstone.dll",
    ICU_WINDOWS_DIR / "bin64" / f"icudt{ICU_VERSION_SHORT}.dll":
        BIN_DIR / f"icudt{ICU_VERSION_SHORT}.dll",
    ICU_WINDOWS_DIR / "bin64" / f"icuuc{ICU_VERSION_SHORT}.dll":
        BIN_DIR / f"icuuc{ICU_VERSION_SHORT}.dll",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(
    url: str,
    label: str,
    expected_sha256: str = "",
    retries: int = 3,
    timeout: int = 120,
) -> bytes:
    """Télécharge une URL avec barre de progression, retry et vérification SHA-256."""
    import time

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            print(
                f"\r  Téléchargement {label} (tentative {attempt}/{retries})… ",
                end="", flush=True,
            )
            with requests.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                chunks: list[bytes] = []
                downloaded = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            print(
                                f"\r  Téléchargement {label}… {pct:3d}%",
                                end="", flush=True,
                            )
            data = b"".join(chunks)
            print(
                f"\r  Téléchargement {label}… OK ({len(data) // 1024} KB)   "
            )

            if expected_sha256:
                actual = _sha256(data)
                if actual.lower() != expected_sha256.lower():
                    raise ValueError(
                        f"SHA-256 incorrect pour {label}.\n"
                        f"  Attendu : {expected_sha256}\n"
                        f"  Obtenu  : {actual}"
                    )
            return data

        except (requests.RequestException, OSError) as e:
            last_exc = e
            print(
                f"\r  Téléchargement {label}… ÉCHEC (tentative {attempt}/{retries})  "
            )
            if attempt < retries:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Impossible de télécharger {label} après {retries} tentatives :\n"
        f"  {last_exc}"
    )


def _extract_zip(
    data: bytes,
    target_dir: Path,
    label: str,
    strip_root: bool = False,
):
    """
    Extrait une archive ZIP dans `target_dir`.
    strip_root=True supprime le dossier racine commun (--strip-components=1).
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        root = ""
        if strip_root and names:
            first = names[0].split("/")[0]
            if first:
                root = first + "/"

        for entry in names:
            dest_name = entry[len(root):] if root and entry.startswith(root) else entry
            if not dest_name:
                continue
            dest = target_dir / dest_name
            if entry.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(entry))

    print(f"  Extraction {label} → {target_dir}")


def _copy_dlls():
    """Copie les DLLs nécessaires vers BIN_DIR."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []

    for src, dst in NEEDED_DLLS.items():
        if not src.is_file():
            missing.append(str(src))
            continue
        shutil.copy2(src, dst)
        print(f"  Copié : {src.name} → bin/")

    if missing:
        print(
            "\n[AVERTISSEMENT] DLLs introuvables après extraction :\n" +
            "\n".join(f"  • {m}" for m in missing),
            file=sys.stderr,
        )
        print(
            "  Vérifiez les constantes ICU_VERSION / CAPSTONE_VERSION.",
            file=sys.stderr,
        )


def _already_installed() -> bool:
    """Vérifie si toutes les DLLs sont présentes dans bin/."""
    return all(dst.is_file() for dst in NEEDED_DLLS.values())


# ── Installation ICU ──────────────────────────────────────────────────────────

def install_icu():
    print("\n── ICU4C ──────────────────────────────────────────────")

    if ICU_WINDOWS_DIR.is_dir() and any(ICU_WINDOWS_DIR.rglob("*.dll")):
        print("  ICU déjà extrait — skip")
        return

    data = _download(ICU_LIB_URL, "ICU4C", expected_sha256=ICU_SHA256)

    # L'archive ICU contient un ZIP imbriqué
    with zipfile.ZipFile(io.BytesIO(data)) as outer:
        inner_name = next(
            (n for n in outer.namelist() if n.lower().endswith(".zip")),
            None,
        )
        if inner_name is None:
            # Certaines versions n'ont pas de ZIP imbriqué
            print("  Structure ICU directe (pas de ZIP imbriqué)")
            _extract_zip(data, ICU_WINDOWS_DIR, "ICU4C")
            return
        inner_data = outer.read(inner_name)

    _extract_zip(inner_data, ICU_WINDOWS_DIR, "ICU4C")


# ── Installation Capstone ─────────────────────────────────────────────────────

def install_capstone():
    print("\n── Capstone ────────────────────────────────────────────")

    if CAPSTONE_DIR.is_dir() and (CAPSTONE_DIR / "capstone.dll").is_file():
        print("  Capstone déjà extrait — skip")
        return

    if CAPSTONE_DIR.is_dir():
        shutil.rmtree(CAPSTONE_DIR)

    data = _download(CAPSTONE_LIB_URL, "Capstone", expected_sha256=CAPSTONE_SHA256)
    _extract_zip(data, CAPSTONE_DIR, "Capstone", strip_root=True)


# ── Point d'entrée ────────────────────────────────────────────────────────────

def main():
    force = "--force" in sys.argv

    print("Ch-blutter — Configuration Windows")
    print(f"  BIN_DIR      : {BIN_DIR}")
    print(f"  EXTERNAL_DIR : {EXTERNAL_DIR}")

    if _already_installed() and not force:
        print("\n  Toutes les DLLs sont déjà présentes dans bin/ — rien à faire.")
        print("  (Utilisez --force pour réinstaller.)")
        return

    EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)

    try:
        install_icu()
        install_capstone()

        print("\n── Copie des DLLs ──────────────────────────────────────")
        _copy_dlls()

        print("\n  ✔  Installation Windows terminée.")
        print(f"\n  Lancez ensuite : python blutter.py app.apk ./out")

    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
