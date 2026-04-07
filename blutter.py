#!/usr/bin/env python3


from __future__ import annotations

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

# ═══════════════════════════════════════════════════════════════
#  CONSTANTES
# ═══════════════════════════════════════════════════════════════
VERSION      = "4.1.0"
SCRIPT_DIR   = os.path.dirname(os.path.realpath(__file__))
BIN_DIR      = os.path.join(SCRIPT_DIR, "bin")
PKG_INC_DIR  = os.path.join(SCRIPT_DIR, "packages", "include")
PKG_LIB_DIR  = os.path.join(SCRIPT_DIR, "packages", "lib")
BUILD_DIR    = os.path.join(SCRIPT_DIR, "build")
HISTORY_FILE = os.path.expanduser("~/.chblutter_history")
LOCK_FILE    = os.path.join(tempfile.gettempdir(), "chblutter.lock")

CMAKE_CMD    = "cmake"
NINJA_CMD    = "ninja"

ARM_ARCH_DIRS       = ["arm64-v8a", "armeabi-v7a", "armeabi"]
FLUTTER_LIB_NAMES   = ["libflutter.so", "Flutter", "libFlutter.so"]
APP_LIB_KNOWN_NAMES = ["libapp.so", "App", "libApp.so"]

DEBUG_MODE    = False
SESSION_START = time.time()
IS_TERMUX     = (
    os.path.exists("/data/data/com.termux") or
    "com.termux" in os.environ.get("PREFIX", "") or
    "com.termux" in os.environ.get("HOME", "")
)


# ═══════════════════════════════════════════════════════════════
#  ANSI COULEURS
# ═══════════════════════════════════════════════════════════════
def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

USE_COLOR = _supports_color()

def _c(code: str) -> str:
    return f"\033[{code}m" if USE_COLOR else ""

class C:
    R    = _c("0")
    B    = _c("1")
    DIM  = _c("2")
    IT   = _c("3")
    RED  = _c("31")
    GRN  = _c("32")
    YLW  = _c("33")
    BLU  = _c("34")
    MAG  = _c("35")
    CYN  = _c("36")
    WHT  = _c("37")
    BRED = _c("91")
    BGRN = _c("92")
    BYLW = _c("93")
    BBLU = _c("94")
    BMAG = _c("95")
    BCYN = _c("96")
    BWHT = _c("97")


def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)

def _term_width() -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, 100)


# ═══════════════════════════════════════════════════════════════
#  LOGGER
# ═══════════════════════════════════════════════════════════════
def log_ok(msg: str):
    print(f"  {C.BGRN}✔{C.R}  {C.GRN}{msg}{C.R}")

def log_info(msg: str):
    print(f"  {C.BCYN}◈{C.R}  {msg}")

def log_warn(msg: str):
    print(f"  {C.BYLW}⚠{C.R}  {C.YLW}{msg}{C.R}", file=sys.stderr)

def log_err(msg: str):
    print(f"  {C.BRED}✘{C.R}  {C.RED}{msg}{C.R}", file=sys.stderr)

def log_dbg(msg: str):
    if DEBUG_MODE:
        print(f"  {C.DIM}{C.MAG}⬡ {msg}{C.R}")

def log_section(title: str, sub: bool = False):
    w   = _term_width()
    bar = C.DIM + C.CYN + ("┄" * w) + C.R
    clr = C.CYN if sub else C.BCYN
    print(f"\n{bar}")
    pad = max(0, (w - len(_strip_ansi(title)) - 4) // 2)
    print(f"{clr}{' ' * pad}▸ {title} ◂{C.R}")
    print(bar)


# ═══════════════════════════════════════════════════════════════
#  CLEAR + BANNIÈRE FUTURISTE
# ═══════════════════════════════════════════════════════════════
def _clear():
    os.system("cls" if os.name == "nt" else "clear")

_LOGO = r"""
  ██████╗██╗  ██╗      ██████╗ ██╗     ██╗   ██╗████████╗████████╗███████╗██████╗
 ██╔════╝██║  ██║      ██╔══██╗██║     ██║   ██║╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
 ██║     ███████║█████╗██████╔╝██║     ██║   ██║   ██║      ██║   █████╗  ██████╔╝
 ██║     ██╔══██║╚════╝██╔══██╗██║     ██║   ██║   ██║      ██║   ██╔══╝  ██╔══██╗
 ╚██████╗██║  ██║      ██████╔╝███████╗╚██████╔╝   ██║      ██║   ███████╗██║  ██║
  ╚═════╝╚═╝  ╚═╝      ╚═════╝ ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝"""

def _glitch(text: str, prob: float = 0.025) -> str:
    import random
    G = "!#$%&*<>[]{}|~░▒▓"
    return "".join(random.choice(G) if random.random() < prob else c for c in text)

def _rand_hex(n: int = 8) -> str:
    import random
    return "0x" + "".join(random.choices("0123456789ABCDEF", k=n))

def print_banner():
    import random
    w = _term_width()
    bar = "".join(
        random.choice("░▒▓█") if random.random() < 0.12 else "─"
        for _ in range(w)
    )
    print(f"{C.DIM}{C.GRN}{bar}{C.R}")
    for line in _LOGO.strip("\n").split("\n"):
        print(f"{C.BGRN}{_glitch(line)}{C.R}")
    env = f"{C.BMAG}Termux{C.R}" if IS_TERMUX else f"{C.BCYN}{platform.system()}{C.R}"
    a1, a2 = _rand_hex(), _rand_hex()
    print()
    print(f"  {C.BCYN}◈{C.R} {C.B}Flutter Reverse Engineering{C.R}  "
          f"{C.DIM}v{VERSION}{C.R}  ·  {env}  ·  {C.DIM}{platform.machine()}{C.R}")
    print(f"  {C.DIM}{C.GRN}◈ ARM64/ARM32 · Dart VM · IDA/Ghidra · [{a1}→{a2}]{C.R}")
    print(f"  {C.DIM}◈ {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}{C.R}")
    print(f"{C.DIM}{C.GRN}{bar}{C.R}\n")


# ═══════════════════════════════════════════════════════════════
#  SPINNER
# ═══════════════════════════════════════════════════════════════
class Spinner:
    FRAMES = ["⠋","⠙","⠸","⠴","⠦","⠇","⠏"]

    def __init__(self, label: str):
        self.label   = label
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        i = 0
        while not self._stop.is_set():
            f = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r  {C.BCYN}{f}{C.R}  {self.label}…", end="", flush=True)
            time.sleep(0.07)
            i += 1

    def __enter__(self):
        if sys.stdout.isatty():
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)
        if sys.stdout.isatty():
            print(f"\r{' ' * (len(self.label) + 14)}\r", end="", flush=True)


# ═══════════════════════════════════════════════════════════════
#  VERROU DE SESSION
# ═══════════════════════════════════════════════════════════════
def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            pid = Path(LOCK_FILE).read_text().strip()
            log_warn(f"Instance ch-blutter déjà en cours ? (PID {pid})")
            rep = input(f"  {C.BYLW}Ignorer le verrou ? [o/N]{C.R} : ").strip().lower()
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


# ═══════════════════════════════════════════════════════════════
#  SIGNAL CTRL+C
# ═══════════════════════════════════════════════════════════════
def _sigint(sig, frame):
    print(f"\n\n  {C.BRED}⚡  Interruption — session avortée.{C.R}")
    print(f"  {C.DIM}Durée : {_fmt_elapsed()}{C.R}\n")
    _release_lock()
    sys.exit(130)

signal.signal(signal.SIGINT, _sigint)

def _fmt_elapsed() -> str:
    s = int(time.time() - SESSION_START)
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


# ═══════════════════════════════════════════════════════════════
#  VÉRIFICATION DES DÉPENDANCES
# ═══════════════════════════════════════════════════════════════
REQUIRED_BINS = {
    "cmake":      "cmake         → pkg install cmake          (ou apt install cmake)",
    "ninja":      "ninja         → pkg install ninja          (ou apt install ninja-build)",
    "git":        "git           → pkg install git",
    "python3":    "python3       → pkg install python         (ou apt install python3)",
}
COMPILER_BINS = {
    "clang":      "clang         → pkg install clang          (ou apt install clang)",
    "gcc":        "gcc           → pkg install gcc            (ou apt install build-essential)",
}
OPTIONAL_BINS = {
    "pkg-config": "pkg-config    → pkg install pkg-config",
    "strip":      "strip         → pkg install binutils",
}
REQUIRED_PY = {
    "pyelftools": "pyelftools     → pip install pyelftools",
    "requests":   "requests       → pip install requests",
}
PKG_CONFIG_LIBS = {
    "capstone":   "libcapstone   → pkg install capstone       (ou apt install libcapstone-dev)",
    "icu-i18n":   "libicu        → pkg install libicu         (ou apt install libicu-dev)",
    "fmt":        "libfmt        → pkg install libfmt         (ou apt install libfmt-dev)",
}

def check_dependencies(silent: bool = False) -> bool:
    """
    Vérifie toutes les dépendances nécessaires au build et à l'analyse.
    Retourne True si toutes les dépendances requises sont présentes.
    """
    if not silent:
        log_section("VÉRIFICATION DES DÉPENDANCES")
    ok_all = True

    def chk_bin(cmd: str, label: str, required: bool = True) -> bool:
        nonlocal ok_all
        found = shutil.which(cmd) is not None
        if not silent:
            st = f"{C.BGRN}OK{C.R}" if found else (
                f"{C.BRED}MANQUANT{C.R}" if required else f"{C.BYLW}ABSENT{C.R}"
            )
            print(f"  {st}  {label}")
        if not found and required:
            ok_all = False
        return found

    def chk_py(mod: str, label: str) -> bool:
        nonlocal ok_all
        try:
            __import__(mod)
            if not silent:
                print(f"  {C.BGRN}OK{C.R}  {label}")
            return True
        except ImportError:
            if not silent:
                print(f"  {C.BRED}MANQUANT{C.R}  {label}")
            ok_all = False
            return False

    def chk_pkgconfig(lib: str, label: str) -> bool:
        nonlocal ok_all
        if not shutil.which("pkg-config"):
            return True
        rc = subprocess.run(
            ["pkg-config", "--exists", lib], capture_output=True,
        ).returncode
        found = (rc == 0)
        if not silent:
            st = f"{C.BGRN}OK{C.R}" if found else f"{C.BRED}MANQUANT{C.R}"
            print(f"  {st}  {label}")
        if not found:
            ok_all = False
        return found

    if not silent:
        print(f"\n  {C.B}Outils système :{C.R}")
    for cmd, lbl in REQUIRED_BINS.items():
        chk_bin(cmd, lbl, required=True)

    if not silent:
        print(f"\n  {C.B}Compilateur C++ (clang ou gcc requis) :{C.R}")
    has_compiler = any(shutil.which(c) for c in COMPILER_BINS)
    for cmd, lbl in COMPILER_BINS.items():
        chk_bin(cmd, lbl, required=False)
    if not has_compiler:
        if not silent:
            print(f"  {C.BRED}✘  AUCUN compilateur C++ trouvé — build impossible !{C.R}")
        ok_all = False

    if not silent:
        print(f"\n  {C.B}Outils optionnels :{C.R}")
    for cmd, lbl in OPTIONAL_BINS.items():
        chk_bin(cmd, lbl, required=False)

    if not silent:
        print(f"\n  {C.B}Modules Python :{C.R}")
    for mod, lbl in REQUIRED_PY.items():
        chk_py(mod, lbl)

    if shutil.which("pkg-config"):
        if not silent:
            print(f"\n  {C.B}Bibliothèques natives :{C.R}")
        for lib, lbl in PKG_CONFIG_LIBS.items():
            chk_pkgconfig(lib, lbl)
    elif not silent:
        print(f"\n  {C.BYLW}pkg-config absent — vérification libs natives ignorée.{C.R}")

    if not silent:
        major, minor = sys.version_info[:2]
        py_ok = (major, minor) >= (3, 9)
        st = f"{C.BGRN}OK{C.R}" if py_ok else f"{C.BRED}TROP ANCIEN{C.R}"
        print(f"\n  {st}  Python {major}.{minor}  (3.9+ requis)")
        if not py_ok:
            ok_all = False

    if not silent:
        print()
        if ok_all:
            print(f"  {C.BGRN}✔  Toutes les dépendances sont présentes.{C.R}")
        else:
            print(f"  {C.BYLW}⚠  Dépendances manquantes — la compilation peut échouer.{C.R}")
            if IS_TERMUX:
                print(f"\n  {C.BCYN}Commande Termux tout-en-un :{C.R}")
                print(f"  {C.DIM}pkg install -y git cmake ninja clang binutils "
                      f"pkg-config libicu capstone fmt python && "
                      f"pip install requests pyelftools{C.R}")
            else:
                print(f"\n  {C.BCYN}Commande Debian/Ubuntu tout-en-un :{C.R}")
                print(f"  {C.DIM}sudo apt install -y git cmake ninja-build clang "
                      f"pkg-config libicu-dev libcapstone-dev libfmt-dev python3 && "
                      f"pip3 install requests pyelftools{C.R}")
    return ok_all


# ═══════════════════════════════════════════════════════════════
#  HISTORIQUE
# ═══════════════════════════════════════════════════════════════
def _load_history() -> list:
    try:
        return json.loads(Path(HISTORY_FILE).read_text())
    except Exception:
        return []

def _save_history(entry: dict):
    hist = _load_history()
    hist.insert(0, entry)
    try:
        Path(HISTORY_FILE).write_text(json.dumps(hist[:30], indent=2))
    except Exception:
        pass

def show_history():
    hist = _load_history()
    log_section("HISTORIQUE DES ANALYSES")
    if not hist:
        log_info("Aucun historique disponible.")
        return
    print(f"  {C.DIM}{'#':<3}  {'Date':<20}  {'Cible':<28}  {'Dart':<16}  Statut{C.R}")
    print(f"  {C.DIM}{'─'*3}  {'─'*20}  {'─'*28}  {'─'*16}  {'─'*6}{C.R}")
    for i, e in enumerate(hist, 1):
        st  = f"{C.BGRN}OK{C.R}" if e.get("success") else f"{C.BRED}ÉCHEC{C.R}"
        tgt = os.path.basename(e.get("indir", "?").rstrip("/\\")) or e.get("indir","?")
        print(f"  {i:<3}  {e.get('date','?'):<20}  {tgt[:28]:<28}  "
              f"{e.get('dart_version','?'):<16}  {st}")


# ═══════════════════════════════════════════════════════════════
#  ANALYSE APK (informations)
# ═══════════════════════════════════════════════════════════════
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def print_apk_info(apk_path: str):
    log_section("ANALYSE APK", sub=True)
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            names     = zf.namelist()
            so_files  = [n for n in names if n.endswith(".so")]
            dex_files = [n for n in names if n.endswith(".dex")]
            total_sz  = sum(i.file_size for i in zf.infolist())
        fsize = os.path.getsize(apk_path)
        sha   = _sha256(apk_path)
        print(f"  {C.BCYN}Fichier  {C.R}: {os.path.basename(apk_path)}")
        print(f"  {C.BCYN}Taille   {C.R}: {fsize/1024/1024:.2f} MB "
              f"{C.DIM}(décomp. {total_sz/1024/1024:.2f} MB){C.R}")
        print(f"  {C.BCYN}SHA-256  {C.R}: {C.DIM}{sha[:20]}…{C.R}")
        print(f"  {C.BCYN}Libs .so {C.R}: {len(so_files)}  {C.BCYN}DEX{C.R}: {len(dex_files)}")
        for arch in ARM_ARCH_DIRS:
            libs = [n for n in so_files if f"lib/{arch}/" in n]
            if libs:
                print(f"\n  {C.B}{C.BCYN}{arch}{C.R}")
                for lib in libs:
                    print(f"    {C.DIM}{C.GRN}◈{C.R}  {os.path.basename(lib)}")
    except zipfile.BadZipFile:
        log_err(f"APK corrompu : {apk_path}")
    except Exception as e:
        log_warn(f"Analyse APK partielle : {e}")


# ═══════════════════════════════════════════════════════════════
#  RECHERCHE DES BIBLIOTHÈQUES
# ═══════════════════════════════════════════════════════════════
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
    app     = _find_app_lib(indir)
    flutter = _find_flutter_lib(indir)
    if app and flutter:
        return app, flutter

    for arch in ARM_ARCH_DIRS:
        for candidate in (
            os.path.join(indir, arch),
            os.path.join(indir, "lib", arch),
        ):
            if not os.path.isdir(candidate):
                continue
            app     = _find_app_lib(candidate)
            flutter = _find_flutter_lib(candidate)
            if app and flutter:
                log_ok(f"Libs dans : {C.BCYN}{arch}{C.R}")
                log_info(f"App     → {os.path.basename(app)}")
                log_info(f"Flutter → {os.path.basename(flutter)}")
                return app, flutter

    if not app:
        log_err(
            f"libapp.so introuvable.\n"
            f"  Dossier : {indir}\n"
            f"  Archs testées : {', '.join(ARM_ARCH_DIRS)}"
        )
    else:
        log_err(f"libflutter.so introuvable. Noms cherchés : {', '.join(FLUTTER_LIB_NAMES)}")
    sys.exit(1)

def extract_libs_from_apk(apk_path: str, tmp_dir: str):
    flutter_lower = {n.lower() for n in FLUTTER_LIB_NAMES}
    with zipfile.ZipFile(apk_path, "r") as zf:
        names = zf.namelist()
        for arch in ARM_ARCH_DIRS:
            prefix  = f"lib/{arch}/"
            fl_info = None
            for fn in FLUTTER_LIB_NAMES:
                if prefix + fn in names:
                    fl_info = zf.getinfo(prefix + fn)
                    break
            if not fl_info:
                continue
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
                log_ok(f"APK → {arch}")
                zf.extract(app_info, tmp_dir)
                zf.extract(fl_info,  tmp_dir)
                return (
                    os.path.join(tmp_dir, app_info.filename),
                    os.path.join(tmp_dir, fl_info.filename),
                )
    log_err(f"Libs introuvables dans l'APK. Archs : {', '.join(ARM_ARCH_DIRS)}")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  DartLibInfo  (importé ou fallback minimal)
# ═══════════════════════════════════════════════════════════════
try:
    from dartvm_fetch_build import DartLibInfo
except ImportError:
    class DartLibInfo:  # type: ignore
        def __init__(self, version, os_name, arch,
                     has_compressed_ptrs=None, snapshot_hash=None):
            self.version          = version
            self.os_name          = os_name
            self.arch             = arch
            self.snapshot_hash    = snapshot_hash
            self.has_compressed_ptrs = (
                has_compressed_ptrs if has_compressed_ptrs is not None
                else (os_name != "ios")
            )
            self.lib_name = f"dartvm{version}_{os_name}_{arch}"


# ═══════════════════════════════════════════════════════════════
#  BlutterInput
# ═══════════════════════════════════════════════════════════════
class BlutterInput:
    def __init__(self, libapp_path, dart_info, outdir,
                 rebuild, create_vs_sln, no_analysis, ida_fcn):
        self.libapp_path   = libapp_path
        self.dart_info     = dart_info
        self.outdir        = outdir
        self.rebuild       = rebuild
        self.create_vs_sln = create_vs_sln
        self.ida_fcn       = ida_fcn

        vers = dart_info.version.split(".", 2)
        if int(vers[0]) == 2 and int(vers[1]) < 15:
            if not no_analysis:
                log_warn("Dart <2.15 → force --no-analysis")
            no_analysis = True
        self.no_analysis = no_analysis

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


# ═══════════════════════════════════════════════════════════════
#  MACROS CMAKE
# ═══════════════════════════════════════════════════════════════
def find_compat_macros(dart_version: str, no_analysis: bool, ida_fcn: bool) -> list:
    macros  = []
    vm_dir  = os.path.join(PKG_INC_DIR, f"dartvm{dart_version}", "vm")

    def _scan(filename: str, checks: list):
        path = os.path.join(vm_dir, filename)
        if not os.path.isfile(path):
            log_warn(f"Header absent (ignoré) : {filename}")
            return
        try:
            with open(path, "rb") as f:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                for needle, macro in checks:
                    hit = (mm.find(needle) != -1) if isinstance(needle, bytes) else needle(mm)
                    if hit:
                        macros.append(macro)
                mm.close()
        except Exception as e:
            log_warn(f"Lecture {filename} : {e}")

    _scan("class_id.h", [
        (b"V(LinkedHashMap)",  "-DOLD_MAP_SET_NAME=1"),
        (lambda mm: mm.find(b"V(LinkedHashMap)") != -1
                    and mm.find(b"V(ImmutableLinkedHashMap)") == -1,
         "-DOLD_MAP_NO_IMMUTABLE=1"),
        (lambda mm: mm.find(b" kLastInternalOnlyCid ") == -1,
         "-DNO_LAST_INTERNAL_ONLY_CID=1"),
        (b"V(TypeRef)",        "-DHAS_TYPE_REF=1"),
    ])
    try:
        if int(dart_version.split(".")[0]) >= 3:
            _scan("class_id.h", [(b"V(RecordType)", "-DHAS_RECORD_TYPE=1")])
    except (ValueError, IndexError):
        pass
    _scan("class_table.h",    [(b"class SharedClassTable {", "-DHAS_SHARED_CLASS_TABLE=1")])
    _scan("stub_code_list.h", [
        (lambda mm: mm.find(b"V(InitLateStaticField)") == -1, "-DNO_INIT_LATE_STATIC_FIELD=1")
    ])
    _scan("object_store.h", [
        (lambda mm: mm.find(b"build_generic_method_extractor_code)") == -1,
         "-DNO_METHOD_EXTRACTOR_STUB=1")
    ])
    _scan("object.h", [
        (lambda mm: mm.find(b"AsTruncatedInt64Value()") == -1, "-DUNIFORM_INTEGER_ACCESS=1")
    ])

    if no_analysis:
        macros.append("-DNO_CODE_ANALYSIS=1")
    if ida_fcn:
        macros.append("-DIDA_FCN=1")
    try:
        maj, min_ = int(dart_version.split(".")[0]), int(dart_version.split(".")[1])
        if (maj, min_) >= (3, 5):
            macros.append("-DOLD_MARKING_STACK_BLOCK=1")
    except (ValueError, IndexError):
        pass

    # Dédoublonnage stable
    seen, result = set(), []
    for m in macros:
        if m not in seen:
            seen.add(m); result.append(m)
    return result


# ═══════════════════════════════════════════════════════════════
#  CMAKE / BUILD
# ═══════════════════════════════════════════════════════════════
def _run_cmd(cmd: list, label: str, timeout: int = None,
             cwd: str = None, env: dict = None):
    log_dbg(f"cmd: {' '.join(str(c) for c in cmd)}")
    with Spinner(label):
        try:
            result = subprocess.run(
                cmd, cwd=cwd, env=env, timeout=timeout,
                capture_output=not DEBUG_MODE,
            )
        except subprocess.TimeoutExpired:
            log_err(f"Timeout ({timeout}s) : {label}")
            raise
        except FileNotFoundError:
            log_err(f"Commande introuvable : {cmd[0]}  → pkg install {cmd[0]}")
            sys.exit(1)
    if result.returncode != 0:
        log_err(f"Échec ({result.returncode}) : {label}")
        if not DEBUG_MODE and hasattr(result, "stderr") and result.stderr:
            for line in result.stderr.decode("utf-8", errors="replace").splitlines()[-15:]:
                print(f"    {C.RED}{line}{C.R}", file=sys.stderr)
        result.check_returncode()
    return result

def cmake_build(inp: BlutterInput):
    blutter_src = os.path.join(SCRIPT_DIR, "blutter")
    build_dir   = os.path.join(BUILD_DIR, inp.blutter_name)
    macros      = find_compat_macros(inp.dart_info.version, inp.no_analysis, inp.ida_fcn)

    extra_env = None
    if platform.system() == "Darwin":
        try:
            mac_major = int(platform.mac_ver()[0].split(".")[0])
            if mac_major < 15:
                prefix = subprocess.run(
                    ["brew", "--prefix", "llvm@16"],
                    capture_output=True, check=True
                ).stdout.decode().strip()
                clang = os.path.join(prefix, "bin", "clang")
                extra_env = {**os.environ, "CC": clang, "CXX": clang + "++"}
        except Exception:
            pass

    log_info("CMake configure…")
    _run_cmd(
        [CMAKE_CMD, "-GNinja", "-B", build_dir,
         f"-DDARTLIB={inp.dart_info.lib_name}",
         f"-DNAME_SUFFIX={inp.name_suffix}",
         "-DCMAKE_BUILD_TYPE=Release", "--log-level=NOTICE"] + macros,
        "CMake configure", cwd=blutter_src, env=extra_env,
    )
    log_info("Ninja build…")
    _run_cmd([NINJA_CMD], "Ninja build", cwd=build_dir)
    log_info("CMake install…")
    _run_cmd([CMAKE_CMD, "--install", "."], "CMake install", cwd=build_dir)
    log_ok("Build terminé !")


# ═══════════════════════════════════════════════════════════════
#  INFOS DART
# ═══════════════════════════════════════════════════════════════
def get_dart_lib_info(libapp: str, libflutter: str):
    try:
        from extract_dart_info import extract_dart_info
    except ImportError:
        log_err("extract_dart_info.py introuvable — doit être dans le même dossier.")
        sys.exit(1)

    dart_version, snapshot_hash, flags, arch, os_name = extract_dart_info(libapp, libflutter)

    log_section("DART INFO", sub=True)
    print(f"  {C.BCYN}Version  {C.R}: {C.BWHT}{dart_version}{C.R}")
    print(f"  {C.BCYN}Snapshot {C.R}: {C.DIM}{snapshot_hash}{C.R}")
    print(f"  {C.BCYN}Cible    {C.R}: {os_name} / {arch}")
    print(f"  {C.BCYN}Flags    {C.R}: {' '.join(flags) or C.DIM+'none'+C.R}")

    return DartLibInfo(dart_version, os_name, arch,
                       "compressed-pointers" in flags, snapshot_hash)


# ═══════════════════════════════════════════════════════════════
#  BUILD + RUN
# ═══════════════════════════════════════════════════════════════
def _show_output_summary(outdir: str):
    print()
    for name in ["asm", "blutter_frida.js", "objs.txt", "pp.txt"]:
        path = os.path.join(outdir, name)
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            count = len(os.listdir(path))
            print(f"  {C.BGRN}◈{C.R}  {C.BCYN}{name}/{C.R}  {C.DIM}({count} fichiers){C.R}")
        else:
            size = os.path.getsize(path)
            print(f"  {C.BGRN}◈{C.R}  {C.BCYN}{name}{C.R}  {C.DIM}({size/1024:.1f} KB){C.R}")

def build_and_run(inp: BlutterInput):
    lib_ext    = ".lib" if os.name == "nt" else ".a"
    lib_prefix = "" if os.name == "nt" else "lib"
    dart_lib   = os.path.join(PKG_LIB_DIR, f"{lib_prefix}{inp.dart_info.lib_name}{lib_ext}")

    if not os.path.isfile(dart_lib):
        log_info("Téléchargement & compilation Dart VM lib…")
        try:
            from dartvm_fetch_build import fetch_and_build
            with Spinner("fetch & build Dart VM"):
                fetch_and_build(inp.dart_info)
        except ImportError:
            log_err("dartvm_fetch_build.py introuvable.")
            sys.exit(1)
        inp.rebuild = True

    if not os.path.isfile(inp.blutter_file) or inp.rebuild:
        cmake_build(inp)
        if not os.path.isfile(inp.blutter_file):
            log_err(f"Build terminé mais exécutable introuvable :\n  {inp.blutter_file}")
            sys.exit(1)

    if inp.create_vs_sln:
        _gen_vs_sln(inp)
        return

    os.makedirs(inp.outdir, exist_ok=True)
    log_section("ANALYSE EN COURS")
    log_info(f"Exe    : {C.DIM}{os.path.basename(inp.blutter_file)}{C.R}")
    log_info(f"Cible  : {C.DIM}{inp.libapp_path}{C.R}")
    log_info(f"Sortie : {C.DIM}{inp.outdir}{C.R}")
    print()

    subprocess.run(
        [inp.blutter_file, "-i", inp.libapp_path, "-o", inp.outdir],
        check=True,
    )
    log_section("RÉSULTATS")
    log_ok(f"Sortie : {C.BCYN}{inp.outdir}{C.R}")
    _show_output_summary(inp.outdir)

def _gen_vs_sln(inp: BlutterInput):
    macros      = find_compat_macros(inp.dart_info.version, inp.no_analysis, inp.ida_fcn)
    blutter_src = os.path.join(SCRIPT_DIR, "blutter")
    dbg_args    = f"-i {inp.libapp_path} -o {os.path.join(inp.outdir,'out')}"
    vscmd       = os.getenv("VSCMD_VER", "")
    if not vscmd:
        log_err("Solution VS : lancez dans 'x64 Native Tools Command Prompt'.")
        sys.exit(1)
    gen = (
        "Visual Studio 18 2026" if vscmd.startswith("18.") else
        "Visual Studio 17 2022" if vscmd.startswith("17.") else None
    )
    if not gen:
        log_err(f"Version VS non supportée : {vscmd}"); sys.exit(1)
    subprocess.run(
        [CMAKE_CMD, "-G", gen, "-A", "x64", "-B", inp.outdir,
         f"-DDARTLIB={inp.dart_info.lib_name}",
         f"-DNAME_SUFFIX={inp.name_suffix}",
         f"-DDBG_CMD:STRING={dbg_args}"] + macros + [blutter_src],
        check=True,
    )
    log_ok(f"Solution VS dans : {inp.outdir}")


# ═══════════════════════════════════════════════════════════════
#  POINTS D'ENTRÉE ANALYSE
# ═══════════════════════════════════════════════════════════════
def run_with_flutter(libapp, libflutter, outdir,
                     rebuild, vs_sln, no_analysis, ida_fcn):
    dart_info = get_dart_lib_info(libapp, libflutter)
    build_and_run(BlutterInput(libapp, dart_info, outdir,
                               rebuild, vs_sln, no_analysis, ida_fcn))

def run_with_dart_version(libapp, dart_version, outdir,
                          rebuild, vs_sln, no_analysis, ida_fcn):
    parts = dart_version.strip().split("_")
    if len(parts) != 3:
        log_err(f"Format invalide '{dart_version}' — attendu: 3.4.2_android_arm64")
        sys.exit(1)
    version, os_name, arch = parts
    build_and_run(BlutterInput(libapp, DartLibInfo(version, os_name, arch),
                               outdir, rebuild, vs_sln, no_analysis, ida_fcn))

def run(indir, outdir, rebuild, vs_sln, no_analysis, ida_fcn):
    if indir.lower().endswith(".apk"):
        print_apk_info(indir)
        with tempfile.TemporaryDirectory() as tmp:
            app, flutter = extract_libs_from_apk(indir, tmp)
            run_with_flutter(app, flutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn)
    else:
        app, flutter = find_lib_files(indir)
        run_with_flutter(app, flutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn)


# ═══════════════════════════════════════════════════════════════
#  MISE À JOUR GIT
# ═══════════════════════════════════════════════════════════════
def git_update(retries: int = 2):
    """
    Met à jour le dépôt local via git pull.
    Gère : HEAD détaché, conflits de merge, submodules, timeout réseau.
    """
    if not shutil.which("git"):
        log_warn("git introuvable — MàJ ignorée.")
        return

    # Vérifie que SCRIPT_DIR est bien un dépôt git
    git_dir = os.path.join(SCRIPT_DIR, ".git")
    if not os.path.exists(git_dir):
        log_warn("Pas de dépôt git détecté — MàJ ignorée.")
        return

    log_info("Vérification MàJ git…")

    def _git(args, timeout=20) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git"] + args, capture_output=True,
                timeout=timeout, cwd=SCRIPT_DIR,
            )
            out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
            return r.returncode, out
        except subprocess.TimeoutExpired:
            log_warn(f"git {args[0]} : timeout ({timeout}s) — réseau lent ?")
            return -1, ""
        except Exception as e:
            log_dbg(f"git {args[0]} exception : {e}")
            return -1, ""

    # Détection HEAD détaché (fréquent après checkout explicite)
    rc, head = _git(["symbolic-ref", "--short", "HEAD"], timeout=5)
    if rc != 0:
        log_warn("HEAD détaché — git pull ignoré (checkout une branche pour activer les MàJ).")
        return

    # Fetch silencieux
    _git(["fetch", "--quiet"], timeout=20)

    # Vérification conflits locaux (modifications non commitées)
    rc_stat, stat_out = _git(["status", "--porcelain"], timeout=5)
    if rc_stat == 0 and stat_out.strip():
        log_warn("Modifications locales non commitées — git pull ignoré pour éviter les conflits.")
        log_dbg(f"status : {stat_out.strip()}")
        return

    for attempt in range(retries):
        rc_pull, pull_out = _git(["pull", "--ff-only", "--quiet"], timeout=30)
        if rc_pull == 0:
            if "Already up to date." in pull_out or not pull_out.strip():
                log_ok("Dépôt à jour.")
            else:
                log_ok("Mise à jour appliquée.")
                # Met à jour les submodules si présents
                sub_file = os.path.join(SCRIPT_DIR, ".gitmodules")
                if os.path.isfile(sub_file):
                    log_info("Mise à jour des submodules…")
                    _git(["submodule", "update", "--init", "--recursive"], timeout=60)
            return
        # Échec ff-only = divergence
        log_warn(f"pull --ff-only échoué (tentative {attempt+1}/{retries}) : {pull_out.strip()[:80]}")
        time.sleep(1)

    log_warn("MàJ git ignorée — résolvez les conflits manuellement (git status).")


# ═══════════════════════════════════════════════════════════════
#  HELPERS TUI
# ═══════════════════════════════════════════════════════════════
def _ask(prompt: str, default: str = "") -> str:
    """
    Prompt utilisateur. Retourne la valeur saisie, ou `default` si vide.
    Ne retourne JAMAIS None.
    """
    hint = f" {C.DIM}[{default}]{C.R}" if default else ""
    try:
        val = input(f"  {C.BCYN}▸{C.R} {prompt}{hint} : ").strip()
        return val if val else default
    except EOFError:
        return default


def _confirm(prompt: str, default: bool = True) -> bool:
    """
    Question oui/non. Retourne TOUJOURS un bool.
    CORRECTION : ne pollue plus dart_ver avec "n" ou d'autres chaînes.
    """
    hint = f"{C.DIM}[O/n]{C.R}" if default else f"{C.DIM}[o/N]{C.R}"
    try:
        val = input(f"  {C.BCYN}▸{C.R} {prompt} {hint} : ").strip().lower()
    except EOFError:
        return default
    if not val:
        return default
    return val in ("o", "oui", "y", "yes", "1")


def _browse(start: str = ".", dirs_only: bool = True,
            ext_filter=None, title: str = "Naviguer"):
    """
    Explorateur interactif — efface le terminal à chaque page.
    Retourne un chemin str ou None si annulé.
    """
    current = os.path.abspath(
        os.path.expanduser(start) if start.startswith("~") else start
    )

    while True:
        _clear()
        print_banner()
        log_section(title)
        print(f"  {C.DIM}{C.BCYN}emplacement :{C.R}  {current}\n")

        try:
            entries = sorted(os.scandir(current),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            log_err(f"Accès refusé : {current}")
            current = os.path.dirname(current)
            continue

        items = [("0", "..", None, True)]
        print(f"  {C.DIM}0{C.R}  {C.BCYN}↑  ..  (dossier parent){C.R}")

        idx = 1
        for entry in entries:
            is_dir = entry.is_dir()
            ext    = os.path.splitext(entry.name)[1].lower()
            if not is_dir and ext_filter and ext not in ext_filter:
                continue
            if is_dir:
                print(f"  {C.DIM}{idx}{C.R}  {C.BCYN}▸ {entry.name}/{C.R}")
            else:
                try:
                    sz = f"{os.path.getsize(entry.path)/1024/1024:.1f} MB"
                except Exception:
                    sz = "?"
                print(f"  {C.DIM}{idx}{C.R}  {C.BGRN}  {entry.name}{C.R}  {C.DIM}{sz}{C.R}")
            items.append((str(idx), entry.name, entry.path, is_dir))
            idx += 1

        print()
        cmds = []
        if dirs_only:
            cmds.append(f"{C.BCYN}S{C.R} sélectionner ici")
        cmds += [f"{C.BCYN}P{C.R} saisir chemin", f"{C.BCYN}Q{C.R} annuler"]
        print(f"  {C.DIM}[{' · '.join(cmds)}]{C.R}")

        choice = _ask("Choix").upper()

        if choice == "Q":
            return None
        if choice == "S" and dirs_only:
            return current
        if choice == "P":
            path = _ask("Chemin complet")
            path = os.path.expanduser(path.strip())
            if os.path.exists(path):
                return os.path.abspath(path)
            log_warn(f"Introuvable : {path}")
            input(f"  {C.DIM}[Entrée]{C.R}")
            continue

        try:
            n = int(choice)
        except ValueError:
            log_warn("Entrée invalide.")
            input(f"  {C.DIM}[Entrée]{C.R}")
            continue

        match = next((it for it in items if it[0] == str(n)), None)
        if not match:
            log_warn("Numéro hors plage.")
            input(f"  {C.DIM}[Entrée]{C.R}")
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


# ═══════════════════════════════════════════════════════════════
#  MODE INTERACTIF PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def interactive_mode():
    """
    Flux TUI :
      clear → bannière → check_deps (silencieux)
      → menu principal → cible → outdir → options → récap
      → git_update (optionnel) → run → historique
    """
    _acquire_lock()
    _clear()
    print_banner()

    # ── Vérif dépendances silencieuse ─────────────────────────
    if not check_dependencies(silent=True):
        log_warn("Certaines dépendances manquantes (tapez D pour détails)")
        print()

    # ═══ MENU PRINCIPAL ═══════════════════════════════════════
    def _show_main_menu():
        log_section("CH-BLUTTER  ·  MENU PRINCIPAL")
        print(f"  {C.BCYN}1{C.R}  {C.B}APK{C.R}                  Naviguer et sélectionner un .apk")
        print(f"  {C.BCYN}2{C.R}  {C.B}Dossier de libs{C.R}      arm64-v8a, armeabi-v7a…")
        print(f"  {C.BCYN}3{C.R}  {C.B}Chemin manuel{C.R}         Saisir directement le chemin")
        print(f"  {C.BCYN}H{C.R}  {C.DIM}Historique des analyses{C.R}")
        print(f"  {C.BCYN}D{C.R}  {C.DIM}Vérifier les dépendances{C.R}")
        print(f"  {C.BCYN}Q{C.R}  {C.DIM}Quitter{C.R}")
        print()

    _show_main_menu()
    indir = None

    while indir is None:
        mode = _ask("Choix", "1").upper()

        if mode == "Q":
            print(f"\n  {C.DIM}À bientôt.{C.R}\n")
            _release_lock()
            sys.exit(0)

        if mode == "H":
            show_history()
            input(f"\n  {C.DIM}[Entrée pour continuer]{C.R}")
            _clear(); print_banner(); _show_main_menu()
            continue

        if mode == "D":
            check_dependencies(silent=False)
            input(f"\n  {C.DIM}[Entrée pour continuer]{C.R}")
            _clear(); print_banner(); _show_main_menu()
            continue

        if mode == "1":
            home = os.path.expanduser("~")
            indir = _browse(start=home, dirs_only=False,
                            ext_filter=[".apk"], title="Sélectionner l'APK")

        elif mode == "2":
            home = os.path.expanduser("~")
            indir = _browse(start=home, dirs_only=True,
                            title="Sélectionner le dossier des libs")

        elif mode == "3":
            raw = _ask("Chemin de la cible (APK ou dossier)")
            if raw.strip():
                expanded = os.path.abspath(os.path.expanduser(raw.strip()))
                if os.path.exists(expanded):
                    indir = expanded
                else:
                    log_warn(f"Introuvable : {expanded}")
                    input(f"  {C.DIM}[Entrée]{C.R}")
        else:
            log_warn("Choix invalide.  1 · 2 · 3 · H · D · Q")

        # Si l'explorateur renvoie None (annulé), on réaffiche le menu
        if indir is None and mode in ("1", "2"):
            _clear(); print_banner(); _show_main_menu()

    if not os.path.exists(indir):
        log_err(f"Cible introuvable : {indir}")
        sys.exit(1)

    # ─── Dossier de sortie ─────────────────────────────────────
    _clear()
    print_banner()
    log_ok(f"Cible sélectionnée : {C.BCYN}{indir}{C.R}")

    log_section("DOSSIER DE SORTIE")
    # Suggestion : dossier parent de la cible + "blutter_out"
    default_out = os.path.join(os.path.dirname(indir.rstrip("/\\")), "blutter_out")
    outdir_raw  = _ask("Dossier de sortie", default_out)
    outdir      = os.path.abspath(os.path.expanduser(outdir_raw.strip()))

    if not os.path.exists(outdir):
        os.makedirs(outdir, exist_ok=True)
        log_ok(f"Créé : {outdir}")

    # ─── Options ───────────────────────────────────────────────
    log_section("OPTIONS")
    print(f"  {C.DIM}Appuyez sur Entrée pour garder la valeur par défaut (entre crochets).{C.R}\n")

    rebuild     = _confirm("--rebuild      force recompilation",          default=False)
    no_analysis = _confirm("--no-analysis  désactiver l'analyse Dart",   default=False)
    ida_fcn     = _confirm("--ida-fcn      noms de fonctions pour IDA",  default=False)
    do_update   = _confirm("Vérifier les MàJ git avant analyse",         default=True)

    print()
    # ─ Version Dart : prompt SÉPARÉ avec default VIDE ─
    # Jamais pollué par la réponse à la question précédente
    print(f"  {C.DIM}Version Dart manuelle (laisser vide pour auto-detect){C.R}")
    dart_ver_raw = _ask("  --dart-version", "")
    # Nettoyage strict : vide → None, jamais "n" ou autre artefact
    dart_ver = dart_ver_raw.strip() if dart_ver_raw.strip() else None

    # ─── Récap ─────────────────────────────────────────────────
    _clear()
    print_banner()
    log_section("CONFIGURATION FINALE")

    def _yn(v: bool) -> str:
        return f"{C.BGRN}OUI{C.R}" if v else f"{C.DIM}non{C.R}"

    print(f"  {C.BCYN}Cible         {C.R}: {C.BWHT}{indir}{C.R}")
    print(f"  {C.BCYN}Sortie        {C.R}: {C.BWHT}{outdir}{C.R}")
    print(f"  {C.BCYN}Rebuild       {C.R}: {_yn(rebuild)}")
    print(f"  {C.BCYN}No-analysis   {C.R}: {_yn(no_analysis)}")
    print(f"  {C.BCYN}IDA fcn       {C.R}: {_yn(ida_fcn)}")
    print(f"  {C.BCYN}MàJ git       {C.R}: {_yn(do_update)}")
    print(f"  {C.BCYN}Dart version  {C.R}: {dart_ver or C.DIM+'auto-detect'+C.R}")
    print()

    if not _confirm("Lancer l'analyse ?", default=True):
        print(f"\n  {C.DIM}Analyse annulée.{C.R}\n")
        _release_lock()
        sys.exit(0)

    # ─── Lancement ─────────────────────────────────────────────
    _clear()
    print_banner()

    if do_update:
        git_update()

    success = True
    try:
        if dart_ver:
            run_with_dart_version(indir, dart_ver, outdir,
                                  rebuild, False, no_analysis, ida_fcn)
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
    status  = f"{C.BGRN}SUCCÈS{C.R}" if success else f"{C.BRED}ÉCHEC{C.R}"
    print(f"\n  {status}  ·  Durée : {C.BCYN}{elapsed}{C.R}\n")


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":

    if "--debug" in sys.argv:
        DEBUG_MODE = True
        sys.argv.remove("--debug")

    if len(sys.argv) == 1:
        interactive_mode()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog="ch-blutter",
        description="Flutter Reverse Engineering — ARM64/ARM32 · Dart VM",
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
    parser.add_argument("indir",  nargs="?")
    parser.add_argument("outdir", nargs="?")
    parser.add_argument("--dart-version")
    parser.add_argument("--rebuild",       action="store_true")
    parser.add_argument("--vs-sln",        action="store_true")
    parser.add_argument("--no-analysis",   action="store_true")
    parser.add_argument("--ida-fcn",       action="store_true")
    parser.add_argument("--no-update",     action="store_true")
    parser.add_argument("--debug",         action="store_true")
    parser.add_argument("--check-deps",    action="store_true")
    parser.add_argument("--history",       action="store_true")

    args = parser.parse_args()
    if args.debug:
        DEBUG_MODE = True

    if args.check_deps:
        _clear(); print_banner()
        check_dependencies(silent=False)
        sys.exit(0)

    if args.history:
        _clear(); print_banner()
        show_history()
        sys.exit(0)

    if not args.indir or not args.outdir:
        parser.error("<indir> et <outdir> sont obligatoires.")

    if not os.path.exists(args.indir):
        log_err(f"Cible introuvable : {args.indir}")
        sys.exit(1)

    _clear()
    print_banner()
    _acquire_lock()
    os.makedirs(args.outdir, exist_ok=True)

    if not args.no_update:
        git_update()

    success = True
    try:
        if args.dart_version:
            run_with_dart_version(args.indir, args.dart_version, args.outdir,
                                  args.rebuild, args.vs_sln,
                                  args.no_analysis, args.ida_fcn)
        else:
            run(args.indir, args.outdir,
                args.rebuild, args.vs_sln, args.no_analysis, args.ida_fcn)
    except SystemExit as e:
        if e.code not in (0, None):
            success = False
    except KeyboardInterrupt:
        log_warn("Interrompu.")
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
        "dart_version": args.dart_version or "auto",
        "success":      success,
    })

    elapsed = _fmt_elapsed()
    log_ok(f"Session terminée en {elapsed}") if success else \
        log_err(f"Session échouée après {elapsed}")
    if not success:
        sys.exit(1)
