#!/usr/bin/env python3
"""
extract_dart_info.py — Extraction des métadonnées Dart depuis libapp.so + libflutter.so

Utilisation autonome :
  python extract_dart_info.py ./libs/arm64-v8a
  python extract_dart_info.py ./libs/arm64-v8a --app myapp.so --flutter myflutter.so
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

# ── Dépendances optionnelles ──────────────────────────────────────────────────
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    requests = None  # type: ignore

try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
    HAS_ELFTOOLS = True
except ImportError as _e:
    HAS_ELFTOOLS = False

# ── Constantes ────────────────────────────────────────────────────────────────
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


class BlutterExtractError(RuntimeError):
    """Erreur d'extraction avec message explicite."""
    pass


def _dbg(msg: str):
    if VERBOSE:
        print(f"  [DBG] {msg}", file=sys.stderr)


def _require_elftools():
    if not HAS_ELFTOOLS:
        raise ImportError(
            "pyelftools est requis : pip install pyelftools"
        )


def _require_requests():
    if not HAS_REQUESTS:
        raise ImportError(
            "Le module 'requests' est requis pour le lookup réseau :\n"
            "  pip install requests"
        )


# ── Recherche de symboles ELF ─────────────────────────────────────────────────

def _find_symbol(elf: "ELFFile", name: str):
    """
    Cherche un symbole ELF dans .dynsym puis .symtab.
    Retourne le symbole ou lève BlutterExtractError.
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
        "  → Vérifiez que le fichier est un build ARM64 valide avec symboles Dart.\n"
        "  → Si le fichier est strippé, spécifiez --dart-version manuellement."
    )


# ── Extraction depuis libapp.so ───────────────────────────────────────────────

def extract_snapshot_hash_flags(libapp_file: str) -> tuple[str, list[str]]:
    """
    Lit le snapshot hash (32 chars) et les flags Dart depuis libapp.so.
    Retourne (snapshot_hash, flags_list).
    Lève BlutterExtractError ou FileNotFoundError.
    """
    _require_elftools()

    if not os.path.isfile(libapp_file):
        raise FileNotFoundError(f"Fichier introuvable : {libapp_file}")

    with open(libapp_file, "rb") as f:
        try:
            elf = ELFFile(f)
        except Exception as e:
            raise BlutterExtractError(
                f"Impossible de parser le fichier ELF : {libapp_file}\n"
                f"  Erreur : {e}"
            )

        sym = _find_symbol(elf, "_kDartVmSnapshotData")

        sym_size  = sym["st_size"]
        sym_value = sym["st_value"]

        if sym_size > 0 and sym_size < 128:
            raise BlutterExtractError(
                f"_kDartVmSnapshotData trop petit ({sym_size} octets).\n"
                "  → Le fichier est peut-être obfusqué ou corrompu."
            )

        _dbg(f"_kDartVmSnapshotData @ {sym_value:#x}  size={sym_size}")

        # Navigation vers l'offset du hash dans le fichier
        # sym_value est une adresse virtuelle — on cherche la section contenant cette adresse
        file_offset = _va_to_file_offset(elf, sym_value)
        if file_offset is None:
            # Fallback : utiliser sym_value comme offset direct (cas de certains builds)
            file_offset = sym_value
            _dbg(f"VA→offset introuvable, utilisation directe : {file_offset:#x}")

        f.seek(file_offset + SNAPSHOT_HASH_OFFSET)
        raw_hash = f.read(SNAPSHOT_HASH_LEN)

        if len(raw_hash) < SNAPSHOT_HASH_LEN:
            raise BlutterExtractError(
                f"Lecture tronquée du snapshot hash à offset {file_offset + SNAPSHOT_HASH_OFFSET:#x}."
            )

        try:
            snapshot_hash = raw_hash.decode("ascii")
        except UnicodeDecodeError:
            raise BlutterExtractError(
                f"Snapshot hash non-ASCII à offset {file_offset + SNAPSHOT_HASH_OFFSET:#x}.\n"
                f"  Octets lus : {raw_hash.hex()}"
            )

        if not re.fullmatch(r"[0-9a-f]{32}", snapshot_hash):
            raise BlutterExtractError(
                f"Snapshot hash inattendu : '{snapshot_hash}'\n"
                "  → Format attendu : 32 caractères hexadécimaux minuscules."
            )

        # Lecture des flags Dart (chaîne ASCII après le hash)
        data = f.read(512)
        null_pos = data.find(b"\x00")
        if null_pos == -1:
            _dbg("Fin des flags non trouvée dans les 512 octets suivants")
            raw_flags = data.decode("ascii", errors="replace").strip()
        else:
            raw_flags = data[:null_pos].decode("ascii", errors="replace").strip()

        flags = [flag for flag in raw_flags.split() if flag]
        _dbg(f"snapshot_hash={snapshot_hash}  flags={flags}")

    return snapshot_hash, flags


def _va_to_file_offset(elf: "ELFFile", va: int) -> Optional[int]:
    """
    Convertit une adresse virtuelle en offset dans le fichier ELF.
    """
    for seg in elf.iter_segments():
        p_vaddr  = seg["p_vaddr"]
        p_filesz = seg["p_filesz"]
        p_offset = seg["p_offset"]
        if p_vaddr <= va < p_vaddr + p_filesz:
            return p_offset + (va - p_vaddr)
    return None


# ── Extraction depuis libflutter.so ──────────────────────────────────────────

def extract_libflutter_info(
    libflutter_file: str,
) -> tuple[list[str], Optional[str], str, str]:
    """
    Lit l'architecture, les engine IDs et la version Dart depuis libflutter.so.
    Retourne (engine_ids, dart_version_or_None, arch, os_name).
    """
    _require_elftools()

    if not os.path.isfile(libflutter_file):
        raise FileNotFoundError(f"Fichier introuvable : {libflutter_file}")

    with open(libflutter_file, "rb") as f:
        try:
            elf = ELFFile(f)
        except Exception as e:
            raise BlutterExtractError(
                f"Impossible de parser le fichier ELF : {libflutter_file}\n"
                f"  Erreur : {e}"
            )

        # ── Architecture ───────────────────────────────────────────────────
        e_machine = elf.header.e_machine
        arch      = ARCH_MAP.get(e_machine)
        if arch is None:
            raise BlutterExtractError(
                f"Architecture non supportée : {e_machine}\n"
                f"  Architectures supportées : {', '.join(ARCH_MAP)}"
            )
        _dbg(f"e_machine={e_machine}  arch={arch}")

        # ── OS ─────────────────────────────────────────────────────────────
        os_name = "android"  # ELF = Android; Mach-O (iOS) non encore supporté

        # ── Engine IDs ─────────────────────────────────────────────────────
        section = elf.get_section_by_name(".rodata")
        if section is None:
            raise BlutterExtractError(
                "Section .rodata introuvable dans libflutter.so.\n"
                "  → Le fichier est peut-être corrompu ou totalement strippé."
            )
        data = section.data()

        sha_hashes = re.findall(rb"\x00([a-f0-9]{40})(?=\x00)", data)
        engine_ids = list(dict.fromkeys(  # déduplique en préservant l'ordre
            h.decode("ascii") for h in sha_hashes
        ))

        if not engine_ids:
            raise BlutterExtractError(
                "Aucun engine ID (SHA-1) trouvé dans libflutter.so .rodata.\n"
                "  → Vérifiez que c'est bien un libflutter.so Flutter standard."
            )
        if len(engine_ids) > 2:
            _dbg(f"{len(engine_ids)} engine IDs, on garde les 2 premiers.")
            engine_ids = engine_ids[:2]

        _dbg(f"engine_ids={engine_ids}")

        # ── Version Dart stable ────────────────────────────────────────────
        dart_version: Optional[str] = None
        epos = data.find(b" (stable) (")
        if epos != -1:
            pos = data.rfind(b"\x00", 0, epos) + 1
            try:
                dart_version = data[pos:epos].decode("ascii").strip()
                _dbg(f"dart_version stable : {dart_version}")
            except (UnicodeDecodeError, ValueError):
                _dbg("dart_version non-ASCII — fallback réseau")
        else:
            _dbg("Version stable introuvable dans .rodata (build beta/dev ?)")

    return engine_ids, dart_version, arch, os_name


# ── Lookup réseau SDK ─────────────────────────────────────────────────────────

def get_dart_sdk_url_size(
    engine_ids: list[str],
    retries: int = 3,
    backoff: float = 1.5,
) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Cherche l'URL du SDK Dart pour un des engine_ids.
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
                    _dbg(f"engine_id={engine_id} — 404")
                    break
                _dbg(f"Tentative {attempt} — HTTP {resp.status_code}")
            except requests.RequestException as e:
                _dbg(f"Tentative {attempt} — erreur réseau : {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)

    return None, None, None


def get_dart_commit(url: str, byte_window: int = 8192) -> tuple[Optional[str], Optional[str]]:
    """
    Télécharge les premiers bytes d'un ZIP SDK Dart et en extrait
    (commit_id, dart_version) sans décompresser tout le fichier.
    """
    _require_requests()

    commit_id: Optional[str]    = None
    dart_version: Optional[str] = None

    headers = {"Range": f"bytes=0-{byte_window - 1}"}
    _dbg(f"GET (range 0-{byte_window-1}) {url}")

    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            if r.status_code not in (200, 206):
                raise BlutterExtractError(
                    f"HTTP {r.status_code} lors du téléchargement SDK :\n  {url}"
                )
            raw = b"".join(r.iter_content(chunk_size=8192))
    except requests.RequestException as e:
        raise BlutterExtractError(
            f"Erreur réseau lors du téléchargement SDK : {e}"
        ) from e

    fp  = io.BytesIO(raw)
    end = len(raw)

    while fp.tell() < end - 30:
        sig = fp.read(4)
        if sig != b"PK\x03\x04":
            break

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

        if comp_method == zipfile.ZIP_DEFLATED:
            try:
                content = zlib.decompress(
                    data_bytes, wbits=-zlib.MAX_WBITS
                ).decode("utf-8", errors="replace").strip()
            except zlib.error as e:
                _dbg(f"Erreur décompression {filename_bytes} : {e}")
                continue
        elif comp_method == zipfile.ZIP_STORED:
            content = data_bytes.decode("utf-8", errors="replace").strip()
        else:
            _dbg(f"Méthode de compression inattendue ({comp_method}) pour {filename_bytes}")
            continue

        if filename_bytes == b"dart-sdk/revision":
            commit_id = content
            _dbg(f"commit_id={commit_id}")
        elif filename_bytes == b"dart-sdk/version":
            dart_version = content
            _dbg(f"dart_version={dart_version}")

        if commit_id is not None and dart_version is not None:
            break

    return commit_id, dart_version


# ── Point d'entrée principal ──────────────────────────────────────────────────

def extract_dart_info(
    libapp_file: str,
    libflutter_file: str,
) -> tuple[str, str, list[str], str, str]:
    """
    Extrait et retourne (dart_version, snapshot_hash, flags, arch, os_name).
    Lève BlutterExtractError ou FileNotFoundError avec message clair.
    """
    if not HAS_ELFTOOLS:
        raise ImportError(
            "pyelftools est requis : pip install pyelftools"
        )

    snapshot_hash, flags = extract_snapshot_hash_flags(libapp_file)
    engine_ids, dart_version, arch, os_name = extract_libflutter_info(libflutter_file)

    if dart_version is None:
        if not HAS_REQUESTS:
            raise BlutterExtractError(
                "Version Dart introuvable localement et 'requests' absent pour le lookup réseau.\n"
                "  pip install requests\n"
                "  Ou spécifiez --dart-version VERSION_OS_ARCH."
            )
        # Lookup réseau pour builds beta/dev
        _engine_id, sdk_url, _sdk_size = get_dart_sdk_url_size(engine_ids)
        if sdk_url is None:
            raise BlutterExtractError(
                "Impossible de trouver le SDK Dart correspondant aux engine IDs :\n"
                f"  {engine_ids}\n"
                "  → Vérifiez votre connexion internet.\n"
                "  → Utilisez --dart-version pour spécifier la version manuellement."
            )
        _commit_id, dart_version = get_dart_commit(sdk_url)
        if dart_version is None:
            raise BlutterExtractError(
                "Impossible d'extraire la version Dart depuis le SDK en ligne.\n"
                f"  SDK URL : {sdk_url}\n"
                "  → Utilisez --dart-version pour spécifier la version manuellement."
            )

    return dart_version, snapshot_hash, flags, arch, os_name


# ── CLI autonome ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Extrait les infos Dart depuis libapp.so + libflutter.so"
    )
    parser.add_argument("libdir",
                        help="Dossier contenant les .so cibles")
    parser.add_argument("--app",     default=None,
                        help="Nom/chemin du .so Dart app (auto-détecté si absent)")
    parser.add_argument("--flutter", default="libflutter.so",
                        help="Nom/chemin de libflutter.so")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        os.environ["BLUTTER_VERBOSE"] = "1"
        VERBOSE = True

    libdir = args.libdir

    # Auto-détection du .so Dart si non spécifié
    if args.app is None:
        # Cherche un .so non-flutter dans le dossier
        flutter_lower = args.flutter.lower()
        candidates = [
            f for f in os.listdir(libdir)
            if f.lower().endswith(".so") and f.lower() != flutter_lower
        ]
        if not candidates:
            print("[ERREUR] Aucun .so app trouvé dans le dossier.", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print("Plusieurs .so app candidats :", file=sys.stderr)
            for i, c in enumerate(candidates):
                print(f"  [{i}] {c}", file=sys.stderr)
            raw = input("Choisissez le numéro [0] : ").strip() or "0"
            app_file = os.path.join(libdir, candidates[int(raw) if raw.isdigit() else 0])
        else:
            app_file = os.path.join(libdir, candidates[0])
    else:
        app_file = args.app if os.path.isabs(args.app) else os.path.join(libdir, args.app)

    flutter_file = (
        args.flutter if os.path.isabs(args.flutter)
        else os.path.join(libdir, args.flutter)
    )

    try:
        version, snap_hash, flags, arch, os_name = extract_dart_info(app_file, flutter_file)
        print(f"Version Dart  : {version}")
        print(f"Snapshot hash : {snap_hash}")
        print(f"Cible         : {os_name} / {arch}")
        print(f"Flags         : {' '.join(flags) or '(aucun)'}")
    except (BlutterExtractError, FileNotFoundError, ImportError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)
