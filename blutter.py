#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║  B(L)UTTER  ·  Flutter Reverse Engineering Tool     ║
║  Termux/Linux Native  ·  ARM64  ·  Dart VM          ║
╚══════════════════════════════════════════════════════╝

Usage:
  python blutter.py <input> <outdir> [options]
  python blutter.py                          # TUI interactif

Arguments:
  input     APK, dossier arm64-v8a, ou libapp.so direct
  outdir    Dossier de sortie (créé si inexistant)

Options:
  --dart-version VER   Version Dart ex: 3.4.2_android_arm64
  --rebuild            Force recompilation de l'exécutable
  --no-analysis        Désactive l'analyse Dart
  --ida-fcn            Génère noms de fonctions pour IDA Pro
  --no-update          Ne pas vérifier les mises à jour git
  --debug              Affiche les tracebacks complets
  --check-deps         Vérifie les dépendances et quitte
  --history            Affiche les dernières analyses
"""

import argparse
import atexit
import glob
import hashlib
import json
import mmap
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path


# ─────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────
VERSION      = "4.0.0"
SCRIPT_DIR   = os.path.dirname(os.path.realpath(__file__))
BIN_DIR      = os.path.join(SCRIPT_DIR, "bin")
PKG_INC_DIR  = os.path.join(SCRIPT_DIR, "packages", "include")
PKG_LIB_DIR  = os.path.join(SCRIPT_DIR, "packages", "lib")
BUILD_DIR    = os.path.join(SCRIPT_DIR, "build")
HISTORY_FILE = os.path.expanduser("~/.blutter_history")
LOCK_FILE    = os.path.join(tempfile.gettempdir(), "blutter.lock")

CMAKE_CMD    = "cmake"
NINJA_CMD    = "ninja"

ARM_ARCH_DIRS       = ["arm64-v8a", "armeabi-v7a", "armeabi"]
FLUTTER_LIB_NAMES   = ["libflutter.so", "Flutter", "libFlutter.so"]
APP_LIB_KNOWN_NAMES = ["libapp.so", "App", "libApp.so"]

DEBUG_MODE   = False
SESSION_START = time.time()

IS_TERMUX = os.path.exists("/data/data/com.termux") or \
            "com.termux" in os.environ.get("PREFIX", "")


# ─────────────────────────────────────────────────────────────
#  Couleurs ANSI (pas de dépendance Rich)
# ─────────────────────────────────────────────────────────────
def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()

USE_COLOR = _supports_color()

class C:
    """Codes ANSI couleur."""
    RESET  = "\033[0m"   if USE_COLOR else ""
    BOLD   = "\033[1m"   if USE_COLOR else ""
    DIM    = "\033[2m"   if USE_COLOR else ""
    GREEN  = "\033[32m"  if USE_COLOR else ""
    BGREEN = "\033[92m"  if USE_COLOR else ""
    CYAN   = "\033[36m"  if USE_COLOR else ""
    BCYAN  = "\033[96m"  if USE_COLOR else ""
    YELLOW = "\033[33m"  if USE_COLOR else ""
    RED    = "\033[31m"  if USE_COLOR else ""
    BRED   = "\033[91m"  if USE_COLOR else ""
    WHITE  = "\033[97m"  if USE_COLOR else ""
    MAG    = "\033[35m"  if USE_COLOR else ""


def _strip_ansi(text: str) -> str:
    return re.sub(r'\033\[[0-9;]*m', '', text)


# ─────────────────────────────────────────────────────────────
#  Logger
# ─────────────────────────────────────────────────────────────
def log_ok(msg: str):
    print(f"{C.BGREEN}  ✔  {C.RESET}{C.GREEN}{msg}{C.RESET}")

def log_info(msg: str):
    print(f"{C.CYAN}  ◈  {C.RESET}{msg}")

def log_warn(msg: str):
    print(f"{C.YELLOW}  ⚠  {C.RESET}{C.YELLOW}{msg}{C.RESET}", file=sys.stderr)

def log_err(msg: str):
    print(f"{C.BRED}  ✘  {C.RESET}{C.RED}{msg}{C.RESET}", file=sys.stderr)

def log_dbg(msg: str):
    if DEBUG_MODE:
        print(f"{C.DIM}{C.MAG}  ⬡ DBG  {C.RESET}{C.DIM}{msg}{C.RESET}")

def log_section(title: str):
    width = min(shutil.get_terminal_size((80, 24)).columns, 80)
    line  = "─" * width
    print(f"\n{C.CYAN}{line}{C.RESET}")
    print(f"{C.BCYAN}  {title}{C.RESET}")
    print(f"{C.CYAN}{line}{C.RESET}")


# ─────────────────────────────────────────────────────────────
#  Bannière
# ─────────────────────────────────────────────────────────────
BANNER = r"""
  ██████╗ ██╗     ██╗   ██╗████████╗████████╗███████╗██████╗
  ██╔══██╗██║     ██║   ██║╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
  ██████╔╝██║     ██║   ██║   ██║      ██║   █████╗  ██████╔╝
  ██╔══██╗██║     ██║   ██║   ██║      ██║   ██╔══╝  ██╔══██╗
  ██████╔╝███████╗╚██████╔╝   ██║      ██║   ███████╗██║  ██║
  ╚═════╝ ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝
"""

def print_banner():
    env_label = "Termux" if IS_TERMUX else platform.system()
    print(f"{C.BGREEN}{BANNER}{C.RESET}")
    print(f"  {C.CYAN}Flutter Reverse Engineering Tool{C.RESET}  "
          f"{C.DIM}v{VERSION}{C.RESET}")
    print(f"  {C.DIM}ARM64 · Dart VM · {env_label} · "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M')}{C.RESET}")
    print()


# ─────────────────────────────────────────────────────────────
#  Spinner léger (thread)
# ─────────────────────────────────────────────────────────────
class Spinner:
    """Spinner minimal sans dépendances externes."""
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, label: str):
        self.label   = label
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r{C.CYAN}  {frame}  {self.label}…{C.RESET}", end="", flush=True)
            time.sleep(0.08)
            i += 1

    def __enter__(self):
        if sys.stdout.isatty():
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.3)
        if sys.stdout.isatty():
            print(f"\r{' ' * (len(self.label) + 8)}\r", end="", flush=True)


# ─────────────────────────────────────────────────────────────
#  Verrou de session (évite les doublons)
# ─────────────────────────────────────────────────────────────
def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            pid = Path(LOCK_FILE).read_text().strip()
            log_warn(f"Instance Blutter déjà en cours (PID {pid}) ?")
            rep = input("  Ignorer le verrou et continuer ? [o/N] : ").strip().lower()
            if rep not in ("o", "oui", "y", "yes"):
                sys.exit(1)
        except (OSError, EOFError):
            pass
    try:
        Path(LOCK_FILE).write_text(str(os.getpid()))
    except OSError:
        pass

def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass

atexit.register(_release_lock)


# ─────────────────────────────────────────────────────────────
#  Signal Ctrl+C
# ─────────────────────────────────────────────────────────────
def _sigint_handler(sig, frame):
    elapsed = _fmt_elapsed()
    print(f"\n\n{C.BRED}  ⚡  Interruption — session avortée.{C.RESET}")
    print(f"{C.DIM}  Durée : {elapsed}{C.RESET}\n")
    _release_lock()
    sys.exit(130)

signal.signal(signal.SIGINT, _sigint_handler)


def _fmt_elapsed() -> str:
    secs = int(time.time() - SESSION_START)
    m, s = divmod(secs, 60)
    return f"{m:02d}:{s:02d}"


# ─────────────────────────────────────────────────────────────
#  Vérification des dépendances
# ─────────────────────────────────────────────────────────────
REQUIRED_BINS = {
    "cmake":   "cmake  (build system)  → pkg install cmake",
    "ninja":   "ninja  (build tool)    → pkg install ninja",
    "git":     "git    (vcs)           → pkg install git",
    "python3": "python3                → pkg install python",
}

OPTIONAL_BINS = {
    "pkg-config": "pkg-config          → pkg install pkg-config",
}

REQUIRED_PY = {
    "pyelftools": "pyelftools           → pip install pyelftools",
    "requests":   "requests             → pip install requests",
}

def check_dependencies(strict: bool = True) -> bool:
    log_section("VÉRIFICATION DES DÉPENDANCES")
    ok_all = True

    def check_bin(cmd, label, required=True):
        nonlocal ok_all
        found = shutil.which(cmd) is not None
        state = f"{C.BGREEN}OK{C.RESET}" if found else f"{C.BRED}MANQUANT{C.RESET}"
        print(f"  {state}  {label}")
        if not found and required:
            ok_all = False
        return found

    def check_py(mod, label):
        nonlocal ok_all
        try:
            __import__(mod)
            print(f"  {C.BGREEN}OK{C.RESET}  {label}")
            return True
        except ImportError:
            print(f"  {C.BRED}MANQUANT{C.RESET}  {label}")
            ok_all = False
            return False

    print(f"\n{C.BOLD}Outils système :{C.RESET}")
    for cmd, lbl in REQUIRED_BINS.items():
        check_bin(cmd, lbl)
    for cmd, lbl in OPTIONAL_BINS.items():
        check_bin(cmd, lbl, required=False)

    print(f"\n{C.BOLD}Modules Python :{C.RESET}")
    for mod, lbl in REQUIRED_PY.items():
        check_py(mod, lbl)

    # capstone via pkg-config (optionnel mais utile)
    if shutil.which("pkg-config"):
        r = subprocess.run(["pkg-config", "--exists", "capstone"],
                           capture_output=True)
        if r.returncode == 0:
            print(f"  {C.BGREEN}OK{C.RESET}  capstone  (pkg-config)")
        else:
            print(f"  {C.YELLOW}AVERTISSEMENT{C.RESET}  capstone introuvable via pkg-config")
            print(f"    → pkg install capstone")

    if ok_all:
        print(f"\n{C.BGREEN}  Toutes les dépendances sont présentes.{C.RESET}")
    else:
        print(f"\n{C.YELLOW}  Des dépendances manquantes peuvent bloquer la compilation.{C.RESET}")
        if IS_TERMUX:
            print(f"\n  {C.CYAN}Commande Termux tout-en-un :{C.RESET}")
            print(f"  {C.DIM}pip install requests pyelftools && "
                  f"pkg install -y git cmake ninja build-essential "
                  f"pkg-config libicu capstone fmt{C.RESET}")

    return ok_all


# ─────────────────────────────────────────────────────────────
#  Historique des analyses
# ─────────────────────────────────────────────────────────────
def _load_history() -> list:
    try:
        return json.loads(Path(HISTORY_FILE).read_text())
    except Exception:
        return []

def _save_history(entry: dict):
    hist = _load_history()
    hist.insert(0, entry)
    hist = hist[:30]
    try:
        Path(HISTORY_FILE).write_text(json.dumps(hist, indent=2))
    except Exception:
        pass

def show_history():
    hist = _load_history()
    if not hist:
        log_info("Aucun historique disponible.")
        return
    log_section("HISTORIQUE DES ANALYSES")
    print(f"  {'#':<3}  {'Date':<20}  {'Cible':<35}  {'Dart':<15}  {'Statut'}")
    print(f"  {'─'*3}  {'─'*20}  {'─'*35}  {'─'*15}  {'─'*7}")
    for i, e in enumerate(hist, 1):
        status  = f"{C.BGREEN}OK{C.RESET}" if e.get("success") else f"{C.BRED}ÉCHEC{C.RESET}"
        cible   = e.get("indir", "?")[-35:]
        print(f"  {i:<3}  {e.get('date','?'):<20}  {cible:<35}  "
              f"{e.get('dart_version','?'):<15}  {status}")


# ─────────────────────────────────────────────────────────────
#  Analyse APK (informations)
# ─────────────────────────────────────────────────────────────
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def print_apk_info(apk_path: str):
    log_section("ANALYSE APK")
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            names    = zf.namelist()
            so_files = [n for n in names if n.endswith(".so")]
            dex_files = [n for n in names if n.endswith(".dex")]
            total_sz = sum(i.file_size for i in zf.infolist())

        fsize = os.path.getsize(apk_path)
        sha   = _sha256(apk_path)

        print(f"  {C.CYAN}Fichier   {C.RESET}: {os.path.basename(apk_path)}")
        print(f"  {C.CYAN}Taille    {C.RESET}: {fsize/1024/1024:.2f} MB  "
              f"(décompressé : {total_sz/1024/1024:.2f} MB)")
        print(f"  {C.CYAN}SHA-256   {C.RESET}: {sha[:16]}…")
        print(f"  {C.CYAN}Libs .so  {C.RESET}: {len(so_files)}")
        print(f"  {C.CYAN}Fichiers  {C.RESET}: {len(names)} ZIP entries, {len(dex_files)} DEX")

        for arch in ARM_ARCH_DIRS:
            arch_libs = [n for n in so_files if f"lib/{arch}/" in n]
            if arch_libs:
                print(f"\n  {C.BOLD}{arch}{C.RESET}")
                for lib in arch_libs:
                    print(f"    {C.DIM}◈{C.RESET}  {os.path.basename(lib)}")
    except zipfile.BadZipFile:
        log_err(f"APK corrompu ou invalide : {apk_path}")
    except Exception as e:
        log_warn(f"Analyse APK partielle : {e}")
        log_dbg(str(e))


# ─────────────────────────────────────────────────────────────
#  Recherche des bibliothèques
# ─────────────────────────────────────────────────────────────
def _find_app_lib(directory: str):
    for name in APP_LIB_KNOWN_NAMES:
        p = os.path.join(directory, name)
        if os.path.isfile(p):
            return os.path.abspath(p)
    flutter_lower = {n.lower() for n in FLUTTER_LIB_NAMES}
    try:
        for f in os.listdir(directory):
            if f.lower().endswith(".so") and f.lower() not in flutter_lower:
                return os.path.abspath(os.path.join(directory, f))
    except OSError:
        pass
    return None

def _find_flutter_lib(directory: str):
    for name in FLUTTER_LIB_NAMES:
        p = os.path.join(directory, name)
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None

def find_lib_files(indir: str):
    """
    Cherche libapp.so + libflutter.so dans `indir` et ses sous-dossiers ARM.
    Retourne (app_path, flutter_path) ou quitte avec un message clair.
    """
    # 1. Dossier direct
    app     = _find_app_lib(indir)
    flutter = _find_flutter_lib(indir)
    if app and flutter:
        return app, flutter

    # 2. Sous-dossiers ARM connus
    search_dirs = []
    for arch in ARM_ARCH_DIRS:
        search_dirs.append(os.path.join(indir, arch))
        search_dirs.append(os.path.join(indir, "lib", arch))

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        app     = _find_app_lib(d)
        flutter = _find_flutter_lib(d)
        if app and flutter:
            arch_name = os.path.basename(d)
            log_ok(f"Libs trouvées dans : {arch_name}")
            log_info(f"App     → {os.path.basename(app)}")
            log_info(f"Flutter → {os.path.basename(flutter)}")
            return app, flutter

    # Diagnostique précis
    if not app:
        log_err(
            "libapp.so introuvable.\n"
            f"       Dossier analysé : {indir}\n"
            f"       Archs testées   : {', '.join(ARM_ARCH_DIRS)}"
        )
    else:
        log_err(
            f"libflutter.so introuvable.\n"
            f"       Noms cherchés : {', '.join(FLUTTER_LIB_NAMES)}"
        )
    sys.exit(1)


def extract_libs_from_apk(apk_path: str, tmp_dir: str):
    """Extrait libapp.so + libflutter.so depuis un APK."""
    flutter_lower = {n.lower() for n in FLUTTER_LIB_NAMES}

    with zipfile.ZipFile(apk_path, "r") as zf:
        names = zf.namelist()

        for arch in ARM_ARCH_DIRS:
            prefix   = f"lib/{arch}/"
            # Flutter
            fl_info  = None
            for fn in FLUTTER_LIB_NAMES:
                if prefix + fn in names:
                    fl_info = zf.getinfo(prefix + fn)
                    break
            if not fl_info:
                continue

            # App
            app_info = None
            for an in APP_LIB_KNOWN_NAMES:
                if prefix + an in names:
                    app_info = zf.getinfo(prefix + an)
                    break
            if not app_info:
                for entry in names:
                    if (entry.startswith(prefix) and entry.endswith(".so")
                            and os.path.basename(entry).lower() not in flutter_lower):
                        app_info = zf.getinfo(entry)
                        break

            if app_info:
                log_ok(f"APK : extraction depuis {arch}")
                log_info(f"App     → {os.path.basename(app_info.filename)}")
                log_info(f"Flutter → {os.path.basename(fl_info.filename)}")
                zf.extract(app_info, tmp_dir)
                zf.extract(fl_info,  tmp_dir)
                return (
                    os.path.join(tmp_dir, app_info.filename),
                    os.path.join(tmp_dir, fl_info.filename),
                )

    log_err(
        "Bibliothèques introuvables dans l'APK.\n"
        f"       Archs testées : {', '.join(ARM_ARCH_DIRS)}"
    )
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
#  DartLibInfo (importé de dartvm_fetch_build ou défini ici)
# ─────────────────────────────────────────────────────────────
try:
    from dartvm_fetch_build import DartLibInfo
    log_dbg("DartLibInfo importé depuis dartvm_fetch_build")
except ImportError:
    log_dbg("dartvm_fetch_build introuvable — DartLibInfo minimal activé")

    class DartLibInfo:
        def __init__(self, version, os_name, arch,
                     has_compressed_ptrs=None, snapshot_hash=None):
            self.version         = version
            self.os_name         = os_name
            self.arch            = arch
            self.snapshot_hash   = snapshot_hash
            self.has_compressed_ptrs = (
                has_compressed_ptrs if has_compressed_ptrs is not None
                else (os_name != "ios")
            )
            self.lib_name = f"dartvm{version}_{os_name}_{arch}"


# ─────────────────────────────────────────────────────────────
#  BlutterInput
# ─────────────────────────────────────────────────────────────
class BlutterInput:
    def __init__(self, libapp_path, dart_info, outdir,
                 rebuild, create_vs_sln, no_analysis, ida_fcn):
        self.libapp_path     = libapp_path
        self.dart_info       = dart_info
        self.outdir          = outdir
        self.rebuild         = rebuild
        self.create_vs_sln   = create_vs_sln
        self.ida_fcn         = ida_fcn

        # Force no-analysis pour Dart <2.15
        vers = dart_info.version.split(".", 2)
        if int(vers[0]) == 2 and int(vers[1]) < 15:
            if not no_analysis:
                log_warn("Dart <2.15 → force --no-analysis")
            no_analysis = True
        self.no_analysis = no_analysis

        # Suffixe d'exécutable
        suffix = ""
        if not dart_info.has_compressed_ptrs:
            suffix += "_no-compressed-ptrs"
        if no_analysis:
            suffix += "_no-analysis"
        if ida_fcn:
            suffix += "_ida-fcn"

        self.name_suffix  = suffix
        self.blutter_name = f"blutter_{dart_info.lib_name}{suffix}"
        self.blutter_file = os.path.join(BIN_DIR, self.blutter_name) + (
            ".exe" if os.name == "nt" else ""
        )


# ─────────────────────────────────────────────────────────────
#  Macros de compatibilité CMake
# ─────────────────────────────────────────────────────────────
def find_compat_macros(dart_version: str, no_analysis: bool, ida_fcn: bool) -> list:
    macros      = []
    include_dir = os.path.join(PKG_INC_DIR, f"dartvm{dart_version}")
    vm_dir      = os.path.join(include_dir, "vm")

    def _scan(filename, checks):
        path = os.path.join(vm_dir, filename)
        try:
            with open(path, "rb") as f:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                for needle, macro in checks:
                    if isinstance(needle, bytes):
                        found = mm.find(needle) != -1
                    else:
                        found = needle(mm)
                    if found:
                        macros.append(macro)
                mm.close()
        except FileNotFoundError:
            log_warn(f"Fichier header introuvable : {path}")
        except Exception as e:
            log_warn(f"Lecture header échouée ({filename}) : {e}")

    _scan("class_id.h", [
        (b"V(LinkedHashMap)",         "-DOLD_MAP_SET_NAME=1"),
        (lambda mm: mm.find(b"V(LinkedHashMap)") != -1
                    and mm.find(b"V(ImmutableLinkedHashMap)") == -1,
         "-DOLD_MAP_NO_IMMUTABLE=1"),
        (lambda mm: mm.find(b" kLastInternalOnlyCid ") == -1,
         "-DNO_LAST_INTERNAL_ONLY_CID=1"),
        (b"V(TypeRef)",               "-DHAS_TYPE_REF=1"),
    ])

    # RecordType uniquement pour Dart 3.x
    major, *_ = dart_version.split(".")
    if int(major) >= 3:
        _scan("class_id.h", [(b"V(RecordType)", "-DHAS_RECORD_TYPE=1")])

    _scan("class_table.h",    [(b"class SharedClassTable {", "-DHAS_SHARED_CLASS_TABLE=1")])
    _scan("stub_code_list.h", [(lambda mm: mm.find(b"V(InitLateStaticField)") == -1,
                                 "-DNO_INIT_LATE_STATIC_FIELD=1")])
    _scan("object_store.h",   [(lambda mm: mm.find(b"build_generic_method_extractor_code)") == -1,
                                 "-DNO_METHOD_EXTRACTOR_STUB=1")])
    _scan("object.h",         [(lambda mm: mm.find(b"AsTruncatedInt64Value()") == -1,
                                 "-DUNIFORM_INTEGER_ACCESS=1")])

    if no_analysis:
        macros.append("-DNO_CODE_ANALYSIS=1")
    if ida_fcn:
        macros.append("-DIDA_FCN=1")

    # Dart ≥3.5
    try:
        maj, min_, *_ = dart_version.split(".")
        if (int(maj) > 3) or (int(maj) == 3 and int(min_) >= 5):
            macros.append("-DOLD_MARKING_STACK_BLOCK=1")
    except ValueError:
        pass

    # Dédoublonnage (l'ordre est important → on garde la première occurrence)
    seen, result = set(), []
    for m in macros:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


# ─────────────────────────────────────────────────────────────
#  CMake / Ninja build
# ─────────────────────────────────────────────────────────────
def _run(cmd: list, label: str, timeout: int = None, **kwargs) -> subprocess.CompletedProcess:
    """Lance une commande avec spinner et gestion d'erreur propre."""
    log_dbg(f"Commande : {' '.join(str(c) for c in cmd)}")
    with Spinner(label):
        try:
            result = subprocess.run(
                cmd,
                timeout=timeout,
                capture_output=not DEBUG_MODE,
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            log_err(f"Timeout ({timeout}s) dépassé : {label}")
            raise
        except FileNotFoundError:
            log_err(f"Commande introuvable : {cmd[0]}")
            raise

    if result.returncode != 0:
        log_err(f"Échec ({result.returncode}) : {label}")
        if not DEBUG_MODE and result.stderr:
            # Affiche les dernières lignes d'erreur
            lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            for line in lines[-10:]:
                print(f"    {C.RED}{line}{C.RESET}", file=sys.stderr)
        result.check_returncode()   # lève CalledProcessError
    return result


def cmake_build(inp: BlutterInput):
    """Configure et compile Blutter avec CMake/Ninja."""
    blutter_src = os.path.join(SCRIPT_DIR, "blutter")
    build_dir   = os.path.join(BUILD_DIR, inp.blutter_name)
    macros      = find_compat_macros(
        inp.dart_info.version, inp.no_analysis, inp.ida_fcn
    )

    # Sur macOS on peut avoir besoin de pointer vers clang 16
    extra_env = None
    if platform.system() == "Darwin":
        mac_major = int(platform.mac_ver()[0].split(".")[0])
        if mac_major < 15:
            try:
                prefix = subprocess.run(
                    ["brew", "--prefix", "llvm@16"],
                    capture_output=True, check=True
                ).stdout.decode().strip()
                clang = os.path.join(prefix, "bin", "clang")
                extra_env = {**os.environ, "CC": clang, "CXX": clang + "++"}
            except Exception:
                pass

    cmake_conf = [
        CMAKE_CMD, "-GNinja", "-B", build_dir,
        f"-DDARTLIB={inp.dart_info.lib_name}",
        f"-DNAME_SUFFIX={inp.name_suffix}",
        "-DCMAKE_BUILD_TYPE=Release",
        "--log-level=NOTICE",
    ] + macros

    log_info("CMake configure…")
    _run(cmake_conf, "CMake configure", cwd=blutter_src, env=extra_env)

    log_info("Ninja build…")
    _run([NINJA_CMD], "Ninja build", cwd=build_dir)

    log_info("CMake install…")
    _run([CMAKE_CMD, "--install", "."], "CMake install", cwd=build_dir)
    log_ok("Build terminé !")


# ─────────────────────────────────────────────────────────────
#  Infos Dart depuis les .so
# ─────────────────────────────────────────────────────────────
def get_dart_lib_info(libapp: str, libflutter: str) -> DartLibInfo:
    try:
        from extract_dart_info import extract_dart_info
    except ImportError:
        log_err(
            "extract_dart_info.py introuvable.\n"
            "       Ce fichier doit être dans le même dossier que blutter.py."
        )
        sys.exit(1)

    dart_version, snapshot_hash, flags, arch, os_name = extract_dart_info(
        libapp, libflutter
    )

    log_section("DART INFO")
    print(f"  {C.CYAN}Version   {C.RESET}: {dart_version}")
    print(f"  {C.CYAN}Snapshot  {C.RESET}: {snapshot_hash}")
    print(f"  {C.CYAN}Cible     {C.RESET}: {os_name} / {arch}")
    print(f"  {C.CYAN}Flags     {C.RESET}: {' '.join(flags) or 'none'}")

    has_compressed = "compressed-pointers" in flags
    return DartLibInfo(dart_version, os_name, arch, has_compressed, snapshot_hash)


# ─────────────────────────────────────────────────────────────
#  Build + Run
# ─────────────────────────────────────────────────────────────
def build_and_run(inp: BlutterInput):
    # Vérifier si la lib Dart est disponible
    lib_ext  = ".lib" if os.name == "nt" else ".a"
    lib_prefix = "" if os.name == "nt" else "lib"
    dart_lib = os.path.join(
        PKG_LIB_DIR, f"{lib_prefix}{inp.dart_info.lib_name}{lib_ext}"
    )

    if not os.path.isfile(dart_lib):
        log_info("Téléchargement & compilation de la Dart VM lib…")
        try:
            from dartvm_fetch_build import fetch_and_build
            with Spinner("fetch & build Dart VM"):
                fetch_and_build(inp.dart_info)
        except ImportError:
            log_err(
                "dartvm_fetch_build.py introuvable.\n"
                "       Ce fichier doit être dans le même dossier que blutter.py."
            )
            sys.exit(1)
        inp.rebuild = True

    # Compiler Blutter si nécessaire
    if not os.path.isfile(inp.blutter_file) or inp.rebuild:
        cmake_build(inp)
        if not os.path.isfile(inp.blutter_file):
            log_err(f"Build terminé mais exécutable introuvable : {inp.blutter_file}")
            sys.exit(1)

    # Générer solution VS (Windows dev)
    if inp.create_vs_sln:
        _generate_vs_solution(inp)
        return

    # Lancer l'analyse
    os.makedirs(inp.outdir, exist_ok=True)
    log_section("ANALYSE EN COURS")
    log_info(f"Exécutable : {os.path.basename(inp.blutter_file)}")
    log_info(f"Cible      : {inp.libapp_path}")
    log_info(f"Sortie     : {inp.outdir}")
    print()

    subprocess.run(
        [inp.blutter_file, "-i", inp.libapp_path, "-o", inp.outdir],
        check=True,
    )

    log_section("ANALYSE TERMINÉE")
    log_ok(f"Résultats dans : {inp.outdir}")
    _show_output_summary(inp.outdir)


def _show_output_summary(outdir: str):
    """Affiche un résumé des fichiers générés."""
    files = []
    for name in ["asm", "blutter_frida.js", "objs.txt", "pp.txt"]:
        path = os.path.join(outdir, name)
        if os.path.exists(path):
            if os.path.isdir(path):
                count = len(os.listdir(path))
                files.append(f"  {C.BGREEN}◈{C.RESET}  {name}/  {C.DIM}({count} fichiers){C.RESET}")
            else:
                size = os.path.getsize(path)
                files.append(f"  {C.BGREEN}◈{C.RESET}  {name}  {C.DIM}({size/1024:.1f} KB){C.RESET}")
    if files:
        print()
        for f in files:
            print(f)


def _generate_vs_solution(inp: BlutterInput):
    """Génère une solution Visual Studio (Windows uniquement)."""
    macros      = find_compat_macros(inp.dart_info.version, inp.no_analysis, inp.ida_fcn)
    blutter_src = os.path.join(SCRIPT_DIR, "blutter")
    dbg_out     = os.path.abspath(os.path.join(inp.outdir, "out"))
    dbg_args    = f"-i {inp.libapp_path} -o {dbg_out}"
    vscmd       = os.getenv("VSCMD_VER", "")

    if not vscmd:
        log_err("Solution VS : lancez dans 'x64 Native Tools Command Prompt'.")
        sys.exit(1)

    gen = None
    if vscmd.startswith("18."): gen = "Visual Studio 18 2026"
    elif vscmd.startswith("17."): gen = "Visual Studio 17 2022"
    if not gen:
        log_err(f"Version VS non supportée : {vscmd}")
        sys.exit(1)

    subprocess.run(
        [CMAKE_CMD, "-G", gen, "-A", "x64", "-B", inp.outdir,
         f"-DDARTLIB={inp.dart_info.lib_name}",
         f"-DNAME_SUFFIX={inp.name_suffix}",
         f"-DDBG_CMD:STRING={dbg_args}"]
        + macros + [blutter_src],
        check=True,
    )
    log_ok(f"Solution VS générée dans : {inp.outdir}")


# ─────────────────────────────────────────────────────────────
#  Points d'entrée analyse
# ─────────────────────────────────────────────────────────────
def run_with_flutter(libapp: str, libflutter: str, outdir: str,
                     rebuild: bool, vs_sln: bool, no_analysis: bool, ida_fcn: bool):
    dart_info = get_dart_lib_info(libapp, libflutter)
    inp = BlutterInput(libapp, dart_info, outdir,
                       rebuild, vs_sln, no_analysis, ida_fcn)
    build_and_run(inp)

def run_with_dart_version(libapp: str, dart_version: str, outdir: str,
                          rebuild: bool, vs_sln: bool, no_analysis: bool, ida_fcn: bool):
    parts = dart_version.split("_")
    if len(parts) != 3:
        log_err(
            f"Format --dart-version invalide : '{dart_version}'\n"
            "       Attendu : VERSION_OS_ARCH  ex: 3.4.2_android_arm64"
        )
        sys.exit(1)
    version, os_name, arch = parts
    dart_info = DartLibInfo(version, os_name, arch)
    inp = BlutterInput(libapp, dart_info, outdir,
                       rebuild, vs_sln, no_analysis, ida_fcn)
    build_and_run(inp)

def run(indir: str, outdir: str,
        rebuild: bool, vs_sln: bool, no_analysis: bool, ida_fcn: bool):
    if indir.lower().endswith(".apk"):
        print_apk_info(indir)
        with tempfile.TemporaryDirectory() as tmp:
            app, flutter = extract_libs_from_apk(indir, tmp)
            run_with_flutter(app, flutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn)
    else:
        app, flutter = find_lib_files(indir)
        run_with_flutter(app, flutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn)


# ─────────────────────────────────────────────────────────────
#  Mise à jour Git
# ─────────────────────────────────────────────────────────────
def git_update(retries: int = 2):
    if not shutil.which("git"):
        log_warn("git introuvable — mise à jour ignorée.")
        return

    log_info("Vérification des mises à jour git…")

    def _git(args, timeout=20) -> str:
        try:
            r = subprocess.run(
                ["git"] + args,
                capture_output=True,
                timeout=timeout,
                cwd=SCRIPT_DIR,
            )
            return (r.stdout or r.stderr).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            log_warn(f"Timeout git {' '.join(args)}")
            return ""
        except Exception as e:
            log_dbg(f"git error : {e}")
            return ""

    _git(["fetch"], timeout=15)

    for attempt in range(1, retries + 1):
        output = _git(["pull"])
        if "Already up to date." in output:
            log_ok("Dépôt déjà à jour.")
            return
        if output.strip():
            log_ok("Mise à jour appliquée.")
            return
        log_dbg(f"Tentative git {attempt}/{retries} — réponse vide")
        time.sleep(1)

    log_warn("Mise à jour git ignorée (pas de réponse).")


# ─────────────────────────────────────────────────────────────
#  Mode TUI interactif
# ─────────────────────────────────────────────────────────────
def _ask(prompt: str, default: str = "") -> str:
    display = f"  {C.BCYAN}{prompt}{C.RESET}"
    if default:
        display += f" {C.DIM}[{default}]{C.RESET}"
    display += " : "
    try:
        val = input(display).strip()
        return val if val else default
    except EOFError:
        return default

def _confirm(prompt: str, default: bool = True) -> bool:
    hint  = "[O/n]" if default else "[o/N]"
    val   = _ask(f"{prompt} {hint}").lower()
    if not val:
        return default
    return val in ("o", "oui", "y", "yes")

def _browse(start: str = ".", dirs_only: bool = True,
            ext_filter: list = None, title: str = "Naviguer"):
    """
    Mini explorateur de fichiers/dossiers en ligne de commande.
    dirs_only=True → retourne un dossier
    ext_filter=[".apk"] → retourne un fichier de cette extension
    """
    current = os.path.abspath(start)

    while True:
        log_section(f"{title}  ·  {current}")

        try:
            entries = sorted(
                os.scandir(current),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except PermissionError:
            log_err(f"Accès refusé : {current}")
            current = os.path.dirname(current)
            continue

        items = [("0", "..", None, True)]
        print(f"  {C.DIM}0{C.RESET}  {C.CYAN}↑  ..(dossier parent){C.RESET}")

        idx = 1
        for entry in entries:
            is_dir = entry.is_dir()
            ext    = os.path.splitext(entry.name)[1].lower()

            if not is_dir and ext_filter and ext not in ext_filter:
                continue   # cacher les fichiers non pertinents

            if is_dir:
                print(f"  {C.DIM}{idx}{C.RESET}  {C.BCYAN}▸ {entry.name}/{C.RESET}")
            else:
                try:
                    sz = f"{os.path.getsize(entry.path)/1024/1024:.1f} MB"
                except Exception:
                    sz = "?"
                print(f"  {C.DIM}{idx}{C.RESET}  {C.BGREEN}  {entry.name}{C.RESET}  "
                      f"{C.DIM}{sz}{C.RESET}")

            items.append((str(idx), entry.name, entry.path, is_dir))
            idx += 1

        if dirs_only:
            print(f"\n  {C.DIM}[S] Sélectionner ce dossier  [P] Saisir chemin  [Q] Annuler{C.RESET}")
        else:
            print(f"\n  {C.DIM}[P] Saisir chemin  [Q] Annuler{C.RESET}")

        choice = _ask("Choix").upper()

        if choice == "Q":
            return None
        if choice == "S" and dirs_only:
            return current
        if choice == "P":
            path = _ask("Chemin complet")
            path = os.path.expanduser(path)
            if os.path.exists(path):
                return os.path.abspath(path)
            log_warn(f"Chemin inexistant : {path}")
            continue

        try:
            n = int(choice)
        except ValueError:
            log_warn("Entrée invalide.")
            continue

        match = next((it for it in items if it[0] == str(n)), None)
        if not match:
            log_warn("Numéro hors plage.")
            continue

        _, name, full_path, is_dir = match

        if name == "..":
            current = os.path.dirname(current)
        elif is_dir:
            current = full_path
        elif ext_filter:
            return full_path
        else:
            log_warn("Sélectionnez un dossier.")


def interactive_mode():
    """Mode interactif complet sans dépendances."""
    _acquire_lock()
    print_banner()

    # ── Sélection de la cible ──────────────────────────────────
    log_section("CIBLE")
    print(f"  {C.CYAN}1{C.RESET}  Naviguer vers un APK")
    print(f"  {C.CYAN}2{C.RESET}  Naviguer vers un dossier de libs")
    print(f"  {C.CYAN}3{C.RESET}  Saisir le chemin manuellement")
    print(f"  {C.CYAN}H{C.RESET}  Historique des analyses")
    print()

    while True:
        mode = _ask("Mode", "1").upper()
        if mode == "H":
            show_history()
            continue
        if mode in ("1", "2", "3"):
            break
        log_warn("Choix invalide.")

    indir = None
    if mode == "1":
        indir = _browse(".", dirs_only=False, ext_filter=[".apk"],
                        title="Sélectionner l'APK")
    elif mode == "2":
        indir = _browse(".", dirs_only=True, title="Sélectionner le dossier des libs")
    else:
        path = _ask("Chemin de la cible")
        path = os.path.expanduser(path)
        if os.path.exists(path):
            indir = os.path.abspath(path)

    if not indir or not os.path.exists(indir):
        log_err(f"Cible introuvable : {indir}")
        sys.exit(1)
    log_ok(f"Cible : {indir}")

    # ── Dossier de sortie ──────────────────────────────────────
    log_section("DOSSIER DE SORTIE")
    outdir_raw = _ask("Dossier de sortie", "./blutter_out")
    outdir     = os.path.abspath(os.path.expanduser(outdir_raw))

    if not os.path.exists(outdir):
        if _confirm(f"'{outdir}' n'existe pas. Créer ?", default=True):
            os.makedirs(outdir, exist_ok=True)
            log_ok(f"Créé : {outdir}")
        else:
            sys.exit(0)

    # ── Options ────────────────────────────────────────────────
    log_section("OPTIONS")

    rebuild     = _confirm("--rebuild  (force recompilation)", default=False)
    no_analysis = _confirm("--no-analysis  (désactiver l'analyse Dart)", default=False)
    ida_fcn     = _confirm("--ida-fcn  (noms de fonctions IDA)", default=False)
    do_update   = _confirm("Vérifier les mises à jour git", default=True)

    dart_ver    = _ask("Version Dart manuelle (laisser vide = auto)", "")
    dart_ver    = dart_ver.strip() or None

    # ── Récap + confirmation ───────────────────────────────────
    log_section("CONFIGURATION")
    print(f"  {C.CYAN}Cible         {C.RESET}: {indir}")
    print(f"  {C.CYAN}Sortie        {C.RESET}: {outdir}")
    print(f"  {C.CYAN}Rebuild       {C.RESET}: {rebuild}")
    print(f"  {C.CYAN}No-analysis   {C.RESET}: {no_analysis}")
    print(f"  {C.CYAN}IDA fcn       {C.RESET}: {ida_fcn}")
    print(f"  {C.CYAN}Dart version  {C.RESET}: {dart_ver or 'auto-detect'}")
    print()

    if not _confirm("Lancer l'analyse ?", default=True):
        log_warn("Annulé.")
        sys.exit(0)

    # ── Lancement ──────────────────────────────────────────────
    if do_update:
        git_update()

    success = True
    try:
        if dart_ver:
            run_with_dart_version(
                indir, dart_ver, outdir,
                rebuild, False, no_analysis, ida_fcn
            )
        else:
            run(indir, outdir, rebuild, False, no_analysis, ida_fcn)
    except SystemExit as e:
        if e.code not in (0, None):
            success = False
    except Exception as e:
        success = False
        log_err(f"Erreur : {e}")
        if DEBUG_MODE:
            raise

    _save_history({
        "date":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indir":        indir,
        "outdir":       outdir,
        "dart_version": dart_ver or "auto",
        "success":      success,
    })

    elapsed = _fmt_elapsed()
    status  = f"{C.BGREEN}SUCCÈS{C.RESET}" if success else f"{C.BRED}ÉCHEC{C.RESET}"
    print(f"\n  {status}  ·  Durée : {elapsed}\n")


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Flag --debug précoce (avant argparse)
    if "--debug" in sys.argv:
        DEBUG_MODE = True
        sys.argv.remove("--debug")

    # Mode interactif si aucun argument
    if len(sys.argv) == 1:
        interactive_mode()
        sys.exit(0)

    # ── Argparse ──────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        prog="blutter",
        description="Flutter Reverse Engineering Tool — ARM64 · Dart VM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples :\n"
            "  python blutter.py app.apk ./out\n"
            "  python blutter.py ./libs/arm64-v8a ./out --rebuild\n"
            "  python blutter.py app.apk ./out --dart-version 3.4.2_android_arm64\n"
            "  python blutter.py --check-deps\n"
            "  python blutter.py --history\n"
        ),
    )
    parser.add_argument("indir",  nargs="?", help="APK ou dossier contenant les libs")
    parser.add_argument("outdir", nargs="?", help="Dossier de sortie")
    parser.add_argument("--dart-version",  help="Version Dart ex: 3.4.2_android_arm64")
    parser.add_argument("--rebuild",       action="store_true", help="Force recompilation")
    parser.add_argument("--vs-sln",        action="store_true", help="Génère solution Visual Studio")
    parser.add_argument("--no-analysis",   action="store_true", help="Désactive l'analyse Dart")
    parser.add_argument("--ida-fcn",       action="store_true", help="Noms de fonctions IDA")
    parser.add_argument("--no-update",     action="store_true", help="Ne pas vérifier les MàJ git")
    parser.add_argument("--debug",         action="store_true", help="Mode debug (tracebacks)")
    parser.add_argument("--check-deps",    action="store_true", help="Vérifie les dépendances")
    parser.add_argument("--history",       action="store_true", help="Affiche l'historique")

    args = parser.parse_args()

    if args.debug:
        DEBUG_MODE = True

    # Commandes autonomes
    if args.check_deps:
        print_banner()
        check_dependencies(strict=True)
        sys.exit(0)

    if args.history:
        print_banner()
        show_history()
        sys.exit(0)

    # Validation des arguments obligatoires
    if not args.indir or not args.outdir:
        parser.error("Les arguments <indir> et <outdir> sont obligatoires.")

    if not os.path.exists(args.indir):
        log_err(f"Cible introuvable : {args.indir}")
        sys.exit(1)

    # ── Démarrage ─────────────────────────────────────────────
    print_banner()
    _acquire_lock()

    os.makedirs(args.outdir, exist_ok=True)

    if not args.no_update:
        git_update()

    success = True
    detected_version = args.dart_version or "auto"

    try:
        if args.dart_version:
            run_with_dart_version(
                args.indir, args.dart_version, args.outdir,
                args.rebuild, args.vs_sln, args.no_analysis, args.ida_fcn,
            )
        else:
            run(
                args.indir, args.outdir,
                args.rebuild, args.vs_sln, args.no_analysis, args.ida_fcn,
            )
    except SystemExit as e:
        if e.code not in (0, None):
            success = False
    except KeyboardInterrupt:
        log_warn("Interrompu par l'utilisateur.")
        success = False
        sys.exit(130)
    except Exception as e:
        success = False
        log_err(f"Erreur fatale : {e}")
        if DEBUG_MODE:
            raise
        sys.exit(1)

    _save_history({
        "date":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indir":        args.indir,
        "outdir":       args.outdir,
        "dart_version": detected_version,
        "success":      success,
    })

    elapsed = _fmt_elapsed()
    if success:
        log_ok(f"Session terminée en {elapsed}")
    else:
        log_err(f"Session échouée après {elapsed}")
        sys.exit(1)
