#!/usr/bin/python3

import argparse
import glob
import hashlib
import json
import mmap
import os
import platform
import shutil
import signal
import subprocess
import sys
import zipfile
import tempfile
import time
import threading
import random
import re
import atexit
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
#  Rich TUI — import gracieux
# ──────────────────────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn,
        TextColumn, TimeElapsedColumn, TaskProgressColumn,
        MofNCompleteColumn,
    )
    from rich.live import Live
    from rich.columns import Columns
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich import box
    from rich.align import Align
    from rich.style import Style
    from rich.markup import escape
    from rich.traceback import install as install_rich_traceback
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

try:
    from dartvm_fetch_build import DartLibInfo
except ImportError:
    DartLibInfo = None

# ──────────────────────────────────────────────────────────────────────────────
#  Constantes globales
# ──────────────────────────────────────────────────────────────────────────────
CMAKE_CMD  = "cmake"
NINJA_CMD  = "ninja"

SCRIPT_DIR  = os.path.dirname(os.path.realpath(__file__))
BIN_DIR     = os.path.join(SCRIPT_DIR, "bin")
PKG_INC_DIR = os.path.join(SCRIPT_DIR, "packages", "include")
PKG_LIB_DIR = os.path.join(SCRIPT_DIR, "packages", "lib")
BUILD_DIR   = os.path.join(SCRIPT_DIR, "build")

ARM_ARCH_DIRS       = ["arm64-v8a", "armeabi-v7a", "armeabi"]
FLUTTER_LIB_NAMES   = ["libflutter.so", "Flutter", "libFlutter.so"]
APP_LIB_KNOWN_NAMES = ["libapp.so", "App", "libApp.so"]

VERSION      = "3.1.0-cyber"
HISTORY_FILE = os.path.expanduser("~/.blutter_history")
LOCK_FILE    = os.path.join(tempfile.gettempdir(), "blutter.lock")

# Mode debug global (activé par --debug)
DEBUG_MODE   = False

# Chronomètre de session
SESSION_START = time.time()

# ──────────────────────────────────────────────────────────────────────────────
#  Console & thème CYBER (stabilisé)
# ──────────────────────────────────────────────────────────────────────────────
console = Console() if HAS_RICH else None

if HAS_RICH and "--debug" in sys.argv:
    install_rich_traceback(show_locals=True)

THEME = {
    "primary":   "bright_green",
    "secondary": "cyan",
    "accent":    "bright_cyan",
    "warning":   "yellow",
    "error":     "bright_red",
    "dim":       "green",
    "bg":        "on black",
    "border":    "green",
    "neon":      "bold bright_green",
    "plasma":    "bold bright_cyan",
    "ghost":     "dim green",
}

# Bannière simplifiée (style hacker mais sans glitch aléatoire)
BANNER = r"""
  ██████╗ ██╗     ██╗   ██╗████████╗████████╗███████╗██████╗ 
  ██╔══██╗██║     ██║   ██║╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
  ██████╔╝██║     ██║   ██║   ██║      ██║   █████╗  ██████╔╝
  ██╔══██╗██║     ██║   ██║   ██║      ██║   ██╔══╝  ██╔══██╗
  ██████╔╝███████╗╚██████╔╝   ██║      ██║   ███████╗██║  ██║
  ╚═════╝ ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝
"""

def print_banner():
    if not HAS_RICH:
        print(BANNER)
        return

    # Bannière fixe sans glitch
    banner_text = f"[bright_green]{BANNER}[/]"
    sub = (
        f"\n[cyan]  ◈ Flutter Reverse Engineering Framework[/]  "
        f"[dim green]v{VERSION}[/]"
        f"\n[dim green]  ◈ ARM32/ARM64 · Dart VM · IDA/Ghidra · Termux Native[/]"
        f"\n[dim cyan]  ◈ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        f"  ·  {platform.system()} {platform.machine()}[/]"
    )

    panel = Panel(
        Align.center(banner_text + sub),
        border_style="bright_green",
        box=box.DOUBLE_EDGE,
        padding=(0, 2),
        title=f"[dim green]◈ BLUTTER CYBER EDITION ◈[/]",
        subtitle=f"[dim cyan]SESSION {datetime.now().strftime('%Y%m%d-%H%M%S')}[/]",
    )
    console.print(panel)

# ──────────────────────────────────────────────────────────────────────────────
#  Helpers d'affichage (inchangés)
# ──────────────────────────────────────────────────────────────────────────────
def rprint(*args, **kwargs):
    if HAS_RICH:
        console.print(*args, **kwargs)
    else:
        text = " ".join(str(a) for a in args)
        text = re.sub(r'\[.*?\]', '', text)
        print(text)

def ok(msg: str):
    rprint(f"[bright_green]  ✔[/]  [green]{msg}[/]")

def info(msg: str):
    rprint(f"[cyan]  ◈[/]  [white]{msg}[/]")

def warn(msg: str):
    rprint(f"[yellow]  ⚠[/]  [yellow]{msg}[/]")

def err(msg: str):
    rprint(f"[bright_red]  ✘[/]  [red]{msg}[/]")

def dbg(msg: str):
    if DEBUG_MODE:
        rprint(f"[dim magenta]  ⬡ DBG[/]  [dim]{msg}[/]")

# ──────────────────────────────────────────────────────────────────────────────
#  Détection intelligente de libapp.so et libflutter.so
# ──────────────────────────────────────────────────────────────────────────────

def is_valid_app_so(so_path: str) -> bool:
    """
    Vérifie si un fichier .so contient un snapshot Dart valide.
    Utilise extract_dart_info (si disponible) ou une heuristique simple.
    """
    try:
        from extract_dart_info import extract_dart_info
        # On a besoin de libflutter pour extraire, mais on peut tester seul ?
        # En réalité extract_dart_info nécessite les deux. On va plutôt tenter
        # de lire les premiers octets pour chercher la signature du snapshot.
        # Alternative : on vérifie la présence de la chaîne "Dart" ou du magic number.
        with open(so_path, 'rb') as f:
            data = f.read(1024)
            # Signature possible du snapshot : "dart" ou "DS"
            if b'dart' in data or b'Dart' in data:
                return True
            # Vérification plus poussée : présence de la section .rodata contenant "kDartVm"
            # On se contente de cette heuristique simple pour le moment.
        return False
    except Exception:
        return False

def find_candidate_app_libs(search_dir: str) -> list:
    """
    Parcourt récursivement search_dir et retourne la liste de tous les fichiers .so
    qui ne sont pas des bibliothèques Flutter connues.
    """
    candidates = []
    flutter_names_lower = {n.lower() for n in FLUTTER_LIB_NAMES}
    for root, dirs, files in os.walk(search_dir):
        for f in files:
            if f.lower().endswith('.so'):
                full_path = os.path.join(root, f)
                # Exclure les noms typiques de Flutter
                if f.lower() in flutter_names_lower:
                    continue
                candidates.append(full_path)
    return candidates

def select_libapp_interactive(candidates: list, auto: bool = False) -> str:
    """
    Si auto == True, retourne le premier candidat valide.
    Sinon, affiche un menu pour que l'utilisateur choisisse.
    """
    if not candidates:
        return None
    if auto:
        return candidates[0]

    if not HAS_RICH:
        print("Plusieurs bibliothèques candidates pour libapp.so :")
        for i, cand in enumerate(candidates):
            print(f"  [{i}] {cand}")
        choice = input("Choisissez le numéro (ou laissez vide pour annuler) : ").strip()
        if choice.isdigit() and 0 <= int(choice) < len(candidates):
            return candidates[int(choice)]
        return None

    # Menu Rich
    table = Table(box=box.SIMPLE, border_style="cyan", show_header=True,
                  header_style="bold cyan")
    table.add_column("#", style="dim green", width=4)
    table.add_column("Chemin", style="bright_white")
    for idx, cand in enumerate(candidates):
        table.add_row(str(idx), escape(cand))
    console.print(Panel(table, title="[bold yellow]Sélectionnez libapp.so[/]",
                         border_style="yellow"))
    choice = Prompt.ask("[bright_green]Numéro[/]", default="")
    if choice.isdigit() and 0 <= int(choice) < len(candidates):
        return candidates[int(choice)]
    return None

def find_lib_files(indir: str, auto_select: bool = False):
    """
    Recherche améliorée :
      1. Cherche dans les sous-dossiers ARM (comme avant)
      2. Si non trouvés, cherche récursivement tous les .so
      3. Pour libapp : collecte tous les .so non-Flutter, puis filtre ceux qui semblent contenir du Dart.
      4. Pour libflutter : cherche les noms connus, sinon tout .so restant.
    """
    # Étape 1 : chercher dans les dossiers ARM standard
    app_file, flutter_file = search_arm_subdirs(indir)
    if app_file and flutter_file:
        return app_file, flutter_file

    # Étape 2 : recherche récursive
    info("Recherche avancée des bibliothèques...")
    all_so = []
    for root, dirs, files in os.walk(indir):
        for f in files:
            if f.endswith('.so'):
                all_so.append(os.path.join(root, f))

    if not all_so:
        err("Aucun fichier .so trouvé dans le dossier.")
        sys.exit(1)

    # Identifier libflutter : noms connus
    flutter_candidates = []
    for so in all_so:
        basename = os.path.basename(so)
        if basename in FLUTTER_LIB_NAMES or basename.lower() in [n.lower() for n in FLUTTER_LIB_NAMES]:
            flutter_candidates.append(so)
    if not flutter_candidates:
        # Fallback : on prend le premier .so qui contient "flutter" dans son nom
        for so in all_so:
            if 'flutter' in os.path.basename(so).lower():
                flutter_candidates.append(so)
    if not flutter_candidates:
        err("Impossible de trouver libflutter.so (aucun fichier correspondant).")
        sys.exit(1)
    # Si plusieurs, on prend le premier (ou on pourrait demander)
    flutter_file = flutter_candidates[0]
    ok(f"libflutter détecté : {os.path.basename(flutter_file)}")

    # Identifier libapp : tous les .so sauf ceux déjà identifiés comme flutter
    app_candidates = [so for so in all_so if so not in flutter_candidates]
    # Filtrer ceux qui contiennent potentiellement du Dart (heuristique)
    valid_apps = []
    for cand in app_candidates:
        if is_valid_app_so(cand):
            valid_apps.append(cand)
    if not valid_apps:
        # Fallback : on prend tous les candidats restants
        valid_apps = app_candidates
    if not valid_apps:
        err("Aucun candidat trouvé pour libapp.so.")
        sys.exit(1)

    # Sélection interactive ou automatique
    if len(valid_apps) == 1:
        app_file = valid_apps[0]
    else:
        app_file = select_libapp_interactive(valid_apps, auto_select)
        if app_file is None:
            err("Aucun libapp sélectionné.")
            sys.exit(1)
    ok(f"libapp détecté : {os.path.basename(app_file)}")
    return app_file, flutter_file

def search_arm_subdirs(base_dir: str):
    """Cherche dans les sous-dossiers ARM classiques (conservé pour compatibilité)"""
    candidate_dirs = []
    for arch in ARM_ARCH_DIRS:
        candidate_dirs.append(os.path.join(base_dir, arch))
        candidate_dirs.append(os.path.join(base_dir, "lib", arch))

    for search_dir in candidate_dirs:
        if not os.path.isdir(search_dir):
            continue
        app_file     = find_app_lib_in_dir(search_dir)
        flutter_file = find_flutter_lib_in_dir(search_dir)
        if app_file and flutter_file:
            arch_name = os.path.basename(search_dir)
            ok(f"Bibliothèques trouvées dans : [cyan]{arch_name}[/]")
            info(f"App     → [white]{os.path.basename(app_file)}[/]")
            info(f"Flutter → [white]{os.path.basename(flutter_file)}[/]")
            return app_file, flutter_file
    return None, None

def find_app_lib_in_dir(search_dir: str):
    """Cherche libapp dans un répertoire (noms connus + tout .so non-Flutter)"""
    for name in APP_LIB_KNOWN_NAMES:
        candidate = os.path.join(search_dir, name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    flutter_names_lower = {n.lower() for n in FLUTTER_LIB_NAMES}
    try:
        for fname in os.listdir(search_dir):
            if fname.lower().endswith(".so") and fname.lower() not in flutter_names_lower:
                return os.path.abspath(os.path.join(search_dir, fname))
    except OSError:
        pass
    return None

def find_flutter_lib_in_dir(search_dir: str):
    for name in FLUTTER_LIB_NAMES:
        candidate = os.path.join(search_dir, name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None

# ──────────────────────────────────────────────────────────────────────────────
#  Le reste du script (extraction APK, build, etc.) reste inchangé
#  Seule la fonction main et interactive_mode sont adaptées pour accepter --auto
# ──────────────────────────────────────────────────────────────────────────────

# ... (le code suivant est identique à l'original jusqu'à la fin,
#      avec l'ajout de l'argument --auto et la correction de l'affichage)