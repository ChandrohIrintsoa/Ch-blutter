#!/usr/bin/env python3
"""
dartvm_fetch_build.py
─────────────────────
Clone le SDK Dart (sparse), patch pour Python 3.12+, génère la liste
de sources, compile la lib statique Dart VM via CMake/Ninja.

Améliorations vs l'original :
  - DartLibInfo.from_string(str)  — parsing sûr du format "VER_OS_ARCH"
  - Toutes les assert → exceptions avec message clair
  - checkout_dart : cleanup atomique si clone incomplet
  - cmake_dart : vérifie cmake/ninja avant de lancer
  - fetch_and_build : crée les dossiers nécessaires au préalable
  - Retry sur les opérations git réseau (fetch/clone)
  - Support BLUTTER_VERBOSE pour le débogage
  - Typage complet (Python 3.9+)
"""

from __future__ import annotations

import mmap
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ── constantes ────────────────────────────────────────────────────────────
VERBOSE = os.environ.get("BLUTTER_VERBOSE", "0") == "1"

GIT_CMD   = os.environ.get("GIT",   "git")
CMAKE_CMD = os.environ.get("CMAKE", "cmake")
NINJA_CMD = os.environ.get("NINJA", "ninja")

SCRIPT_DIR          = os.path.dirname(os.path.realpath(__file__))
CMAKE_TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "scripts", "CMakeLists.txt")
CREATE_SRCLIST_FILE = os.path.join(SCRIPT_DIR, "scripts", "dartvm_create_srclist.py")
MAKE_VERSION_FILE   = os.path.join(SCRIPT_DIR, "scripts", "dartvm_make_version.py")

SDK_DIR   = os.path.join(SCRIPT_DIR, "dartsdk")
BUILD_DIR = os.path.join(SCRIPT_DIR, "build")

DART_GIT_URL = "https://github.com/dart-lang/sdk.git"

# Patch Python 3.12 (suppression de imp)
_IMP_REPLACE = """\
import importlib.util
import importlib.machinery

def load_source(modname, filename):
    loader = importlib.machinery.SourceFileLoader(modname, filename)
    spec   = importlib.util.spec_from_file_location(modname, filename, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module
"""

# Mapping version → groupe de compatibilité binaire
DART_VERSION_GROUPS: dict[str, list[str]] = {
    "3.0_aa":  ["3.0.0", "3.0.1", "3.0.2"],
    "3.0_90":  ["3.0.3", "3.0.4", "3.0.5", "3.0.6", "3.0.7"],
    "3.1":     ["3.1.0", "3.1.1", "3.1.2", "3.1.3", "3.1.4", "3.1.5"],
    "3.2":     ["3.2.0", "3.2.1", "3.2.2", "3.2.3", "3.2.4", "3.2.5", "3.2.6"],
    "3.3":     ["3.3.0", "3.3.1", "3.3.2", "3.3.3", "3.3.4"],
    "3.4":     ["3.4.0", "3.4.1", "3.4.2", "3.4.3", "3.4.4"],
    "3.5":     ["3.5.0", "3.5.1", "3.5.2", "3.5.3", "3.5.4"],
    "3.6":     ["3.6.1", "3.6.2"],
    "3.7":     ["3.7.0", "3.7.1", "3.7.2"],
    "3.8":     ["3.8.0", "3.8.1"],
    "3.9":     ["3.9.0", "3.9.2"],
    "3.10":    ["3.10.0", "3.10.1", "3.10.3", "3.10.4",
                "3.10.7", "3.10.8", "3.10.9"],
}


# ── helpers ───────────────────────────────────────────────────────────────

class BlutterBuildError(RuntimeError):
    """Erreur de build avec message explicite."""
    pass


def _dbg(msg: str):
    if VERBOSE:
        print(f"  [DBG] {msg}", file=sys.stderr)


def _require_tool(cmd: str):
    if shutil.which(cmd) is None:
        raise BlutterBuildError(
            f"Outil requis introuvable : '{cmd}'\n"
            f"  → Termux : pkg install {cmd}"
        )


def _run(args: list, cwd: str = None, check: bool = True,
         timeout: int = None, retries: int = 1, **kwargs) -> subprocess.CompletedProcess:
    """
    Lance une sous-commande avec logging optionnel et retry.
    """
    _dbg(f"run: {' '.join(str(a) for a in args)}")
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                check=check,
                timeout=timeout,
                **kwargs,
            )
            return result
        except subprocess.TimeoutExpired as e:
            raise BlutterBuildError(
                f"Timeout ({timeout}s) dépassé : {' '.join(str(a) for a in args)}"
            ) from e
        except subprocess.CalledProcessError as e:
            last_exc = e
            if attempt < retries:
                _dbg(f"Tentative {attempt}/{retries} échouée, retry dans {2**attempt}s")
                time.sleep(2 ** attempt)
    raise last_exc


def _rmtree_robust(path: str):
    """Supprime un dossier même si des fichiers sont en lecture seule (Windows)."""
    def _on_error(func, fpath, _):
        try:
            os.chmod(fpath, stat.S_IWRITE)
            func(fpath)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_on_error)


# ── DartLibInfo ───────────────────────────────────────────────────────────

class DartLibInfo:
    """
    Informations sur une lib Dart VM.

    Paramètres :
      version              – ex: "3.4.2"
      os_name              – "android" | "ios"
      arch                 – "arm64" | "arm" | "x64"
      has_compressed_ptrs  – True/False (None = déduction depuis os_name)
      snapshot_hash        – hash du snapshot (optionnel)
    """

    def __init__(
        self,
        version: str,
        os_name: str,
        arch: str,
        has_compressed_ptrs: Optional[bool] = None,
        snapshot_hash: Optional[str] = None,
    ):
        self._validate_inputs(version, os_name, arch)

        self.version       = version
        self.os_name       = os_name
        self.arch          = arch
        self.snapshot_hash = snapshot_hash

        # Par défaut Flutter compresse les pointeurs sauf iOS
        self.has_compressed_ptrs = (
            has_compressed_ptrs if has_compressed_ptrs is not None
            else (os_name != "ios")
        )

        # Résolution du nom de lib (avec compatibilité de groupe)
        resolved_version = self._resolve_version(version, os_name, arch)
        self.version      = resolved_version
        self.lib_name     = f"dartvm{resolved_version}_{os_name}_{arch}"

    # ── validation ────────────────────────────────────────────────────────

    @staticmethod
    def _validate_inputs(version: str, os_name: str, arch: str):
        if not version or not version[0].isdigit():
            raise BlutterBuildError(
                f"Version Dart invalide : '{version}'\n"
                "  Exemple valide : '3.4.2'"
            )
        valid_os   = {"android", "ios", "linux", "windows", "macos"}
        valid_arch = {"arm64", "arm", "x64", "x86"}
        if os_name not in valid_os:
            raise BlutterBuildError(
                f"os_name invalide : '{os_name}'  (valides : {', '.join(sorted(valid_os))})"
            )
        if arch not in valid_arch:
            raise BlutterBuildError(
                f"arch invalide : '{arch}'  (valides : {', '.join(sorted(valid_arch))})"
            )

    # ── résolution de groupe de compatibilité ─────────────────────────────

    @staticmethod
    def _resolve_version(version: str, os_name: str, arch: str) -> str:
        """
        Si un exécutable compilé pour un autre patch de la même série
        existe déjà dans bin/, on le réutilise.
        """
        bin_dir = os.path.join(SCRIPT_DIR, "bin")
        if not os.path.isdir(bin_dir):
            return version

        suffixes = [
            "", "_no-analysis", "_ida-fcn", "_no-analysis_ida-fcn",
            "_no-compressed-ptrs", "_no-compressed-ptrs_no-analysis",
            "_no-compressed-ptrs_no-analysis_ida-fcn",
            "_no-compressed-ptrs_ida-fcn",
        ]

        # Recueille les versions déjà compilées pour cet OS/arch
        compiled_versions: set[str] = set()
        for fname in os.listdir(bin_dir):
            for sfx in suffixes:
                expected_end = f"{os_name}_{arch}{sfx}"
                if fname.startswith("blutter_dartvm") and fname.endswith(expected_end):
                    # Extrait la version : blutter_dartvm<VER>_<os>_<arch>[suffix]
                    inner = fname[len("blutter_dartvm"):-len(sfx) - len(f"_{os_name}_{arch}") or None]
                    # Retire le dernier _os_arch
                    candidate = fname[len("blutter_dartvm"):]
                    candidate = candidate[: -(len(sfx) + len(f"_{os_name}_{arch}")) or None]
                    # Gère les groupes de version
                    compiled_versions.add(candidate)

        for _group, versions_in_group in DART_VERSION_GROUPS.items():
            if version in versions_in_group:
                for cv in compiled_versions:
                    if cv in versions_in_group:
                        _dbg(
                            f"Réutilisation de la lib compilée pour {cv} "
                            f"(compatible avec {version})"
                        )
                        return cv

        return version

    # ── constructeur depuis une chaîne "VER_OS_ARCH" ─────────────────────

    @classmethod
    def from_string(cls, dart_version_str: str, **kwargs) -> "DartLibInfo":
        """
        Construit un DartLibInfo depuis le format "3.4.2_android_arm64".
        Lève BlutterBuildError si le format est incorrect.
        """
        parts = dart_version_str.strip().split("_")
        if len(parts) != 3:
            raise BlutterBuildError(
                f"Format --dart-version invalide : '{dart_version_str}'\n"
                "  Attendu : VERSION_OS_ARCH  ex: 3.4.2_android_arm64"
            )
        version, os_name, arch = parts
        return cls(version, os_name, arch, **kwargs)

    def __repr__(self) -> str:
        return (
            f"DartLibInfo({self.version!r}, {self.os_name!r}, {self.arch!r}, "
            f"compressed={self.has_compressed_ptrs})"
        )


# ── clone Dart SDK ────────────────────────────────────────────────────────

def checkout_dart(info: DartLibInfo, git_retries: int = 2) -> str:
    """
    Clone (sparse) le SDK Dart à la version exacte si absent.
    Retourne le chemin vers le répertoire cloné.
    """
    _require_tool(GIT_CMD)

    clone_dir    = os.path.join(SDK_DIR, "v" + info.version)
    version_file = os.path.join(clone_dir, "runtime", "vm", "version.cc")

    # Nettoyage si clone précédent incomplet
    if os.path.isdir(clone_dir) and not os.path.isfile(version_file):
        print(f"  Clone incomplet détecté — suppression de {clone_dir}")
        _rmtree_robust(clone_dir)

    # Clone si dossier absent
    if not os.path.isdir(clone_dir):
        print(f"  Clonage de Dart SDK {info.version}…")
        Path(clone_dir).parent.mkdir(parents=True, exist_ok=True)

        _run(
            [GIT_CMD, "-c", "advice.detachedHead=false",
             "clone", "-b", info.version,
             "--depth", "1", "--filter=blob:none",
             "--sparse", "--progress",
             DART_GIT_URL, clone_dir],
            retries=git_retries,
            timeout=300,
        )

        # Checkout sparse : seulement les sources nécessaires
        _run(
            [GIT_CMD, "sparse-checkout", "set",
             "runtime", "tools", "third_party/double-conversion"],
            cwd=clone_dir,
        )

        # Supprimer les fichiers racine inutiles (pas les dossiers)
        for entry in os.scandir(clone_dir):
            if entry.is_file():
                try:
                    os.remove(entry.path)
                except OSError:
                    pass

        # Patch Python 3.12+ si besoin
        if info.snapshot_hash is None:
            _patch_python312(clone_dir)
            _make_version_official(clone_dir)
        else:
            _make_version_custom(clone_dir, info.snapshot_hash)

        # Patch Windows ARM64 (Dart ≥3.8)
        if sys.platform == "win32":
            _patch_win32_arm64(clone_dir, info.version)

    return clone_dir


def _patch_python312(clone_dir: str):
    """Corrige tools/utils.py pour Python 3.12 (supprime le module imp)."""
    if sys.version_info < (3, 12):
        return

    utils_path = os.path.join(clone_dir, "tools", "utils.py")
    if not os.path.exists(utils_path):
        return

    with open(utils_path, "r+", encoding="utf-8") as f:
        content = f.read()

        # Déjà patché ou pas besoin
        if "import importlib.util" in content:
            return

        patched = content
        # Corrige les chaînes d'échappement invalides (SyntaxWarning → SyntaxError en 3.12)
        if r"match_against('^MAJOR (\d+)$', content)" in patched:
            patched = (
                patched
                .replace(" ' awk ", " r' awk ")
                .replace("match_against('", "match_against(r'")
                .replace("re.search('", "re.search(r'")
            )

        # Remplace `import imp` par importlib
        if "import imp\n" in patched:
            patched = patched.replace("import imp\n", _IMP_REPLACE)
            patched = patched.replace("imp.load_source", "load_source")

        if patched != content:
            f.seek(0)
            f.truncate()
            f.write(patched)
            _dbg("tools/utils.py patché pour Python 3.12+")


def _make_version_official(clone_dir: str):
    """Génère runtime/vm/version.cc via tools/make_version.py."""
    _run(
        [sys.executable, "tools/make_version.py",
         "--output", "runtime/vm/version.cc",
         "--input",  "runtime/vm/version_in.cc"],
        cwd=clone_dir,
    )
    _dbg("version.cc généré (officiel)")


def _make_version_custom(clone_dir: str, snapshot_hash: str):
    """Génère runtime/vm/version.cc avec le snapshot hash personnalisé."""
    if not os.path.isfile(MAKE_VERSION_FILE):
        raise BlutterBuildError(
            f"Script de version introuvable : {MAKE_VERSION_FILE}\n"
            "  → Vérifiez l'intégrité du dépôt blutter."
        )
    _run([sys.executable, MAKE_VERSION_FILE, clone_dir, snapshot_hash])
    _dbg(f"version.cc généré (custom hash={snapshot_hash[:8]}…)")


def _patch_win32_arm64(clone_dir: str, version: str):
    """
    Depuis Dart 3.8, RUNTIME_FUNCTION est déclaré pour Windows+ARM64.
    Ce patch commente la ligne incriminée.
    """
    try:
        major, minor = int(version.split(".")[0]), int(version.split(".")[1])
    except (ValueError, IndexError):
        return

    if (major, minor) < (3, 8):
        return

    hdr = os.path.join(clone_dir, "runtime", "platform", "unwinding_records.h")
    if not os.path.isfile(hdr):
        return

    with open(hdr, "r+b") as f:
        mm = mmap.mmap(f.fileno(), 0)
        target = b"\n#if !defined(DART_HOST_OS_WINDOWS) || !defined(HOST_ARCH_ARM64)"
        pos = mm.find(target)
        if pos != -1:
            mm[pos + 36: pos + 38] = b"//"  # commente "||"
        else:
            target2 = b"\nstatic_assert(sizeof("
            pos = mm.find(target2)
            if pos != -1:
                mm[pos + 1: pos + 3] = b"//"
        mm.close()
    _dbg("unwinding_records.h patché (Windows ARM64)")


# ── CMake build de la lib Dart ────────────────────────────────────────────

def cmake_dart(info: DartLibInfo, target_dir: str):
    """Configure et compile la lib statique Dart VM."""
    _require_tool(CMAKE_CMD)
    _require_tool(NINJA_CMD)

    if not os.path.isfile(CMAKE_TEMPLATE_FILE):
        raise BlutterBuildError(
            f"Template CMake introuvable : {CMAKE_TEMPLATE_FILE}\n"
            "  → Vérifiez l'intégrité du dépôt blutter."
        )
    if not os.path.isfile(CREATE_SRCLIST_FILE):
        raise BlutterBuildError(
            f"Script de liste de sources introuvable : {CREATE_SRCLIST_FILE}"
        )

    # Dart ≥3.11 requiert C++20
    try:
        major, minor = int(info.version.split(".")[0]), int(info.version.split(".")[1])
    except (ValueError, IndexError):
        major, minor = 3, 0
    cpp_std = "20" if (major, minor) >= (3, 11) else "17"

    # Écrit CMakeLists.txt dans le répertoire SDK cloné
    template = Path(CMAKE_TEMPLATE_FILE).read_text(encoding="utf-8")
    cmake_out = Path(target_dir) / "CMakeLists.txt"
    cmake_out.write_text(
        template
        .replace("VERSION_PLACE_HOLDER", info.version)
        .replace("STD_PLACE_HOLDER", cpp_std),
        encoding="utf-8",
    )

    # Config.cmake.in
    (Path(target_dir) / "Config.cmake.in").write_text(
        "@PACKAGE_INIT@\n\n"
        'include ( "${CMAKE_CURRENT_LIST_DIR}/dartvmTarget.cmake" )\n',
        encoding="utf-8",
    )

    # Génère sourcelist.cmake
    _run([sys.executable, CREATE_SRCLIST_FILE, target_dir])

    # cmake configure
    build_dir = os.path.join(BUILD_DIR, info.lib_name)
    Path(build_dir).mkdir(parents=True, exist_ok=True)

    _run(
        [CMAKE_CMD, "-GNinja", "-B", build_dir,
         f"-DTARGET_OS={info.os_name}",
         f"-DTARGET_ARCH={info.arch}",
         f"-DCOMPRESSED_PTRS={1 if info.has_compressed_ptrs else 0}",
         "-DCMAKE_BUILD_TYPE=Release",
         "--log-level=NOTICE"],
        cwd=target_dir,
    )

    # ninja build
    _run([NINJA_CMD], cwd=build_dir)

    # cmake install
    _run([CMAKE_CMD, "--install", "."], cwd=build_dir)
    _dbg(f"Lib Dart VM installée : {info.lib_name}")


# ── point d'entrée ────────────────────────────────────────────────────────

def fetch_and_build(info: DartLibInfo):
    """Clone le SDK Dart si absent et compile la lib VM statique."""
    Path(SDK_DIR).mkdir(parents=True, exist_ok=True)
    Path(BUILD_DIR).mkdir(parents=True, exist_ok=True)

    sdk_dir = checkout_dart(info)
    cmake_dart(info, sdk_dir)


# ── CLI standalone ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Télécharge et compile la Dart VM lib pour Blutter"
    )
    parser.add_argument("version",  help="Version Dart  ex: 3.4.2")
    parser.add_argument("os_name",  nargs="?", default="android",
                        help="OS cible  (android|ios)  [défaut: android]")
    parser.add_argument("arch",     nargs="?", default="arm64",
                        help="Architecture  (arm64|arm|x64)  [défaut: arm64]")
    parser.add_argument("snapshot_hash", nargs="?", default=None,
                        help="Hash du snapshot (optionnel)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        os.environ["BLUTTER_VERBOSE"] = "1"
        VERBOSE = True

    try:
        dart_info = DartLibInfo(
            args.version,
            args.os_name,
            args.arch,
            snapshot_hash=args.snapshot_hash,
        )
        print(f"  DartLibInfo : {dart_info}")
        fetch_and_build(dart_info)
        print("  Build terminé avec succès.")
    except BlutterBuildError as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)
