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
VERSION      = "4.2.0" # Version améliorée
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
    print(f"  {C.BGRN}[+] {msg}{C.R}")

def log_info(msg: str):
    print(f"  {C.BCYN}[*] {msg}{C.R}")

def log_warn(msg: str):
    print(f"  {C.BYLW}[!] {msg}{C.R}", file=sys.stderr)

def log_err(msg: str):
    print(f"  {C.BRED}[-] {msg}{C.R}", file=sys.stderr)

def log_dbg(msg: str):
    if DEBUG_MODE:
        print(f"  {C.DIM}[D] {msg}{C.R}")

def log_section(title: str):
    w = _term_width()
    print(f"\n{C.DIM}{C.CYN}═{'═' * (w-2)}═{C.R}")
    pad = max(0, (w - len(_strip_ansi(title)) - 4) // 2)
    print(f"{C.BCYN}{' ' * pad}  {title}  {C.R}")
    print(f"{C.DIM}{C.CYN}═{'═' * (w-2)}═{C.R}\n")

# ═══════════════════════════════════════════════════════════════
#  BANNIÈRE CYBERPUNK (Fixée)
# ═══════════════════════════════════════════════════════════════
def _clear():
    os.system("cls" if os.name == "nt" else "clear")

_LOGO = r"""
  ██████╗ ██╗     ██╗   ██╗████████╗████████╗███████╗██████╗ 
  ██╔══██╗██║     ██║   ██║╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
  ██████╔╝██║     ██║   ██║   ██║      ██║   █████╗  ██████╔╝
  ██╔══██╗██║     ██║   ██║   ██║      ██║   ██╔══╝  ██╔══██╗
  ██████╔╝███████╗╚██████╔╝   ██║      ██║   ███████╗██║  ██║
  ╚═════╝ ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝
"""

def print_banner():
    w = _term_width()
    bar = f"{C.DIM}{C.GRN}{'─' * w}{C.R}"
    print(bar)
    for line in _LOGO.strip("\n").split("\n"):
        print(f"{C.BGRN}{line}{C.R}")
    
    env = f"{C.MAG}Termux{C.R}" if IS_TERMUX else f"{C.BCYN}{platform.system()}{C.R}"
    print()
    print(f"  {C.BCYN}◈{C.R} {C.B}Flutter Reverse Engineering{C.R}  "
          f"{C.DIM}v{VERSION}{C.R}  ·  {env}  ·  {C.DIM}{platform.machine()}{C.R}")
    print(f"  {C.DIM}◈ Auto-Detection · Dart VM · ARM64/ARM32{C.R}")
    print(bar + "\n")

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
            print(f"\r  {C.BCYN}{f}{C.R}  {self.label}...", end="", flush=True)
            time.sleep(0.07)
            i += 1

    def __enter__(self):
        if sys.stdout.isatty(): self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread.is_alive(): self._thread.join(timeout=0.5)
        if sys.stdout.isatty(): print(f"\r{' ' * (len(self.label) + 14)}\r", end="", flush=True)

def _acquire_lock():
    try: Path(LOCK_FILE).write_text(str(os.getpid()))
    except OSError: pass

def _release_lock():
    try:
        if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
    except OSError: pass

atexit.register(_release_lock)

# ═══════════════════════════════════════════════════════════════
#  DÉPENDANCES
# ═══════════════════════════════════════════════════════════════
def check_dependencies(silent: bool = False) -> bool:
    # Réduction au minimum silencieux pour plus de vitesse
    required = ["cmake", "ninja", "git"]
    missing = [cmd for cmd in required if shutil.which(cmd) is None]
    if missing and not silent:
        log_err(f"Dépendances manquantes : {', '.join(missing)}")
    return len(missing) == 0

# ═══════════════════════════════════════════════════════════════
#  RECHERCHE ET SÉLECTION DES LIBS (.so)
# ═══════════════════════════════════════════════════════════════
def _ask_user_for_so(options: list, title: str) -> str:
    """Demande à l'utilisateur de choisir parmi une liste de fichiers .so."""
    print(f"\n  {C.BYLW}[?] {title}{C.R}")
    print(f"  {C.DIM}Plusieurs bibliothèques détectées. Le fichier AOT Dart (libapp) est souvent le plus volumineux.{C.R}")
    
    for i, (name, size) in enumerate(options):
        size_mb = size / 1024 / 1024
        color = C.BGRN if i == 0 else C.BCYN
        print(f"  {C.DIM}[{i+1}]{C.R} {color}{name}{C.R} {C.DIM}({size_mb:.2f} MB){C.R}")
    
    while True:
        try:
            choice = input(f"\n  {C.BCYN}▸{C.R} Entrez le numéro (1-{len(options)}) : ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print(f"  {C.RED}Choix invalide.{C.R}")

def _find_flutter_lib(directory: str):
    for name in FLUTTER_LIB_NAMES:
        p = os.path.join(directory, name)
        if os.path.isfile(p): return os.path.abspath(p)
    return None

def find_lib_files(indir: str):
    """Recherche dans un dossier décompressé."""
    flutter = _find_flutter_lib(indir)
    
    if not flutter:
        for arch in ARM_ARCH_DIRS:
            cand = os.path.join(indir, arch)
            if os.path.isdir(cand):
                flutter = _find_flutter_lib(cand)
                if flutter:
                    indir = cand
                    break

    if not flutter:
        log_err("libflutter.so introuvable dans le répertoire.")
        sys.exit(1)

    # Trouver l'application
    app = None
    for name in APP_LIB_KNOWN_NAMES:
        p = os.path.join(indir, name)
        if os.path.isfile(p):
            app = os.path.abspath(p)
            break
            
    if not app:
        # Chercher tous les autres .so
        other_so = []
        for f in os.listdir(indir):
            if f.endswith(".so") and os.path.join(indir, f) != flutter:
                size = os.path.getsize(os.path.join(indir, f))
                other_so.append((f, size))
        
        if len(other_so) == 1:
            app = os.path.abspath(os.path.join(indir, other_so[0][0]))
            log_info(f"Auto-détection de l'app : {other_so[0][0]}")
        elif len(other_so) > 1:
            other_so.sort(key=lambda x: x[1], reverse=True) # Trier par taille décroissante
            chosen = _ask_user_for_so(other_so, "Sélectionnez la bibliothèque AOT Dart (libapp) :")
            app = os.path.abspath(os.path.join(indir, chosen))

    if not app:
        log_err("Aucune bibliothèque cible (libapp.so) trouvée.")
        sys.exit(1)

    return app, flutter

def extract_libs_from_apk(apk_path: str, tmp_dir: str):
    """Extrait flutter et libapp (ou demande de choisir) depuis un APK."""
    flutter_lower = {n.lower() for n in FLUTTER_LIB_NAMES}
    app_known_lower = {n.lower() for n in APP_LIB_KNOWN_NAMES}
    
    with zipfile.ZipFile(apk_path, "r") as zf:
        names = zf.namelist()
        
        # 1. Chercher Flutter et déterminer l'architecture
        target_arch = None
        fl_info = None
        for arch in ARM_ARCH_DIRS:
            prefix = f"lib/{arch}/"
            for fn in FLUTTER_LIB_NAMES:
                if prefix + fn in names:
                    fl_info = zf.getinfo(prefix + fn)
                    target_arch = arch
                    break
            if target_arch: break
            
        if not fl_info:
            log_err("libflutter.so introuvable dans l'APK.")
            sys.exit(1)
            
        prefix = f"lib/{target_arch}/"
        
        # 2. Chercher libapp.so
        app_info = None
        for an in APP_LIB_KNOWN_NAMES:
            if prefix + an in names:
                app_info = zf.getinfo(prefix + an)
                break
                
        # 3. Si libapp.so introuvable par nom, lister les autres .so
        if not app_info:
            other_so = []
            for n in names:
                if n.startswith(prefix) and n.endswith(".so") and os.path.basename(n).lower() not in flutter_lower:
                    other_so.append((n, zf.getinfo(n).file_size))
                    
            if len(other_so) == 1:
                app_info = zf.getinfo(other_so[0][0])
                log_info(f"Auto-détection de l'app : {os.path.basename(app_info.filename)}")
            elif len(other_so) > 1:
                other_so.sort(key=lambda x: x[1], reverse=True)
                choices = [(os.path.basename(n), s) for n, s in other_so]
                chosen_base = _ask_user_for_so(choices, "Sélectionnez la bibliothèque AOT Dart de l'APK :")
                for n, s in other_so:
                    if os.path.basename(n) == chosen_base:
                        app_info = zf.getinfo(n)
                        break

        if not app_info:
            log_err("Impossible de trouver la cible AOT dans l'APK.")
            sys.exit(1)

        log_ok(f"Extraction depuis {target_arch}...")
        zf.extract(app_info, tmp_dir)
        zf.extract(fl_info,  tmp_dir)
        return (
            os.path.join(tmp_dir, app_info.filename),
            os.path.join(tmp_dir, fl_info.filename)
        )

# ═══════════════════════════════════════════════════════════════
#  DART INFO & CLASSES DE BASE
# ═══════════════════════════════════════════════════════════════
try:
    from dartvm_fetch_build import DartLibInfo
except ImportError:
    class DartLibInfo:  # type: ignore
        def __init__(self, version, os_name, arch, has_compressed_ptrs=None, snapshot_hash=None):
            self.version, self.os_name, self.arch = version, os_name, arch
            self.snapshot_hash = snapshot_hash
            self.has_compressed_ptrs = has_compressed_ptrs if has_compressed_ptrs is not None else (os_name != "ios")
            self.lib_name = f"dartvm{version}_{os_name}_{arch}"

class BlutterInput:
    def __init__(self, libapp_path, dart_info, outdir, rebuild, create_vs_sln, no_analysis, ida_fcn):
        self.libapp_path = libapp_path
        self.dart_info   = dart_info
        self.outdir      = outdir
        self.rebuild     = rebuild
        self.create_vs_sln = create_vs_sln
        self.ida_fcn     = ida_fcn

        vers = dart_info.version.split(".", 2)
        if int(vers[0]) == 2 and int(vers[1]) < 15:
            no_analysis = True
        self.no_analysis = no_analysis

        suffix = ""
        if not dart_info.has_compressed_ptrs: suffix += "_no-compressed-ptrs"
        if no_analysis: suffix += "_no-analysis"
        if ida_fcn: suffix += "_ida-fcn"

        self.name_suffix  = suffix
        self.blutter_name = f"blutter_{dart_info.lib_name}{suffix}"
        self.blutter_file = os.path.join(BIN_DIR, self.blutter_name) + (".exe" if os.name == "nt" else "")

# (Les fonctions find_compat_macros, cmake_build, get_dart_lib_info, build_and_run sont standard)
def find_compat_macros(dart_version: str, no_analysis: bool, ida_fcn: bool) -> list:
    macros, vm_dir = [], os.path.join(PKG_INC_DIR, f"dartvm{dart_version}", "vm")
    def _scan(filename: str, checks: list):
        path = os.path.join(vm_dir, filename)
        if not os.path.isfile(path): return
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            for needle, macro in checks:
                if (mm.find(needle) != -1) if isinstance(needle, bytes) else needle(mm): macros.append(macro)
            mm.close()

    _scan("class_id.h", [
        (b"V(LinkedHashMap)", "-DOLD_MAP_SET_NAME=1"),
        (lambda mm: mm.find(b"V(LinkedHashMap)") != -1 and mm.find(b"V(ImmutableLinkedHashMap)") == -1, "-DOLD_MAP_NO_IMMUTABLE=1"),
        (lambda mm: mm.find(b" kLastInternalOnlyCid ") == -1, "-DNO_LAST_INTERNAL_ONLY_CID=1"),
        (b"V(TypeRef)", "-DHAS_TYPE_REF=1"),
    ])
    try:
        if int(dart_version.split(".")[0]) >= 3: _scan("class_id.h", [(b"V(RecordType)", "-DHAS_RECORD_TYPE=1")])
    except: pass
    _scan("class_table.h", [(b"class SharedClassTable {", "-DHAS_SHARED_CLASS_TABLE=1")])
    _scan("stub_code_list.h", [(lambda mm: mm.find(b"V(InitLateStaticField)") == -1, "-DNO_INIT_LATE_STATIC_FIELD=1")])
    _scan("object_store.h", [(lambda mm: mm.find(b"build_generic_method_extractor_code)") == -1, "-DNO_METHOD_EXTRACTOR_STUB=1")])
    _scan("object.h", [(lambda mm: mm.find(b"AsTruncatedInt64Value()") == -1, "-DUNIFORM_INTEGER_ACCESS=1")])

    if no_analysis: macros.append("-DNO_CODE_ANALYSIS=1")
    if ida_fcn: macros.append("-DIDA_FCN=1")
    try:
        if (int(dart_version.split(".")[0]), int(dart_version.split(".")[1])) >= (3, 5):
            macros.append("-DOLD_MARKING_STACK_BLOCK=1")
    except: pass
    return list(set(macros))

def cmake_build(inp: BlutterInput):
    blutter_src = os.path.join(SCRIPT_DIR, "blutter")
    build_dir   = os.path.join(BUILD_DIR, inp.blutter_name)
    macros      = find_compat_macros(inp.dart_info.version, inp.no_analysis, inp.ida_fcn)
    
    log_info("CMake configure...")
    subprocess.run([CMAKE_CMD, "-GNinja", "-B", build_dir, f"-DDARTLIB={inp.dart_info.lib_name}", f"-DNAME_SUFFIX={inp.name_suffix}", "-DCMAKE_BUILD_TYPE=Release", "--log-level=NOTICE"] + macros, cwd=blutter_src, capture_output=True, check=True)
    log_info("Ninja build...")
    subprocess.run([NINJA_CMD], cwd=build_dir, capture_output=True, check=True)
    subprocess.run([CMAKE_CMD, "--install", "."], cwd=build_dir, capture_output=True, check=True)

def get_dart_lib_info(libapp: str, libflutter: str):
    from extract_dart_info import extract_dart_info
    dart_version, snapshot_hash, flags, arch, os_name = extract_dart_info(libapp, libflutter)
    print(f"  {C.BCYN}Dart Version{C.R} : {dart_version} ({os_name}_{arch})")
    return DartLibInfo(dart_version, os_name, arch, "compressed-pointers" in flags, snapshot_hash)

def build_and_run(inp: BlutterInput):
    lib_ext = ".lib" if os.name == "nt" else ".a"
    lib_prefix = "" if os.name == "nt" else "lib"
    dart_lib = os.path.join(PKG_LIB_DIR, f"{lib_prefix}{inp.dart_info.lib_name}{lib_ext}")

    if not os.path.isfile(dart_lib):
        log_info("Téléchargement & compilation Dart VM lib...")
        from dartvm_fetch_build import fetch_and_build
        with Spinner("Build Dart VM"):
            fetch_and_build(inp.dart_info)
        inp.rebuild = True

    if not os.path.isfile(inp.blutter_file) or inp.rebuild:
        with Spinner("Build Blutter Executable"):
            cmake_build(inp)

    os.makedirs(inp.outdir, exist_ok=True)
    log_info("Analyse en cours...")
    subprocess.run([inp.blutter_file, "-i", inp.libapp_path, "-o", inp.outdir], check=True)
    log_ok(f"Terminé. Résultats dans : {inp.outdir}")

def run_with_flutter(libapp, libflutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn):
    dart_info = get_dart_lib_info(libapp, libflutter)
    build_and_run(BlutterInput(libapp, dart_info, outdir, rebuild, vs_sln, no_analysis, ida_fcn))

def run(indir, outdir, rebuild, vs_sln, no_analysis, ida_fcn):
    if indir.lower().endswith(".apk"):
        with tempfile.TemporaryDirectory() as tmp:
            app, flutter = extract_libs_from_apk(indir, tmp)
            run_with_flutter(app, flutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn)
    else:
        app, flutter = find_lib_files(indir)
        run_with_flutter(app, flutter, outdir, rebuild, vs_sln, no_analysis, ida_fcn)

# ═══════════════════════════════════════════════════════════════
#  TUI MINIMAL & RAPIDE
# ═══════════════════════════════════════════════════════════════
def _ask(prompt: str, default: str = "") -> str:
    hint = f" {C.DIM}[{default}]{C.R}" if default else ""
    val = input(f"  {C.BCYN}▸{C.R} {prompt}{hint} : ").strip()
    return val if val else default

def _browse(start: str = ".", ext_filter=None, title: str = "Naviguer"):
    current = os.path.abspath(os.path.expanduser(start))
    while True:
        _clear()
        print_banner()
        log_section(title)
        print(f"  {C.BCYN}Dossier actuel :{C.R} {current}\n")
        
        try: entries = sorted(os.scandir(current), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError: 
            current = os.path.dirname(current)
            continue

        items = [("0", "..", None, True)]
        print(f"  {C.DIM}0{C.R}  {C.BCYN}↑  ..  (Dossier parent){C.R}")

        idx = 1
        for entry in entries:
            is_dir = entry.is_dir()
            if not is_dir and ext_filter and not any(entry.name.lower().endswith(ext) for ext in ext_filter):
                continue
            color = C.CYN if is_dir else C.GRN
            print(f"  {C.DIM}{idx}{C.R}  {color}{entry.name}{'/' if is_dir else ''}{C.R}")
            items.append((str(idx), entry.name, entry.path, is_dir))
            idx += 1

        print(f"\n  {C.DIM}[S: Sélectionner ce dossier · P: Chemin manuel · Q: Quitter]{C.R}")
        choice = _ask("Choix").upper()

        if choice == "Q": sys.exit(0)
        if choice == "S": return current
        if choice == "P":
            path = _ask("Chemin")
            if os.path.exists(path): return os.path.abspath(path)
            continue

        match = next((it for it in items if it[0] == choice), None)
        if match:
            if match[1] == "..": current = os.path.dirname(current)
            elif match[3]: current = match[2]
            elif ext_filter: return match[2]

def interactive_mode():
    _acquire_lock()
    _clear()
    print_banner()

    log_section("SÉLECTION DE LA CIBLE")
    print(f"  {C.BCYN}1{C.R}  Analyser un fichier {C.BGRN}.APK{C.R}")
    print(f"  {C.BCYN}2{C.R}  Analyser un {C.BCYN}dossier{C.R} (contenant les .so)")
    print(f"  {C.BCYN}Q{C.R}  Quitter\n")

    mode = _ask("Choix", "1").upper()
    if mode == "Q": sys.exit(0)

    home = os.path.expanduser("~")
    indir = None
    if mode == "1":
        indir = _browse(start=home, ext_filter=[".apk"], title="SÉLECTIONNER L'APK")
    else:
        indir = _browse(start=home, title="SÉLECTIONNER LE DOSSIER")

    # Calcul automatique du dossier de sortie
    outdir = os.path.join(os.path.dirname(indir), "blutter_out")
    
    _clear()
    print_banner()
    log_section("RÉSUMÉ ET LANCEMENT")
    print(f"  {C.BCYN}Cible  :{C.R} {indir}")
    print(f"  {C.BCYN}Sortie :{C.R} {outdir}\n")
    
    # Lancement Automatique (comme l'original)
    log_info("Démarrage automatique de l'analyse avec les paramètres standards...")
    time.sleep(1)
    
    try:
        run(indir, outdir, rebuild=False, vs_sln=False, no_analysis=False, ida_fcn=False)
    except Exception as e:
        log_err(f"Erreur fatale : {e}")

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) == 1:
        interactive_mode()
        sys.exit(0)

    parser = argparse.ArgumentParser()
    parser.add_argument("indir",  nargs="?")
    parser.add_argument("outdir", nargs="?")
    parser.add_argument("--rebuild",       action="store_true")
    parser.add_argument("--vs-sln",        action="store_true")
    parser.add_argument("--no-analysis",   action="store_true")
    parser.add_argument("--ida-fcn",       action="store_true")

    args = parser.parse_args()
    if not args.indir or not args.outdir:
        parser.error("Indiquez la cible et le dossier de sortie.")

    _acquire_lock()
    print_banner()
    run(args.indir, args.outdir, args.rebuild, args.vs_sln, args.no_analysis, args.ida_fcn)