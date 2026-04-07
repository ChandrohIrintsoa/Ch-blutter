#!/usr/bin/env python3
"""
dartvm_fetch_build.py — Téléchargement et build de la lib Dart VM statique
pour Blutter.

Utilisation autonome :
  python dartvm_fetch_build.py 3.4.2
  python dartvm_fetch_build.py 3.4.2 android arm64
  python dartvm_fetch_build.py 3.4.2 android arm64 <snapshot_hash>
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

# ── Constantes ────────────────────────────────────────────────────────────────
VERBOSE = os.environ.get("BLUTTER_VERBOSE", "0") == "1"

GIT_CMD   = os.environ.get("GIT",   "git")
CMAKE_CMD = os.environ.get("CMAKE", "cmake")
NINJA_CMD = os.environ.get("NINJA", "ninja")

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# Ces scripts peuvent être dans scripts/ ou à la racine
def _find_script(name: str) -> str:
    for candidate in (
        os.path.join(SCRIPT_DIR, "scripts", name),
        os.path.join(SCRIPT_DIR, name),
    ):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(SCRIPT_DIR, name)  # chemin par défaut (peut échouer)

CMAKE_TEMPLATE_FILE = _find_script("CMakeLists.txt")
CREATE_SRCLIST_FILE = _find_script("dartvm_create_srclist.py")
MAKE_VERSION_FILE   = _find_script("dartvm_make_version.py")

SDK_DIR   = os.path.join(SCRIPT_DIR, "dartsdk")
BUILD_DIR = os.path.join(SCRIPT_DIR, "build")

DART_GIT_URL = "https://github.com/dart-lang/sdk.git"

# Patch Python 3.12 (remplacement du module `imp` supprimé)
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

# Groupes de compatibilité binaire : une lib compilée pour un patch
# peut être réutilisée pour tous les patches du même groupe.
DART_VERSION_GROUPS: dict[str, list[str]] = {
    "3.0_aa":  ["3.0.0", "3.0.1", "3.0.2"],
    "3.0_90":  ["3.0.3", "3.0.4", "3.0.5", "3.0.6", "3.0.7"],
    "3.1":     ["3.1.0", "3.1.1", "3.1.2", "3.1.3", "3.1.4", "3.1.5"],
    "3.2":     ["3.2.0", "3.2.1", "3.2.2", "3.2.3", "3.2.4", "3.2.5", "3.2.6"],
    "3.3":     ["3.3.0", "3.3.1", "3.3.2", "3.3.3", "3.3.4"],
    "3.4":     ["3.4.0", "3.4.1", "3.4.2", "3.4.3", "3.4.4"],
    "3.5":     ["3.5.0", "3.5.1", "3.5.2", "3.5.3", "3.5.4"],
    "3.6":     ["3.6.0", "3.6.1", "3.6.2"],
    "3.7":     ["3.7.0", "3.7.1", "3.7.2"],
    "3.8":     ["3.8.0", "3.8.1"],
    "3.9":     ["3.9.0", "3.9.1", "3.9.2"],
    "3.10":    ["3.10.0", "3.10.1", "3.10.2", "3.10.3", "3.10.4",
                "3.10.5", "3.10.6", "3.10.7", "3.10.8", "3.10.9"],
}

# ── Helpers ───────────────────────────────────────────────────────────────────

class BlutterBuildError(RuntimeError):
    """Erreur de build avec message explicite."""
    pass


def _dbg(msg: str):
    if VERBOSE:
        print(f"  [DBG] {msg}", file=sys.stderr)


def _require_tool(cmd: str):
    """Vérifie qu'un outil système est disponible."""
    if shutil.which(cmd) is None:
        raise BlutterBuildError(
            f"Outil requis introuvable : '{cmd}'\n"
            f"  → Sur Termux : pkg install {cmd}\n"
            f"  → Sur Linux  : sudo apt install {cmd}"
        )


def _run(
    args: list,
    cwd: str = None,
    check: bool = True,
    timeout: int = None,
    retries: int = 1,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Lance une sous-commande avec retry optionnel."""
    _dbg(f"run: {' '.join(str(a) for a in args)}")
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return subprocess.run(
                args,
                cwd=cwd,
                check=check,
                timeout=timeout,
                **kwargs,
            )
        except subprocess.TimeoutExpired as e:
            raise BlutterBuildError(
                f"Timeout ({timeout}s) dépassé : {' '.join(str(a) for a in args)}"
            ) from e
        except subprocess.CalledProcessError as e:
            last_exc = e
            if attempt < retries:
                delay = 2 ** attempt
                _dbg(f"Tentative {attempt}/{retries} échouée, retry dans {delay}s")
                time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise BlutterBuildError("Commande échouée sans exception (cas impossible)")


def _rmtree_robust(path: str):
    """Supprime un dossier même si des fichiers sont en lecture seule (Windows)."""
    def _on_error(func, fpath, _exc_info):
        try:
            os.chmod(fpath, stat.S_IWRITE)
            func(fpath)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_on_error)


# ── DartLibInfo ───────────────────────────────────────────────────────────────

class DartLibInfo:
    """
    Représente une configuration de lib Dart VM à compiler.

    Paramètres :
      version              – ex: "3.4.2"
      os_name              – "android" | "ios" | "linux" | "windows" | "macos"
      arch                 – "arm64" | "arm" | "x64" | "x86"
      has_compressed_ptrs  – True/False (None = déduction depuis os_name)
      snapshot_hash        – hash du snapshot 32 chars hex (optionnel)
    """

    VALID_OS   = {"android", "ios", "linux", "windows", "macos"}
    VALID_ARCH = {"arm64", "arm", "x64", "x86"}

    def __init__(
        self,
        version: str,
        os_name: str = "android",
        arch: str = "arm64",
        has_compressed_ptrs: Optional[bool] = None,
        snapshot_hash: Optional[str] = None,
    ):
        self._validate(version, os_name, arch, snapshot_hash)

        self.os_name       = os_name
        self.arch          = arch
        self.snapshot_hash = snapshot_hash

        # Pointeurs compressés : activé par défaut sauf iOS
        self.has_compressed_ptrs = (
            has_compressed_ptrs
            if has_compressed_ptrs is not None
            else (os_name != "ios")
        )

        # Résolution de groupe de compatibilité
        self.version  = self._resolve_version(version, os_name, arch)
        self.lib_name = f"dartvm{self.version}_{os_name}_{arch}"

    # ── Validation ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate(version: str, os_name: str, arch: str,
                  snapshot_hash: Optional[str]):
        if not version or not version[0].isdigit():
            raise BlutterBuildError(
                f"Version Dart invalide : '{version}'\n"
                "  Exemple valide : '3.4.2'"
            )
        if os_name not in DartLibInfo.VALID_OS:
            raise BlutterBuildError(
                f"os_name invalide : '{os_name}'\n"
                f"  Valides : {', '.join(sorted(DartLibInfo.VALID_OS))}"
            )
        if arch not in DartLibInfo.VALID_ARCH:
            raise BlutterBuildError(
                f"arch invalide : '{arch}'\n"
                f"  Valides : {', '.join(sorted(DartLibInfo.VALID_ARCH))}"
            )
        if snapshot_hash is not None:
            if len(snapshot_hash) != 32 or not all(
                c in "0123456789abcdefABCDEF" for c in snapshot_hash
            ):
                raise BlutterBuildError(
                    f"snapshot_hash invalide : '{snapshot_hash}'\n"
                    "  Attendu : 32 caractères hexadécimaux"
                )

    # ── Résolution de groupe de compatibilité ─────────────────────────────────

    @staticmethod
    def _resolve_version(version: str, os_name: str, arch: str) -> str:
        """
        Si une lib compilée compatible existe déjà dans bin/, retourne sa version.
        Sinon, retourne la version demandée.
        """
        bin_dir = os.path.join(SCRIPT_DIR, "bin")
        if not os.path.isdir(bin_dir):
            return version

        # Collecte les versions déjà compilées pour cet OS/arch
        compiled: set[str] = set()
        prefix = "blutter_dartvm"
        suffix_os_arch = f"_{os_name}_{arch}"
        for fname in os.listdir(bin_dir):
            if not fname.startswith(prefix):
                continue
            rest = fname[len(prefix):]
            # rest = VERSION_os_arch[_suffixes...]
            if suffix_os_arch not in rest:
                continue
            ver_end = rest.find(suffix_os_arch)
            if ver_end > 0:
                compiled.add(rest[:ver_end])

        # Vérifie si une version compatible est dans le même groupe
        for _group, versions_in_group in DART_VERSION_GROUPS.items():
            if version in versions_in_group:
                for cv in compiled:
                    if cv in versions_in_group:
                        _dbg(
                            f"Lib compatible trouvée : {cv} "
                            f"(demandé : {version}, groupe : {_group})"
                        )
                        return cv

        return version

    # ── Constructeur depuis chaîne "VER_OS_ARCH" ──────────────────────────────

    @classmethod
    def from_string(cls, s: str, **kwargs) -> "DartLibInfo":
        """
        Construit un DartLibInfo depuis "3.4.2_android_arm64".
        Lève BlutterBuildError si le format est incorrect.
        """
        parts = s.strip().split("_")
        if len(parts) < 3:
            raise BlutterBuildError(
                f"Format --dart-version invalide : '{s}'\n"
                "  Attendu : VERSION_OS_ARCH  ex: 3.4.2_android_arm64"
            )
        version, os_name, arch = parts[0], parts[1], parts[2]
        return cls(version, os_name, arch, **kwargs)

    def __repr__(self) -> str:
        return (
            f"DartLibInfo(version={self.version!r}, os={self.os_name!r}, "
            f"arch={self.arch!r}, compressed_ptrs={self.has_compressed_ptrs})"
        )


# ── Clone du SDK Dart ──────────────────────────────────────────────────────────

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
        print(f"  Clone incomplet détecté — nettoyage de {clone_dir}…")
        _rmtree_robust(clone_dir)

    if os.path.isdir(clone_dir):
        _dbg(f"SDK Dart {info.version} déjà cloné dans {clone_dir}")
        return clone_dir

    print(f"  Clonage du SDK Dart {info.version}…")
    Path(clone_dir).parent.mkdir(parents=True, exist_ok=True)

    # Clone sparse pour limiter la bande passante
    _run(
        [GIT_CMD, "-c", "advice.detachedHead=false",
         "clone", "-b", info.version,
         "--depth", "1", "--filter=blob:none",
         "--sparse", "--progress",
         DART_GIT_URL, clone_dir],
        retries=git_retries,
        timeout=600,
    )

    # Checkout sparse : seulement les sources nécessaires
    _run(
        [GIT_CMD, "sparse-checkout", "set",
         "runtime", "tools", "third_party/double-conversion"],
        cwd=clone_dir,
    )

    # Supprimer les fichiers racine superflus (pas les dossiers)
    for entry in os.scandir(clone_dir):
        if entry.is_file():
            try:
                os.remove(entry.path)
            except OSError:
                pass

    # Générer version.cc
    if info.snapshot_hash is None:
        _patch_python312(clone_dir)
        _make_version_official(clone_dir)
    else:
        _make_version_custom(clone_dir, info.snapshot_hash)

    # Patch Windows ARM64 (Dart ≥ 3.8)
    if sys.platform == "win32":
        _patch_win32_arm64(clone_dir, info.version)

    print(f"  SDK Dart {info.version} prêt dans {clone_dir}")
    return clone_dir


# ── Patches ───────────────────────────────────────────────────────────────────

def _patch_python312(clone_dir: str):
    """Corrige tools/utils.py pour Python 3.12 (suppression du module imp)."""
    if sys.version_info < (3, 12):
        return

    utils_path = os.path.join(clone_dir, "tools", "utils.py")
    if not os.path.isfile(utils_path):
        return

    with open(utils_path, "r+", encoding="utf-8") as f:
        content = f.read()
        if "import importlib.util" in content:
            return  # déjà patché

        patched = content

        # Corrige les chaînes d'échappement invalides (SyntaxWarning → SyntaxError 3.12)
        for old, new in [
            (" ' awk ", " r' awk "),
            ("match_against('", "match_against(r'"),
            ("re.search('", "re.search(r'"),
        ]:
            patched = patched.replace(old, new)

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
    """Génère runtime/vm/version.cc via l'outil officiel du SDK."""
    _run(
        [sys.executable, "tools/make_version.py",
         "--output", "runtime/vm/version.cc",
         "--input",  "runtime/vm/version_in.cc"],
        cwd=clone_dir,
        timeout=60,
    )
    _dbg("version.cc généré (officiel)")


def _make_version_custom(clone_dir: str, snapshot_hash: str):
    """Génère runtime/vm/version.cc avec un snapshot hash personnalisé."""
    make_ver = _find_script("dartvm_make_version.py")
    if not os.path.isfile(make_ver):
        raise BlutterBuildError(
            f"Script introuvable : {make_ver}\n"
            "  Vérifiez l'intégrité du dépôt."
        )
    _run(
        [sys.executable, make_ver, clone_dir, snapshot_hash],
        timeout=60,
    )
    _dbg(f"version.cc généré (snapshot_hash={snapshot_hash[:8]}…)")


def _patch_win32_arm64(clone_dir: str, version: str):
    """
    Depuis Dart 3.8, RUNTIME_FUNCTION est déclaré pour Windows+ARM64.
    Ce patch commente la ligne incriminée dans unwinding_records.h.
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
        try:
            target = b"\n#if !defined(DART_HOST_OS_WINDOWS) || !defined(HOST_ARCH_ARM64)"
            pos = mm.find(target)
            if pos != -1:
                mm[pos + 36: pos + 38] = b"//"
            else:
                target2 = b"\nstatic_assert(sizeof("
                pos = mm.find(target2)
                if pos != -1:
                    mm[pos + 1: pos + 3] = b"//"
        finally:
            mm.close()
    _dbg("unwinding_records.h patché (Windows ARM64)")


# ── Build CMake ────────────────────────────────────────────────────────────────

def cmake_dart(info: DartLibInfo, target_dir: str):
    """Configure et compile la lib statique Dart VM via CMake/Ninja."""
    _require_tool(CMAKE_CMD)
    _require_tool(NINJA_CMD)

    cmake_tmpl = _find_script("CMakeLists.txt")
    create_src = _find_script("dartvm_create_srclist.py")

    if not os.path.isfile(cmake_tmpl):
        raise BlutterBuildError(
            f"Template CMakeLists.txt introuvable : {cmake_tmpl}\n"
            "  Vérifiez l'intégrité du dépôt."
        )
    if not os.path.isfile(create_src):
        raise BlutterBuildError(
            f"Script dartvm_create_srclist.py introuvable : {create_src}"
        )

    # Dart ≥ 3.11 requiert C++20
    try:
        major, minor = int(info.version.split(".")[0]), int(info.version.split(".")[1])
    except (ValueError, IndexError):
        major, minor = 3, 0
    cpp_std = "20" if (major, minor) >= (3, 11) else "17"

    # Écrit CMakeLists.txt dans le SDK cloné
    template = Path(cmake_tmpl).read_text(encoding="utf-8")
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
        'include("${CMAKE_CURRENT_LIST_DIR}/dartvmTarget.cmake")\n',
        encoding="utf-8",
    )

    # Génère sourcelist.cmake
    _run([sys.executable, create_src, target_dir], timeout=120)

    # cmake configure
    build_subdir = os.path.join(BUILD_DIR, info.lib_name)
    Path(build_subdir).mkdir(parents=True, exist_ok=True)

    _run(
        [CMAKE_CMD, "-GNinja", "-B", build_subdir,
         f"-DTARGET_OS={info.os_name}",
         f"-DTARGET_ARCH={info.arch}",
         f"-DCOMPRESSED_PTRS={1 if info.has_compressed_ptrs else 0}",
         "-DCMAKE_BUILD_TYPE=Release",
         "--log-level=NOTICE"],
        cwd=target_dir,
        timeout=120,
    )

    # ninja build
    cpu_count = os.cpu_count() or 2
    _run([NINJA_CMD, f"-j{cpu_count}"], cwd=build_subdir, timeout=3600)

    # cmake install
    _run([CMAKE_CMD, "--install", "."], cwd=build_subdir, timeout=120)
    _dbg(f"Lib Dart VM installée : {info.lib_name}")
    print(f"  Lib compilée avec succès : {info.lib_name}")


# ── Point d'entrée ────────────────────────────────────────────────────────────

def fetch_and_build(info: DartLibInfo):
    """Clone le SDK Dart si absent et compile la lib VM statique."""
    Path(SDK_DIR).mkdir(parents=True, exist_ok=True)
    Path(BUILD_DIR).mkdir(parents=True, exist_ok=True)
    sdk_dir = checkout_dart(info)
    cmake_dart(info, sdk_dir)


# ── CLI autonome ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Télécharge et compile la Dart VM lib pour Blutter"
    )
    parser.add_argument("version",
                        help="Version Dart  ex: 3.4.2  ou  3.4.2_android_arm64")
    parser.add_argument("os_name",  nargs="?", default="android",
                        help="OS cible  (android|ios)  [défaut: android]")
    parser.add_argument("arch",     nargs="?", default="arm64",
                        help="Architecture  (arm64|arm|x64)  [défaut: arm64]")
    parser.add_argument("snapshot_hash", nargs="?", default=None,
                        help="Hash du snapshot 32 hex chars (optionnel)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        os.environ["BLUTTER_VERBOSE"] = "1"
        VERBOSE = True

    try:
        # Support du format condensé "3.4.2_android_arm64"
        if "_" in args.version and args.os_name == "android" and args.arch == "arm64":
            dart_info = DartLibInfo.from_string(
                args.version, snapshot_hash=args.snapshot_hash
            )
        else:
            dart_info = DartLibInfo(
                args.version, args.os_name, args.arch,
                snapshot_hash=args.snapshot_hash,
            )
        print(f"  DartLibInfo : {dart_info}")
        fetch_and_build(dart_info)
        print("  Build terminé avec succès.")
    except BlutterBuildError as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Interruption — arrêt.")
        sys.exit(130)
