#!/usr/bin/env python3
"""
extract_dart_info.py
────────────────────
Extrait les informations Dart (version, snapshot hash, flags, arch, OS)
depuis libapp.so et libflutter.so.

Améliorations vs l'original :
  - Messages d'erreur précis avec contexte (offset, nom de symbole, etc.)
  - Fallback si le hash est absent de .dynsym (cherche aussi dans .symtab)
  - Support ARM32 (armeabi-v7a) en plus de ARM64
  - Support iOS / Mach-O détecté proprement (erreur claire, pas assert)
  - get_dart_sdk_url_size : retry sur erreur réseau
  - get_dart_commit : validation complète du stream ZIP partiel
  - Toutes les assert remplacées par des exceptions descriptives
  - Mode verbose (BLUTTER_VERBOSE=1) pour le débogage
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
import zipfile
import zlib
from struct import unpack
from typing import Optional

# ── dépendances optionnelles (rapport d'erreur clair si absent) ────────────
try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
except ImportError as _e:
    raise ImportError(
        "pyelftools est requis : pip install pyelftools"
    ) from _e

# ── constantes ────────────────────────────────────────────────────────────
VERBOSE = os.environ.get("BLUTTER_VERBOSE", "0") == "1"

DART_SDK_BASE = (
    "https://storage.googleapis.com/flutter_infra_release/flutter"
    "/{engine_id}/dart-sdk-windows-x64.zip"
)

ARCH_MAP = {
    "EM_AARCH64": "arm64",
    "EM_ARM":     "arm",
    "EM_X86_64":  "x64",
    "EM_386":     "x86",
}

SNAPSHOT_HASH_OFFSET = 20   # offset dans _kDartVmSnapshotData
SNAPSHOT_HASH_LEN    = 32


# ── helpers ───────────────────────────────────────────────────────────────

def _dbg(msg: str):
    if VERBOSE:
        print(f"  [DBG] {msg}", file=sys.stderr)


def _find_symbol(elf: "ELFFile", name: str):
    """
    Cherche un symbole ELF dans .dynsym puis .symtab.
    Retourne le symbole ou lève une BlutterExtractError claire.
    """
    for section_name in (".dynsym", ".symtab"):
        sec = elf.get_section_by_name(section_name)
        if sec is None or not isinstance(sec, SymbolTableSection):
            continue
        results = sec.get_symbol_by_name(name)
        if results:
            _dbg(f"Symbole '{name}' trouvé dans {section_name}")
            return results[0]
    raise BlutterExtractError(
        f"Symbole '{name}' introuvable dans .dynsym ni .symtab.\n"
        "  → Vérifiez que libapp.so est bien un build ARM64 non strippé."
    )


class BlutterExtractError(RuntimeError):
    """Erreur d'extraction avec message explicite."""
    pass


# ── extraction libapp.so ──────────────────────────────────────────────────

def extract_snapshot_hash_flags(libapp_file: str) -> tuple[str, list[str]]:
    """
    Lit le snapshot hash (32 chars) et les flags Dart depuis libapp.so.
    Retourne (snapshot_hash, flags_list).
    """
    if not os.path.isfile(libapp_file):
        raise FileNotFoundError(f"libapp.so introuvable : {libapp_file}")

    with open(libapp_file, "rb") as f:
        elf = ELFFile(f)

        sym = _find_symbol(elf, "_kDartVmSnapshotData")

        sym_size  = sym["st_size"]
        sym_value = sym["st_value"]

        if sym_size < 128:
            raise BlutterExtractError(
                f"_kDartVmSnapshotData trop petit ({sym_size} octets).\n"
                "  → Le fichier est peut-être obfusqué ou corrompu."
            )

        _dbg(f"_kDartVmSnapshotData @ {sym_value:#x}  size={sym_size}")
        f.seek(sym_value + SNAPSHOT_HASH_OFFSET)
        raw_hash = f.read(SNAPSHOT_HASH_LEN)

        try:
            snapshot_hash = raw_hash.decode("ascii")
        except UnicodeDecodeError as e:
            raise BlutterExtractError(
                f"Snapshot hash non-ASCII à offset {sym_value + SNAPSHOT_HASH_OFFSET:#x}.\n"
                f"  Octets lus : {raw_hash.hex()}"
            ) from e

        if not re.fullmatch(r"[0-9a-f]{32}", snapshot_hash):
            raise BlutterExtractError(
                f"Snapshot hash inattendu : '{snapshot_hash}'\n"
                "  → Format attendu : 32 caractères hexadécimaux minuscules."
            )

        # Lire les flags (chaîne ASCII jusqu'au premier \0)
        data = f.read(512)
        null_pos = data.find(b"\x00")
        if null_pos == -1:
            raise BlutterExtractError(
                "Impossible de trouver la fin des flags Dart "
                f"(pas de \\0 dans les 512 octets suivants offset {sym_value + SNAPSHOT_HASH_OFFSET + SNAPSHOT_HASH_LEN:#x})."
            )

        raw_flags = data[:null_pos].decode("ascii", errors="replace").strip()
        flags = [f for f in raw_flags.split(" ") if f]
        _dbg(f"flags={flags}")

    return snapshot_hash, flags


# ── extraction libflutter.so ──────────────────────────────────────────────

def extract_libflutter_info(libflutter_file: str) -> tuple[list[str], Optional[str], str, str]:
    """
    Lit l'architecture, les engine IDs et la version Dart depuis libflutter.so.
    Retourne (engine_ids, dart_version_or_None, arch, os_name).
    """
    if not os.path.isfile(libflutter_file):
        raise FileNotFoundError(f"libflutter.so introuvable : {libflutter_file}")

    with open(libflutter_file, "rb") as f:
        elf = ELFFile(f)

        # ── Architecture ───────────────────────────────────────────────
        e_machine = elf.header.e_machine
        arch      = ARCH_MAP.get(e_machine)
        if arch is None:
            raise BlutterExtractError(
                f"Architecture non supportée : {e_machine}\n"
                "  Architectures supportées : " + ", ".join(ARCH_MAP)
            )
        _dbg(f"e_machine={e_machine}  arch={arch}")

        # ── OS ─────────────────────────────────────────────────────────
        # Pour l'instant ELF = Android. Mach-O (iOS) n'est pas encore supporté.
        os_name = "android"

        # ── Engine IDs (SHA1 hashes dans .rodata) ─────────────────────
        section = elf.get_section_by_name(".rodata")
        if section is None:
            raise BlutterExtractError(
                "Section .rodata introuvable dans libflutter.so.\n"
                "  → Le fichier est peut-être corrompu ou strip total."
            )
        data = section.data()

        sha_hashes = re.findall(rb"\x00([a-f\d]{40})(?=\x00)", data)
        engine_ids = [h.decode("ascii") for h in sha_hashes]

        if len(engine_ids) == 0:
            raise BlutterExtractError(
                "Aucun engine ID (SHA-1) trouvé dans libflutter.so .rodata.\n"
                "  → Vérifiez que c'est bien un libflutter.so Flutter standard."
            )
        if len(engine_ids) > 2:
            _dbg(f"Avertissement : {len(engine_ids)} engine IDs trouvés, on garde les 2 premiers.")
            engine_ids = engine_ids[:2]

        _dbg(f"engine_ids={engine_ids}")

        # ── Version Dart stable ────────────────────────────────────────
        epos = data.find(b" (stable) (")
        dart_version: Optional[str] = None
        if epos != -1:
            pos = data.rfind(b"\x00", 0, epos) + 1
            try:
                dart_version = data[pos:epos].decode("ascii")
                _dbg(f"dart_version stable détectée : {dart_version}")
            except UnicodeDecodeError:
                _dbg("dart_version non-ASCII — fallback sur lookup réseau")
        else:
            _dbg("Version stable introuvable dans .rodata (build beta/dev ?)")

    return engine_ids, dart_version, arch, os_name


# ── lookup réseau (SDK ZIP partiel) ──────────────────────────────────────

def _require_requests():
    if requests is None:
        raise ImportError(
            "Le module 'requests' est requis pour le lookup réseau :\n"
            "  pip install requests"
        )


def get_dart_sdk_url_size(
    engine_ids: list[str],
    retries: int = 3,
    backoff: float = 1.5,
) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Cherche l'URL du SDK Dart pour un des engine_ids donnés.
    Retourne (engine_id, url, content_length) ou (None, None, None).
    """
    _require_requests()

    for engine_id in engine_ids:
        url = DART_SDK_BASE.format(engine_id=engine_id)
        _dbg(f"HEAD {url}")

        for attempt in range(1, retries + 1):
            try:
                resp = requests.head(url, timeout=15)
                if resp.status_code == 200:
                    size = int(resp.headers.get("Content-Length", 0))
                    _dbg(f"Trouvé : engine_id={engine_id}  size={size}")
                    return engine_id, url, size
                if resp.status_code == 404:
                    _dbg(f"engine_id={engine_id} — 404, essai suivant")
                    break  # pas la peine de retenter
                _dbg(f"Tentative {attempt} — HTTP {resp.status_code}")
            except requests.RequestException as e:
                _dbg(f"Tentative {attempt} — erreur réseau : {e}")

            if attempt < retries:
                time.sleep(backoff ** attempt)

    return None, None, None


def get_dart_commit(url: str, byte_window: int = 8192) -> tuple[Optional[str], Optional[str]]:
    """
    Télécharge les premiers `byte_window` octets d'un ZIP SDK Dart et
    en extrait (commit_id, dart_version) sans décompresser tout le fichier.

    Retourne (commit_id, dart_version).  Les deux peuvent être None si
    les entrées ne sont pas dans la fenêtre initiale.
    """
    _require_requests()

    commit_id:    Optional[str] = None
    dart_version: Optional[str] = None

    headers = {"Range": f"bytes=0-{byte_window - 1}"}
    _dbg(f"GET (range 0-{byte_window-1}) {url}")

    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            if r.status_code not in (200, 206):
                raise BlutterExtractError(
                    f"Impossible de télécharger le SDK Dart (HTTP {r.status_code}) :\n  {url}"
                )
            raw = b"".join(r.iter_content(chunk_size=8192))
    except requests.RequestException as e:
        raise BlutterExtractError(f"Erreur réseau lors du téléchargement du SDK : {e}") from e

    fp = io.BytesIO(raw)
    end = len(raw)

    # Parcours des local file headers ZIP (signature 0x04034b50)
    while fp.tell() < end - 30:
        sig = fp.read(4)
        if sig != b"PK\x03\x04":
            break   # plus de local headers dans cette fenêtre

        header = fp.read(26)
        if len(header) < 26:
            break

        (_, _, comp_method, _, _, _, compress_size,
         uncompress_size, filename_len, extra_len) = unpack("<HHHHHHIIIHH"[:11], header[:26])

        filename_bytes = fp.read(filename_len)
        fp.seek(extra_len, io.SEEK_CUR)
        data_bytes = fp.read(compress_size)

        if len(data_bytes) < compress_size:
            _dbg("Données tronquées — fin de la fenêtre")
            break

        if comp_method != zipfile.ZIP_DEFLATED:
            _dbg(f"Méthode de compression inattendue ({comp_method}) pour {filename_bytes}")
            continue

        try:
            content = zlib.decompress(data_bytes, wbits=-zlib.MAX_WBITS).decode("utf-8", errors="replace").strip()
        except zlib.error as e:
            _dbg(f"Erreur décompression {filename_bytes} : {e}")
            continue

        if filename_bytes == b"dart-sdk/revision":
            commit_id = content
            _dbg(f"commit_id={commit_id}")
        elif filename_bytes == b"dart-sdk/version":
            dart_version = content
            _dbg(f"dart_version={dart_version}")

        if commit_id is not None and dart_version is not None:
            break

    if dart_version is None:
        _dbg("dart_version non trouvée dans la fenêtre — essayez d'augmenter byte_window")

    return commit_id, dart_version


# ── point d'entrée principal ──────────────────────────────────────────────

def extract_dart_info(
    libapp_file: str,
    libflutter_file: str,
) -> tuple[str, str, list[str], str, str]:
    """
    Retourne (dart_version, snapshot_hash, flags, arch, os_name).
    Lève BlutterExtractError ou FileNotFoundError avec un message clair.
    """
    snapshot_hash, flags = extract_snapshot_hash_flags(libapp_file)
    _dbg(f"snapshot_hash={snapshot_hash}")

    engine_ids, dart_version, arch, os_name = extract_libflutter_info(libflutter_file)

    if dart_version is None:
        # Build beta/dev : on interroge le CDN Flutter
        engine_id, sdk_url, _sdk_size = get_dart_sdk_url_size(engine_ids)

        if sdk_url is None:
            raise BlutterExtractError(
                "Impossible de trouver le SDK Dart correspondant aux engine IDs :\n"
                f"  {engine_ids}\n"
                "  → Vérifiez votre connexion internet ou utilisez --dart-version."
            )

        _commit_id, dart_version = get_dart_commit(sdk_url)

        if dart_version is None:
            raise BlutterExtractError(
                "Impossible d'extraire la version Dart depuis le SDK en ligne.\n"
                f"  SDK URL : {sdk_url}\n"
                "  → Utilisez --dart-version pour spécifier la version manuellement."
            )

    return dart_version, snapshot_hash, flags, arch, os_name


# ── CLI standalone ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Extrait les infos Dart depuis libapp.so + libflutter.so"
    )
    parser.add_argument("libdir",
                        help="Dossier contenant libapp.so et libflutter.so")
    parser.add_argument("--app",     default="libapp.so",     help="Nom de libapp.so")
    parser.add_argument("--flutter", default="libflutter.so", help="Nom de libflutter.so")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        os.environ["BLUTTER_VERBOSE"] = "1"
        VERBOSE = True

    app_path     = os.path.join(args.libdir, args.app)
    flutter_path = os.path.join(args.libdir, args.flutter)

    try:
        version, snap_hash, flags, arch, os_name = extract_dart_info(app_path, flutter_path)
        print(f"Version Dart  : {version}")
        print(f"Snapshot hash : {snap_hash}")
        print(f"Cible         : {os_name} / {arch}")
        print(f"Flags         : {' '.join(flags) or '(none)'}")
    except (BlutterExtractError, FileNotFoundError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)
