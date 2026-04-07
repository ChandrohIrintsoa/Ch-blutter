#!/usr/bin/env python3
"""
blutter.py — Flutter Reverse Engineering Framework
Ch-blutter / blutter-termux
ARM64/ARM32 · Dart VM · Termux Native

Usage:
  python blutter.py                          # TUI interactif
  python blutter.py app.apk ./out            # CLI direct
  python blutter.py ./libs/arm64-v8a ./out   # dossier .so
  python blutter.py --check-deps             # vérifier les dépendances
  python blutter.py --history                # historique
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

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
        TextColumn, TimeElapsedColumn,
    )
    from rich.live import Live
    from rich.rule import Rule
    from rich import box
    from rich.align import Align
    from rich.markup import escape
    from rich.traceback import install as install_rich_traceback
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ──────────────────────────────────────────────────────────────────────────────
#  Import modules blutter (gracieux)
# ──────────────────────────────────────────────────────────────────────────────
try:
    from dartvm_fetch_build import DartLibInfo, fetch_and_build, BlutterBuildError
    HAS_DARTVM = True
except ImportError:
    HAS_DARTVM = False
    BlutterBuildError = RuntimeError

try:
    from extract_dart_info import extract_dart_info, BlutterExtractError
    HAS_EXTRACT = True
except ImportError:
    HAS_EXTRACT = False
    BlutterExtractError = RuntimeError

# ──────────────────────────────────────────────────────────────────────────────
#  Constantes
# ──────────────────────────────────────────────────────────────────────────────
VERSION      = "3.2.0-ch"
SCRIPT_DIR   = os.path.dirname(os.path.realpath(__file__))
BIN_DIR      = os.path.join(SCRIPT_DIR, "bin")
BUILD_DIR    = os.path.join(SCRIPT_DIR, "build")
PKG_INC_DIR  = os.path.join(SCRIPT_DIR, "packages", "include")
PKG_LIB_DIR  = os.path.join(SCRIPT_DIR, "packages", "lib")
HISTORY_FILE = os.path.expanduser("~/.chblutter_history")

CMAKE_CMD = os.environ.get("CMAKE", "cmake")
NINJA_CMD = os.environ.get("NINJA", "ninja")

# Architectures ARM standard dans les APK
ARM_ARCH_DIRS = ["arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86"]

# Symboles ELF présents dans le .so Dart (libapp équivalent)
DART_APP_SYMBOLS = [
    "_kDartVmSnapshotData",
    "_kDartIsolateSnapshotData",
    "_kDartVmSnapshotInstructions",
    "_kDartIsolateSnapshotInstructions",
]

# Marqueurs binaires présents dans libflutter
FLUTTER_ENGINE_STRINGS = [
    b"Platform_GetVersion",
    b"Dart_NewStringFromCString",
    b"Dart_SetReturnValue",
    b"libflutter",
    b"io.flutter",
]

DEBUG_MODE    = False
SESSION_START = time.time()

# ──────────────────────────────────────────────────────────────────────────────
#  Console
# ──────────────────────────────────────────────────────────────────────────────
console = Console(highlight=False) if HAS_RICH else None

if HAS_RICH and "--debug" in sys.argv:
    install_rich_traceback(show_locals=True)

BANNER = r"""
  ██████╗██╗  ██╗    ██████╗ ██╗     ██╗   ██╗████████╗████████╗███████╗██████╗
 ██╔════╝██║  ██║    ██╔══██╗██║     ██║   ██║╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
 ██║     ███████║    ██████╔╝██║     ██║   ██║   ██║      ██║   █████╗  ██████╔╝
 ██║     ██╔══██║    ██╔══██╗██║     ██║   ██║   ██║      ██║   ██╔══╝  ██╔══██╗
 ╚██████╗██║  ██║    ██████╔╝███████╗╚██████╔╝   ██║      ██║   ███████╗██║  ██║
  ╚═════╝╚═╝  ╚═╝    ╚═════╝ ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝
"""

# ──────────────────────────────────────────────────────────────────────────────
#  Helpers d'affichage
# ──────────────────────────────────────────────────────────────────────────────

def _strip_markup(text: str) -> str:
    return re.sub(r"\[/?[^\]]*\]", "", text)

def rprint(*args, **kwargs):
    if HAS_RICH:
        console.print(*args, **kwargs)
    else:
        text = " ".join(str(a) for a in args)
        print(_strip_markup(text))

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
        rprint(f"[dim magenta]  ⬡[/]  [dim]{msg}[/]")

def section(title: str):
    if HAS_RICH:
        console.print(Rule(f"[bold cyan] {title} [/]", style="cyan"))
    else:
        print(f"\n{'─'*60}\n  {title}\n{'─'*60}")

def print_banner():
    if not HAS_RICH:
        print(BANNER)
        print(f"  Flutter Reverse Engineering  v{VERSION}")
        print(f"  {platform.system()} {platform.machine()}")
        return
    panel = Panel(
        Align.center(
            f"[bright_green]{BANNER}[/]"
            f"\n[cyan]  ◈ Flutter Reverse Engineering Framework[/]  [dim green]v{VERSION}[/]"
            f"\n[dim green]  ◈ ARM32/ARM64 · Dart VM · Termux Native[/]"
            f"\n[dim cyan]  ◈ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            f"  ·  {platform.system()} {platform.machine()}[/]"
        ),
        border_style="bright_green",
        box=box.DOUBLE_EDGE,
        padding=(0, 2),
        title="[dim green]◈ CH-BLUTTER ◈[/]",
        subtitle=f"[dim cyan]SESSION {datetime.now().strftime('%Y%m%d-%H%M%S')}[/]",
    )
    console.print(panel)

# ──────────────────────────────────────────────────────────────────────────────
#  Analyse ELF — identification des .so
# ──────────────────────────────────────────────────────────────────────────────

def _read_elf_header(data: bytes) -> Optional[dict]:
    """Parse les champs clés de l'en-tête ELF."""
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None
    ei_class = data[4]  # 1=32bit, 2=64bit
    ei_data  = data[5]  # 1=LE, 2=BE
    if ei_data == 1:
        endian = "<"
    elif ei_data == 2:
        endian = ">"
    else:
        return None
    if ei_class == 1:  # ELF32
        e_machine, = struct.unpack_from(endian + "H", data, 18)
        e_shoff,   = struct.unpack_from(endian + "I", data, 32)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", data, 46)
        addr_size = 4
    elif ei_class == 2:  # ELF64
        e_machine, = struct.unpack_from(endian + "H", data, 18)
        e_shoff,   = struct.unpack_from(endian + "Q", data, 40)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", data, 58)
        addr_size = 8
    else:
        return None

    ARCH_MAP = {
        0x28: "arm",    # EM_ARM
        0xB7: "arm64",  # EM_AARCH64
        0x3E: "x64",    # EM_X86_64
        0x03: "x86",    # EM_386
    }
    arch = ARCH_MAP.get(e_machine, f"unknown({e_machine:#x})")
    return {
        "class":     ei_class,
        "endian":    endian,
        "e_machine": e_machine,
        "arch":      arch,
        "e_shoff":   e_shoff,
        "e_shentsize": e_shentsize,
        "e_shnum":   e_shnum,
        "e_shstrndx": e_shstrndx,
        "addr_size": addr_size,
    }


def _get_section_data(raw: bytes, hdr: dict, name: bytes) -> Optional[bytes]:
    """Retourne les données d'une section ELF par nom."""
    shoff     = hdr["e_shoff"]
    shentsize = hdr["e_shentsize"]
    shnum     = hdr["e_shnum"]
    shstrndx  = hdr["e_shstrndx"]
    endian    = hdr["endian"]
    addr_size = hdr["addr_size"]

    if shoff == 0 or shnum == 0:
        return None

    # Lecture de la section shstrtab
    str_off = shoff + shstrndx * shentsize
    if str_off + shentsize > len(raw):
        return None

    if addr_size == 4:
        fmt_off  = endian + "II"  # sh_name, sh_type
        fmt_data = endian + "II"  # sh_offset, sh_size
        hdr_size = 40
    else:
        fmt_off  = endian + "II"
        fmt_data = endian + "QQ"  # sh_offset(8), sh_size(8)
        hdr_size = 64

    # Offset de shstrtab
    sh_name_str, _ = struct.unpack_from(fmt_off, raw, str_off)
    if addr_size == 4:
        sh_str_offset, sh_str_size = struct.unpack_from(fmt_data, raw, str_off + 16)
    else:
        sh_str_offset, sh_str_size = struct.unpack_from(fmt_data, raw, str_off + 24)

    if sh_str_offset + sh_str_size > len(raw):
        return None
    strtab = raw[sh_str_offset: sh_str_offset + sh_str_size]

    # Parcours des sections
    for i in range(shnum):
        off = shoff + i * shentsize
        if off + shentsize > len(raw):
            break
        sh_name_idx, _ = struct.unpack_from(fmt_off, raw, off)
        # Lecture du nom
        null = strtab.find(b"\x00", sh_name_idx)
        if null == -1:
            continue
        sec_name = strtab[sh_name_idx: null]
        if sec_name != name:
            continue
        # Lecture offset+size
        if addr_size == 4:
            sh_offset, sh_size = struct.unpack_from(fmt_data, raw, off + 16)
        else:
            sh_offset, sh_size = struct.unpack_from(fmt_data, raw, off + 24)
        if sh_offset + sh_size > len(raw):
            return None
        return raw[sh_offset: sh_offset + sh_size]

    return None


def _search_dynstr(raw: bytes, hdr: dict, target: bytes) -> bool:
    """Cherche un symbole dans .dynstr ou .strtab."""
    for sec_name in (b".dynstr", b".strtab"):
        data = _get_section_data(raw, hdr, sec_name)
        if data and target in data:
            return True
    return False


def classify_so_file(so_path: str) -> dict:
    """
    Analyse un fichier .so ELF et le classifie :
      - 'dart_app'      : contient les symboles Dart snapshot
      - 'flutter_engine': contient les marqueurs du moteur Flutter
      - 'unknown'       : aucun marqueur reconnu
      - 'invalid'       : pas un ELF valide

    Retourne un dict {type, confidence, arch, size, path, name, matched}.
    """
    result = {
        "type":       "invalid",
        "confidence": 0,
        "arch":       "unknown",
        "size":       0,
        "path":       so_path,
        "name":       os.path.basename(so_path),
        "matched":    [],
    }

    try:
        stat = os.stat(so_path)
        result["size"] = stat.st_size
        if stat.st_size < 512:
            return result

        # Lecture partielle pour l'en-tête (max 16 Mo pour éviter OOM)
        read_size = min(stat.st_size, 16 * 1024 * 1024)
        with open(so_path, "rb") as f:
            raw = f.read(read_size)

        elf_hdr = _read_elf_header(raw)
        if elf_hdr is None:
            return result

        result["arch"] = elf_hdr["arch"]
        result["type"] = "unknown"

        dart_score    = 0
        flutter_score = 0
        matched       = []

        # ── Test 1 : symboles Dart dans .dynstr / .strtab ────────────────
        for sym in DART_APP_SYMBOLS:
            if _search_dynstr(raw, elf_hdr, sym.encode()):
                dart_score += 3
                matched.append(sym)

        # ── Test 2 : marqueurs Flutter dans binaire ──────────────────────
        for marker in FLUTTER_ENGINE_STRINGS:
            if marker in raw:
                flutter_score += 2
                matched.append(marker.decode("ascii", errors="replace"))

        # ── Test 3 : SHA-1 engine IDs dans .rodata ───────────────────────
        rodata = _get_section_data(raw, elf_hdr, b".rodata")
        if rodata:
            sha1_count = len(re.findall(rb"\x00[a-f0-9]{40}\x00", rodata))
            if sha1_count >= 1:
                flutter_score += sha1_count * 2
                matched.append(f"engine_id_sha1×{sha1_count}")

        # ── Test 4 : nom du fichier (heuristique faible) ─────────────────
        fname_low = result["name"].lower()
        if "flutter" in fname_low:
            flutter_score += 1
            matched.append("name:flutter")
        if fname_low in ("libapp.so", "app.so", "libapp"):
            dart_score += 1
            matched.append("name:app")

        result["matched"] = matched

        if dart_score > 0 and dart_score >= flutter_score:
            result["type"]       = "dart_app"
            result["confidence"] = min(dart_score * 10, 100)
        elif flutter_score > 0:
            result["type"]       = "flutter_engine"
            result["confidence"] = min(flutter_score * 10, 100)
        else:
            result["type"]       = "unknown"
            result["confidence"] = 0

    except (OSError, struct.error, Exception) as e:
        dbg(f"classify_so_file({so_path}): {e}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
#  Découverte des .so
# ──────────────────────────────────────────────────────────────────────────────

def find_all_so_files(directory: str) -> list[str]:
    """Retourne tous les fichiers .so trouvés récursivement."""
    found = []
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            if fname.lower().endswith(".so"):
                found.append(os.path.join(root, fname))
    return found


def scan_and_classify(directory: str) -> dict[str, list[dict]]:
    """
    Scanne un dossier, classe chaque .so, retourne :
    { 'dart_app': [...], 'flutter_engine': [...], 'unknown': [...] }
    """
    so_files = find_all_so_files(directory)
    results  = {"dart_app": [], "flutter_engine": [], "unknown": []}

    for so_path in so_files:
        r = classify_so_file(so_path)
        key = r["type"] if r["type"] in results else "unknown"
        results[key].append(r)

    # Tri par confiance décroissante
    for key in results:
        results[key].sort(key=lambda x: x["confidence"], reverse=True)

    return results


# ──────────────────────────────────────────────────────────────────────────────
#  Menu de sélection interactif
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1_024:
        return f"{n / 1_024:.0f} KB"
    return f"{n} B"


def select_so_menu(candidates: list[dict], label: str = "fichier .so") -> Optional[dict]:
    """
    Affiche un menu pour choisir parmi plusieurs .so.
    Retourne le dict sélectionné ou None.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    if HAS_RICH:
        t = Table(box=box.SIMPLE_HEAVY, border_style="cyan",
                  show_header=True, header_style="bold cyan",
                  title=f"[bold yellow]Sélection : {label}[/]",
                  title_style="bold yellow")
        t.add_column("#",          style="dim cyan",   width=4,  justify="right")
        t.add_column("Fichier",    style="bright_white", min_width=24)
        t.add_column("Arch",       style="green",       width=8)
        t.add_column("Taille",     style="dim",         width=10, justify="right")
        t.add_column("Confiance",  style="cyan",        width=10, justify="right")
        t.add_column("Marqueurs",  style="dim green",   min_width=20)

        for idx, c in enumerate(candidates):
            conf_str  = f"{c['confidence']}%"
            mark_str  = ", ".join(c["matched"][:3])
            if len(c["matched"]) > 3:
                mark_str += f" +{len(c['matched'])-3}"
            rel_path = os.path.relpath(c["path"])
            t.add_row(
                str(idx),
                escape(rel_path),
                c["arch"],
                _fmt_size(c["size"]),
                conf_str,
                escape(mark_str),
            )
        console.print(t)
        raw = Prompt.ask(
            f"[bright_green]Numéro (0–{len(candidates)-1})[/]",
            default="0",
        ).strip()
    else:
        print(f"\nSélection : {label}")
        for idx, c in enumerate(candidates):
            rel = os.path.relpath(c["path"])
            print(f"  [{idx}] {rel}  arch={c['arch']}  conf={c['confidence']}%")
        raw = input(f"Numéro (0–{len(candidates)-1}) [0]: ").strip() or "0"

    if raw.isdigit():
        n = int(raw)
        if 0 <= n < len(candidates):
            return candidates[n]
    warn("Sélection invalide, utilisation du premier candidat.")
    return candidates[0]


def locate_libs(
    indir: str,
    auto: bool = False,
    prefer_arch: str = "arm64",
) -> tuple[str, str]:
    """
    Localise (dart_app_so, flutter_so) dans un répertoire.

    Stratégie :
      1. Sous-dossiers ARM standard (arm64-v8a, armeabi-v7a, …)
      2. Scan récursif + classification ELF de tous les .so
      3. Menu interactif si plusieurs candidats (sauf auto=True)

    Lève SystemExit si introuvable.
    """
    # ── Étape 1 : sous-dossiers ARM standard ─────────────────────────────
    for arch_dir in ARM_ARCH_DIRS:
        for sub in (arch_dir, os.path.join("lib", arch_dir)):
            d = os.path.join(indir, sub)
            if not os.path.isdir(d):
                continue
            candidates = scan_and_classify(d)
            dart_list    = candidates["dart_app"]
            flutter_list = candidates["flutter_engine"]
            if dart_list and flutter_list:
                ok(f"Bibliothèques trouvées dans [cyan]{sub}[/]")
                return dart_list[0]["path"], flutter_list[0]["path"]

    # ── Étape 2 : scan global ─────────────────────────────────────────────
    info("Scan complet des fichiers .so…")
    all_classified = scan_and_classify(indir)
    dart_list    = all_classified["dart_app"]
    flutter_list = all_classified["flutter_engine"]
    unknown_list = all_classified["unknown"]

    dbg(f"dart_app={len(dart_list)}  flutter={len(flutter_list)}  unknown={len(unknown_list)}")

    # ── Flutter engine ─────────────────────────────────────────────────────
    if not flutter_list:
        err("Aucune bibliothèque Flutter engine détectée.")
        err("Vérifiez que le dossier contient un libflutter.so valide.")
        sys.exit(1)
    flutter_so = flutter_list[0]["path"]
    if len(flutter_list) > 1 and not auto:
        info(f"{len(flutter_list)} bibliothèques Flutter détectées.")
        sel = select_so_menu(flutter_list, "Flutter engine (.so)")
        if sel:
            flutter_so = sel["path"]
    ok(f"Flutter engine → [cyan]{os.path.basename(flutter_so)}[/]")

    # ── Dart app ─────────────────────────────────────────────────────────
    if not dart_list:
        # Fallback : on propose tous les .so non-Flutter
        fallback = [c for c in unknown_list
                    if c["path"] != flutter_so]
        if not fallback:
            err("Aucun .so Dart app détecté.")
            err("Aucun candidat disponible pour libapp.so.")
            sys.exit(1)
        warn(f"Aucun .so Dart détecté avec certitude — {len(fallback)} candidat(s) inconnu(s).")
        dart_list = fallback

    if len(dart_list) == 1 or auto:
        dart_so = dart_list[0]["path"]
    else:
        info(f"{len(dart_list)} candidats pour le .so Dart app.")
        sel = select_so_menu(dart_list, "Dart app snapshot (.so)")
        dart_so = sel["path"] if sel else dart_list[0]["path"]

    ok(f"Dart app     → [cyan]{os.path.basename(dart_so)}[/]")
    return dart_so, flutter_so


# ──────────────────────────────────────────────────────────────────────────────
#  Extraction APK
# ──────────────────────────────────────────────────────────────────────────────

def extract_apk(apk_path: str, out_dir: str) -> str:
    """
    Extrait les bibliothèques .so d'un APK dans out_dir/libs.
    Retourne le chemin du dossier libs extrait.
    """
    libs_dir = os.path.join(out_dir, "libs")
    os.makedirs(libs_dir, exist_ok=True)

    info(f"Extraction APK : [cyan]{os.path.basename(apk_path)}[/]")
    extracted = 0

    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            entries = z.namelist()
            so_entries = [e for e in entries if e.lower().endswith(".so")]

            if not so_entries:
                err("Aucun fichier .so dans l'APK.")
                sys.exit(1)

            if HAS_RICH:
                with Progress(
                    SpinnerColumn(style="green"),
                    TextColumn("[cyan]{task.description}[/]"),
                    BarColumn(bar_width=30, style="green"),
                    TextColumn("[dim]{task.completed}/{task.total}[/]"),
                    console=console,
                    transient=True,
                ) as prog:
                    task = prog.add_task("Extraction…", total=len(so_entries))
                    for entry in so_entries:
                        dest = os.path.join(libs_dir, entry.replace("/", os.sep))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with z.open(entry) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        extracted += 1
                        prog.advance(task)
            else:
                for entry in so_entries:
                    dest = os.path.join(libs_dir, entry.replace("/", os.sep))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(entry) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted += 1

    except zipfile.BadZipFile:
        err(f"Le fichier n'est pas un APK/ZIP valide : {apk_path}")
        sys.exit(1)

    ok(f"{extracted} bibliothèque(s) extraite(s) dans [cyan]{libs_dir}[/]")
    return libs_dir


# ──────────────────────────────────────────────────────────────────────────────
#  Vérification des dépendances
# ──────────────────────────────────────────────────────────────────────────────

def _check_cmd(cmd: str) -> tuple[bool, str]:
    path = shutil.which(cmd)
    if path is None:
        return False, ""
    try:
        r = subprocess.run(
            [cmd, "--version"], capture_output=True, timeout=5
        )
        ver_line = (r.stdout or r.stderr).decode("utf-8", errors="replace").splitlines()
        ver = ver_line[0].strip() if ver_line else "?"
    except Exception:
        ver = "?"
    return True, ver


def _check_python_pkg(pkg: str) -> bool:
    try:
        __import__(pkg)
        return True
    except ImportError:
        return False


def check_deps(verbose: bool = False) -> bool:
    """Vérifie toutes les dépendances. Retourne True si tout est OK."""
    section("Vérification des dépendances")
    all_ok = True

    # Python
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok  = sys.version_info >= (3, 9)
    _row("Python ≥ 3.9", py_ok, py_ver)
    if not py_ok:
        all_ok = False

    # Outils système
    tools = {
        "git":        ("git", True),
        CMAKE_CMD:    ("cmake", True),
        NINJA_CMD:    ("ninja / ninja-build", True),
        "clang":      ("clang (ou gcc)", False),
        "pkg-config": ("pkg-config", False),
    }
    # ninja-build alias
    has_ninja, _ = _check_cmd(NINJA_CMD)
    if not has_ninja:
        has_ninja, _ = _check_cmd("ninja-build")
        if has_ninja:
            tools[NINJA_CMD] = ("ninja-build ✓", True)

    for cmd, (label, required) in tools.items():
        found, ver = _check_cmd(cmd)
        if not found and cmd == "clang":
            found, ver = _check_cmd("gcc")
            if found:
                label = "gcc (clang non trouvé)"
        _row(label, found, ver, required=required)
        if required and not found:
            all_ok = False

    # Bibliothèques système (via pkg-config)
    sys_libs = {
        "icu-uc":   ("libicu",     True),
        "capstone": ("capstone",   True),
        "fmt":      ("libfmt",     True),
    }
    for pkg, (label, required) in sys_libs.items():
        found = (shutil.which("pkg-config") and
                 subprocess.run(["pkg-config", "--exists", pkg],
                                capture_output=True).returncode == 0)
        _row(label, found, "pkg-config ✓" if found else "", required=required)
        if required and not found:
            all_ok = False

    # Modules Python
    py_mods = {
        "pyelftools": ("pyelftools", True),
        "requests":   ("requests",   True),
        "rich":       ("rich (TUI)", False),
        "capstone":   ("capstone-py", False),
    }
    for mod, (label, required) in py_mods.items():
        found = _check_python_pkg(mod)
        _row(f"[py] {label}", found, "pip ✓" if found else "pip install " + mod,
             required=required)
        if required and not found:
            all_ok = False

    print()
    if all_ok:
        ok("Toutes les dépendances obligatoires sont présentes.")
    else:
        warn("Certaines dépendances manquantes — voir ci-dessus.")
        warn("Sur Termux : ./setup_termux.sh")
        warn("Sur Linux  : sudo apt install cmake ninja-build clang pkg-config libicu-dev libcapstone-dev libfmt-dev")
    return all_ok


def _row(label: str, ok_: bool, detail: str = "", required: bool = True):
    if HAS_RICH:
        status = "[bright_green]✔[/]" if ok_ else ("[bright_red]✘[/]" if required else "[yellow]—[/]")
        req_tag = ""  if required else "[dim](optionnel)[/]"
        det_str = f"[dim]{escape(detail)}[/]" if detail else ""
        rprint(f"  {status}  [white]{label:<28}[/] {det_str} {req_tag}")
    else:
        status = "✔" if ok_ else ("✘" if required else "—")
        print(f"  {status}  {label:<28} {detail}")


# ──────────────────────────────────────────────────────────────────────────────
#  Build de la lib Dart VM
# ──────────────────────────────────────────────────────────────────────────────

def build_dart_vm_lib(
    dart_version: str,
    os_name: str,
    arch: str,
    snapshot_hash: Optional[str] = None,
    no_compressed_ptrs: bool = False,
) -> bool:
    """
    Compile la lib statique Dart VM via dartvm_fetch_build.
    Retourne True si succès.
    """
    if not HAS_DARTVM:
        err("Module dartvm_fetch_build introuvable.")
        return False

    section(f"Build Dart VM  {dart_version} / {os_name} / {arch}")

    try:
        info_obj = DartLibInfo(
            version=dart_version,
            os_name=os_name,
            arch=arch,
            has_compressed_ptrs=(not no_compressed_ptrs),
            snapshot_hash=snapshot_hash,
        )
        info(f"Lib cible : [cyan]{info_obj.lib_name}[/]")

        # Vérifier si déjà compilé
        lib_name = info_obj.lib_name
        install_hint = os.path.join(PKG_LIB_DIR, f"lib{lib_name}.a")
        if os.path.isfile(install_hint):
            ok(f"Lib déjà compilée : [cyan]{lib_name}[/]")
            return True

        fetch_and_build(info_obj)
        ok(f"Lib Dart VM compilée : [cyan]{lib_name}[/]")
        return True

    except BlutterBuildError as e:
        err(f"Erreur de build Dart VM :\n  {e}")
        return False
    except Exception as e:
        err(f"Erreur inattendue lors du build Dart VM : {e}")
        if DEBUG_MODE:
            raise
        return False


# ──────────────────────────────────────────────────────────────────────────────
#  Build de l'exécutable blutter
# ──────────────────────────────────────────────────────────────────────────────

def _blutter_exe_name(dart_version: str, os_name: str, arch: str,
                      no_analysis: bool = False, ida_fcn: bool = False,
                      no_compressed_ptrs: bool = False) -> str:
    name = f"blutter_dartvm{dart_version}_{os_name}_{arch}"
    if no_compressed_ptrs:
        name += "_no-compressed-ptrs"
    if no_analysis:
        name += "_no-analysis"
    if ida_fcn:
        name += "_ida-fcn"
    return name


def blutter_exe_exists(
    dart_version: str, os_name: str, arch: str,
    no_analysis: bool = False, ida_fcn: bool = False,
    no_compressed_ptrs: bool = False,
) -> Optional[str]:
    """Retourne le chemin de l'exécutable blutter s'il existe, sinon None."""
    name = _blutter_exe_name(dart_version, os_name, arch,
                              no_analysis, ida_fcn, no_compressed_ptrs)
    for exe in (name, name + ".exe"):
        path = os.path.join(BIN_DIR, exe)
        if os.path.isfile(path):
            return path
    return None


def build_blutter_exe(
    dart_version: str,
    os_name: str,
    arch: str,
    no_analysis: bool = False,
    ida_fcn: bool = False,
    no_compressed_ptrs: bool = False,
) -> Optional[str]:
    """
    Configure et compile l'exécutable blutter via CMake/Ninja.
    Retourne le chemin de l'exécutable ou None.
    """
    section("Build exécutable blutter")

    blutter_src = os.path.join(SCRIPT_DIR, "blutter")
    cmake_lists = os.path.join(blutter_src, "CMakeLists.txt")
    if not os.path.isfile(cmake_lists):
        err(f"Sources C++ blutter introuvables : {blutter_src}/CMakeLists.txt")
        err("Clonez le sous-dépôt : git clone https://github.com/worawit/blutter.git blutter")
        return None

    exe_name = _blutter_exe_name(dart_version, os_name, arch,
                                  no_analysis, ida_fcn, no_compressed_ptrs)
    build_subdir = os.path.join(BUILD_DIR, exe_name)
    exe_path     = os.path.join(BIN_DIR, exe_name)
    os.makedirs(build_subdir, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)

    # Options CMake
    cmake_defs = [
        f"-DDARTVM_VERSION={dart_version}",
        f"-DDARTVM_OS={os_name}",
        f"-DDARTVM_ARCH={arch}",
        f"-DCOMPRESSED_PTRS={'OFF' if no_compressed_ptrs else 'ON'}",
        f"-DNO_ANALYSIS={'ON' if no_analysis else 'OFF'}",
        f"-DIDA_FCN={'ON' if ida_fcn else 'OFF'}",
        f"-DCMAKE_INSTALL_PREFIX={SCRIPT_DIR}",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DPKG_INCLUDE_DIR={PKG_INC_DIR}",
        f"-DPKG_LIB_DIR={PKG_LIB_DIR}",
    ]

    try:
        info("Configuration CMake…")
        _run_cmd(
            [CMAKE_CMD, "-GNinja", blutter_src, "-B", build_subdir] + cmake_defs,
            cwd=SCRIPT_DIR,
        )
        info("Compilation…")
        _run_cmd([NINJA_CMD, "-C", build_subdir])
        info("Installation…")
        _run_cmd([CMAKE_CMD, "--install", build_subdir])

    except subprocess.CalledProcessError as e:
        err(f"Erreur CMake/Ninja (code {e.returncode})")
        return None
    except FileNotFoundError as e:
        err(f"Outil manquant : {e}")
        return None

    if os.path.isfile(exe_path):
        ok(f"Exécutable compilé : [cyan]{exe_name}[/]")
        return exe_path

    # Cherche aussi dans build dir
    for fname in os.listdir(build_subdir):
        if fname.startswith("blutter"):
            src = os.path.join(build_subdir, fname)
            shutil.copy2(src, exe_path)
            ok(f"Exécutable copié : [cyan]{exe_name}[/]")
            return exe_path

    err(f"Exécutable introuvable après build dans {build_subdir}")
    return None


def _run_cmd(args: list, cwd: str = None, timeout: int = 600):
    """Lance une commande en affichant la sortie en temps réel."""
    dbg(f"Cmd: {' '.join(str(a) for a in args)}")
    proc = subprocess.run(
        args,
        cwd=cwd,
        timeout=timeout,
        check=True,
        stdout=None if DEBUG_MODE else subprocess.PIPE,
        stderr=None if DEBUG_MODE else subprocess.STDOUT,
    )
    if not DEBUG_MODE and proc.stdout:
        dbg(proc.stdout.decode("utf-8", errors="replace"))


# ──────────────────────────────────────────────────────────────────────────────
#  Lancement de l'analyse
# ──────────────────────────────────────────────────────────────────────────────

def run_analysis(
    dart_app_so: str,
    flutter_so: str,
    out_dir: str,
    blutter_exe: str,
    dart_version: str,
    os_name: str,
    arch: str,
    no_analysis: bool = False,
    ida_fcn: bool = False,
) -> bool:
    """Lance l'exécutable blutter sur les .so cibles."""
    section("Analyse Dart")
    os.makedirs(out_dir, exist_ok=True)

    cmd = [blutter_exe, dart_app_so, flutter_so, out_dir]
    if no_analysis:
        cmd.append("--no-analysis")
    if ida_fcn:
        cmd.append("--ida-fcn")

    info(f"Exécutable : [cyan]{os.path.basename(blutter_exe)}[/]")
    info(f"App        : [cyan]{os.path.basename(dart_app_so)}[/]")
    info(f"Flutter    : [cyan]{os.path.basename(flutter_so)}[/]")
    info(f"Sortie     : [cyan]{out_dir}[/]")

    start = time.time()
    try:
        if HAS_RICH:
            with Progress(
                SpinnerColumn(style="bright_green"),
                TextColumn("[cyan]Analyse en cours…[/]"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as prog:
                prog.add_task("analyse")
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        else:
            print("  Analyse en cours…")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        elapsed = time.time() - start

        if proc.returncode != 0:
            err(f"Blutter a retourné le code {proc.returncode}")
            if proc.stderr:
                rprint(f"[dim red]{escape(proc.stderr[:2000])}[/]")
            return False

        if proc.stdout:
            rprint(f"[dim green]{escape(proc.stdout[:3000])}[/]")

        ok(f"Analyse terminée en [cyan]{elapsed:.1f}s[/]")
        _show_results(out_dir)
        return True

    except subprocess.TimeoutExpired:
        err("Timeout : l'analyse a dépassé 10 minutes.")
        return False
    except FileNotFoundError:
        err(f"Exécutable introuvable : {blutter_exe}")
        return False
    except Exception as e:
        err(f"Erreur inattendue lors de l'analyse : {e}")
        if DEBUG_MODE:
            raise
        return False


def _show_results(out_dir: str):
    """Affiche un résumé des fichiers produits."""
    items = []
    for fname in sorted(os.listdir(out_dir)):
        fpath = os.path.join(out_dir, fname)
        if os.path.isfile(fpath):
            items.append((fname, _fmt_size(os.path.getsize(fpath))))
        elif os.path.isdir(fpath):
            count = len(list(Path(fpath).rglob("*")))
            items.append((fname + "/", f"{count} fichiers"))

    if not items:
        return

    if HAS_RICH:
        t = Table(box=box.SIMPLE, border_style="dim green",
                  title="[bold green]Résultats produits[/]",
                  title_style="bold green",
                  show_header=False)
        t.add_column("Nom",    style="cyan",    min_width=28)
        t.add_column("Info",   style="dim",     width=12, justify="right")
        for name, size in items:
            t.add_row(name, size)
        console.print(t)
    else:
        print("\n  Résultats :")
        for name, size in items:
            print(f"    {name:<30} {size}")


# ──────────────────────────────────────────────────────────────────────────────
#  Historique
# ──────────────────────────────────────────────────────────────────────────────

def _save_history(entry: dict):
    history = _load_history()
    history.insert(0, entry)
    history = history[:50]  # garder les 50 dernières
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _load_history() -> list:
    if not os.path.isfile(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def show_history():
    section("Historique des analyses")
    history = _load_history()
    if not history:
        info("Aucun historique trouvé.")
        return

    if HAS_RICH:
        t = Table(box=box.SIMPLE_HEAVY, border_style="cyan",
                  show_header=True, header_style="bold cyan")
        t.add_column("Date",        style="dim",         width=20)
        t.add_column("App .so",     style="bright_white", min_width=20)
        t.add_column("Version",     style="green",        width=12)
        t.add_column("OS/Arch",     style="cyan",         width=16)
        t.add_column("Durée",       style="dim",          width=8)
        t.add_column("Statut",      style="",             width=8)
        for h in history:
            status = "[bright_green]✔[/]" if h.get("success") else "[bright_red]✘[/]"
            t.add_row(
                h.get("date", ""),
                escape(os.path.basename(h.get("app_so", ""))),
                h.get("dart_version", ""),
                f"{h.get('os_name','')} / {h.get('arch','')}",
                h.get("duration", ""),
                status,
            )
        console.print(t)
    else:
        for h in history:
            print(f"  {h.get('date','')}  {h.get('dart_version','')}  {h.get('app_so','')}")


# ──────────────────────────────────────────────────────────────────────────────
#  Flux principal d'analyse
# ──────────────────────────────────────────────────────────────────────────────

def run_full_pipeline(
    input_path: str,
    out_dir: str,
    dart_version_override: Optional[str] = None,
    rebuild: bool = False,
    no_analysis: bool = False,
    ida_fcn: bool = False,
    auto_select: bool = False,
    no_compressed_ptrs: bool = False,
) -> bool:
    """
    Pipeline complet :
      1. Préparer le dossier d'entrée (APK → extract, dossier direct)
      2. Localiser dart_app.so + flutter.so
      3. Extraire les infos Dart (version, arch, hash…)
      4. Builder la lib Dart VM si nécessaire
      5. Builder l'exécutable blutter si nécessaire
      6. Lancer l'analyse
    """
    os.makedirs(out_dir, exist_ok=True)
    t_start = time.time()

    # ── Étape 1 : préparation ───────────────────────────────────────────────
    section("Préparation")
    if os.path.isfile(input_path) and input_path.lower().endswith(".apk"):
        work_dir = extract_apk(input_path, out_dir)
    elif os.path.isdir(input_path):
        work_dir = input_path
    else:
        err(f"Chemin introuvable ou type non reconnu : {input_path}")
        return False

    # ── Étape 2 : localisation des .so ─────────────────────────────────────
    section("Localisation des bibliothèques")
    dart_app_so, flutter_so = locate_libs(work_dir, auto=auto_select)

    # ── Étape 3 : extraction des infos Dart ────────────────────────────────
    section("Extraction des métadonnées Dart")

    dart_version  = None
    snapshot_hash = None
    flags         = []
    arch          = "arm64"
    os_name       = "android"

    if dart_version_override:
        # Format "3.4.2_android_arm64" ou "3.4.2"
        parts = dart_version_override.split("_")
        if len(parts) == 3:
            dart_version, os_name, arch = parts
        else:
            dart_version = parts[0]
        info(f"Version Dart forcée : [cyan]{dart_version}[/]")
    elif HAS_EXTRACT:
        try:
            dart_version, snapshot_hash, flags, arch, os_name = \
                extract_dart_info(dart_app_so, flutter_so)
            ok(f"Version Dart  : [cyan]{dart_version}[/]")
            ok(f"Architecture  : [cyan]{os_name}/{arch}[/]")
            if snapshot_hash:
                ok(f"Snapshot hash : [dim cyan]{snapshot_hash}[/]")
            if flags:
                info(f"Flags         : [dim]{' '.join(flags)}[/]")
        except (BlutterExtractError, FileNotFoundError) as e:
            err(f"Extraction Dart échouée : {e}")
            err("Utilisez --dart-version VERSION_OS_ARCH pour spécifier manuellement.")
            return False
    else:
        err("Module extract_dart_info introuvable.")
        err("Utilisez --dart-version VERSION_OS_ARCH.")
        return False

    if not dart_version:
        err("Version Dart introuvable.")
        return False

    # Normalisation arch depuis l'analyse .so si ELF disponible
    dart_elf = classify_so_file(dart_app_so)
    if dart_elf["arch"] != "unknown":
        arch = dart_elf["arch"]
        dbg(f"Arch depuis ELF : {arch}")

    # ── Étape 4 : build lib Dart VM ─────────────────────────────────────────
    if HAS_DARTVM:
        lib_built = build_dart_vm_lib(
            dart_version, os_name, arch,
            snapshot_hash=snapshot_hash,
            no_compressed_ptrs=no_compressed_ptrs,
        )
        if not lib_built:
            return False
    else:
        warn("Module dartvm_fetch_build absent — on suppose la lib déjà compilée.")

    # ── Étape 5 : build exécutable blutter ──────────────────────────────────
    exe_path = None
    if not rebuild:
        exe_path = blutter_exe_exists(
            dart_version, os_name, arch,
            no_analysis, ida_fcn, no_compressed_ptrs,
        )
    if exe_path:
        ok(f"Exécutable existant : [cyan]{os.path.basename(exe_path)}[/]")
    else:
        exe_path = build_blutter_exe(
            dart_version, os_name, arch,
            no_analysis, ida_fcn, no_compressed_ptrs,
        )
        if exe_path is None:
            return False

    # ── Étape 6 : analyse ────────────────────────────────────────────────────
    success = run_analysis(
        dart_app_so, flutter_so, out_dir, exe_path,
        dart_version, os_name, arch,
        no_analysis=no_analysis, ida_fcn=ida_fcn,
    )

    elapsed = time.time() - t_start
    _save_history({
        "date":         datetime.now().strftime("%Y-%m-%d %H:%M"),
        "app_so":       dart_app_so,
        "flutter_so":   flutter_so,
        "dart_version": dart_version,
        "os_name":      os_name,
        "arch":         arch,
        "out_dir":      out_dir,
        "duration":     f"{elapsed:.0f}s",
        "success":      success,
    })

    if success:
        print()
        if HAS_RICH:
            console.print(Panel(
                f"[bright_green]  ✔  Analyse réussie en {elapsed:.1f}s[/]\n"
                f"[cyan]  Résultats dans : {escape(out_dir)}[/]",
                border_style="bright_green",
                box=box.ROUNDED,
            ))
        else:
            print(f"\n  ✔  Analyse réussie en {elapsed:.1f}s")
            print(f"  Résultats dans : {out_dir}")
    return success


# ──────────────────────────────────────────────────────────────────────────────
#  Mode TUI interactif
# ──────────────────────────────────────────────────────────────────────────────

def interactive_mode():
    """Lance le mode TUI interactif avec menu."""
    print_banner()
    section("Mode interactif")

    # ── Choix du fichier d'entrée ────────────────────────────────────────────
    if HAS_RICH:
        rprint("\n[bold cyan]  Entrez le chemin vers :[/]")
        rprint("  [dim]• un fichier APK  (app.apk)[/]")
        rprint("  [dim]• un dossier de libs  (./libs/arm64-v8a)[/]\n")
        input_path = Prompt.ask("[bright_green]  Chemin[/]").strip().strip('"\'')
    else:
        print("\n  Entrez le chemin vers un APK ou dossier de libs :")
        input_path = input("  Chemin : ").strip().strip('"\'')

    if not input_path:
        err("Chemin vide.")
        sys.exit(1)
    input_path = os.path.expanduser(input_path)
    if not os.path.exists(input_path):
        err(f"Chemin introuvable : {input_path}")
        sys.exit(1)

    # ── Dossier de sortie ────────────────────────────────────────────────────
    default_out = os.path.join(
        os.path.dirname(os.path.abspath(input_path)),
        "blutter_out",
    )
    if HAS_RICH:
        out_dir = Prompt.ask(
            f"[bright_green]  Dossier de sortie[/]",
            default=default_out,
        ).strip().strip('"\'')
    else:
        out_dir = input(f"  Dossier de sortie [{default_out}] : ").strip() or default_out

    out_dir = os.path.expanduser(out_dir)

    # ── Options ───────────────────────────────────────────────────────────────
    if HAS_RICH:
        no_analysis = Confirm.ask("  [cyan]Désactiver l'analyse Dart ?[/]", default=False)
        ida_fcn     = Confirm.ask("  [cyan]Générer les noms IDA Pro ?[/]",   default=False)
        rebuild     = Confirm.ask("  [cyan]Forcer la recompilation ?[/]",     default=False)
        dart_ver    = Prompt.ask(
            "  [cyan]Version Dart manuelle (vide = auto)[/]",
            default="",
        ).strip() or None
    else:
        no_analysis = input("  Désactiver l'analyse Dart ? [o/N] : ").lower() in ("o","y","oui","yes")
        ida_fcn     = input("  Générer les noms IDA Pro ?   [o/N] : ").lower() in ("o","y","oui","yes")
        rebuild     = input("  Forcer la recompilation ?     [o/N] : ").lower() in ("o","y","oui","yes")
        dart_ver    = input("  Version Dart (vide = auto) : ").strip() or None

    # ── Résumé avant lancement ───────────────────────────────────────────────
    section("Résumé")
    info(f"Entrée      : [cyan]{input_path}[/]")
    info(f"Sortie      : [cyan]{out_dir}[/]")
    info(f"Version     : [cyan]{dart_ver or 'auto'}[/]")
    info(f"No analysis : [cyan]{no_analysis}[/]")
    info(f"IDA FCN     : [cyan]{ida_fcn}[/]")
    info(f"Rebuild     : [cyan]{rebuild}[/]")
    print()

    if HAS_RICH:
        go = Confirm.ask("[bright_green]  Lancer l'analyse ?[/]", default=True)
    else:
        go = input("  Lancer l'analyse ? [O/n] : ").lower() not in ("n","non","no")
    if not go:
        info("Annulé.")
        return

    success = run_full_pipeline(
        input_path=input_path,
        out_dir=out_dir,
        dart_version_override=dart_ver,
        rebuild=rebuild,
        no_analysis=no_analysis,
        ida_fcn=ida_fcn,
    )
    sys.exit(0 if success else 1)


# ──────────────────────────────────────────────────────────────────────────────
#  Gestion du lock et des signaux
# ──────────────────────────────────────────────────────────────────────────────

_LOCK_FILE = os.path.join(tempfile.gettempdir(), "chblutter.lock")

def _cleanup():
    try:
        if os.path.isfile(_LOCK_FILE):
            os.remove(_LOCK_FILE)
    except OSError:
        pass

def _signal_handler(sig, frame):
    print("\n  Interruption reçue — arrêt.")
    _cleanup()
    sys.exit(130)


# ──────────────────────────────────────────────────────────────────────────────
#  Point d'entrée CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blutter",
        description="Ch-blutter — Flutter Reverse Engineering Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python blutter.py                          # TUI interactif
  python blutter.py app.apk ./out            # CLI direct (APK)
  python blutter.py ./libs/arm64-v8a ./out   # CLI direct (dossier)
  python blutter.py app.apk ./out --rebuild
  python blutter.py app.apk ./out --dart-version 3.4.2_android_arm64
  python blutter.py --check-deps
  python blutter.py --history
""",
    )
    p.add_argument("indir",   nargs="?", default=None,
                   help="APK ou dossier contenant les .so")
    p.add_argument("outdir",  nargs="?", default=None,
                   help="Dossier de sortie")
    p.add_argument("--dart-version", metavar="VER",
                   help="Version Dart manuelle : VERSION_OS_ARCH  ex: 3.4.2_android_arm64")
    p.add_argument("--rebuild",       action="store_true",
                   help="Force la recompilation de l'exécutable blutter")
    p.add_argument("--no-analysis",   action="store_true",
                   help="Désactive l'analyse Dart (plus rapide)")
    p.add_argument("--ida-fcn",       action="store_true",
                   help="Génère les noms de fonctions pour IDA Pro")
    p.add_argument("--no-update",     action="store_true",
                   help="Ne pas vérifier les mises à jour git")
    p.add_argument("--no-compressed-ptrs", action="store_true",
                   help="Désactive la compression des pointeurs Dart")
    p.add_argument("--auto",          action="store_true",
                   help="Sélection automatique des .so (pas de menu)")
    p.add_argument("--check-deps",    action="store_true",
                   help="Vérifie les dépendances et quitte")
    p.add_argument("--history",       action="store_true",
                   help="Affiche l'historique des analyses")
    p.add_argument("--debug",         action="store_true",
                   help="Affiche les messages de débogage et tracebacks complets")
    return p


def main():
    global DEBUG_MODE

    # Signaux
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    import atexit
    atexit.register(_cleanup)

    parser = build_parser()
    args   = parser.parse_args()

    if args.debug:
        DEBUG_MODE = True

    # ── Commandes autonomes ──────────────────────────────────────────────────
    if args.check_deps:
        print_banner()
        ok_ = check_deps()
        sys.exit(0 if ok_ else 1)

    if args.history:
        print_banner()
        show_history()
        sys.exit(0)

    # ── Mode interactif si pas d'arguments positionnels ──────────────────────
    if args.indir is None:
        interactive_mode()
        return  # interactive_mode appelle sys.exit()

    # ── Mode CLI ──────────────────────────────────────────────────────────────
    if args.outdir is None:
        parser.error("outdir est requis en mode CLI.")

    print_banner()

    input_path = os.path.expanduser(args.indir)
    out_dir    = os.path.expanduser(args.outdir)

    if not os.path.exists(input_path):
        err(f"Chemin d'entrée introuvable : {input_path}")
        sys.exit(1)

    success = run_full_pipeline(
        input_path=input_path,
        out_dir=out_dir,
        dart_version_override=args.dart_version,
        rebuild=args.rebuild,
        no_analysis=args.no_analysis,
        ida_fcn=args.ida_fcn,
        auto_select=args.auto,
        no_compressed_ptrs=args.no_compressed_ptrs,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
