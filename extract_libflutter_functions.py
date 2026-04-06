#!/usr/bin/env python3
"""
scripts/extract_libflutter_functions.py
────────────────────────────────────────
Extrait les adresses des fonctions Dart C API internes de libflutter.so
par disassemblage ARM64 ciblé.

Améliorations vs l'original :
  - Toutes les assert → exceptions avec contexte (adresse, instruction)
  - Détection propre si capstone est absent
  - Vérification préalable que .rodata, .rela.dyn, .text existent
  - Support du mode verbose
  - get_dart_version() extractible indépendamment
  - CLI amélioré avec argparse
"""

from __future__ import annotations

import os
import sys
from struct import pack, unpack
from typing import Optional

VERBOSE = os.environ.get("BLUTTER_VERBOSE", "0") == "1"


def _dbg(msg: str):
    if VERBOSE:
        print(f"  [DBG] {msg}", file=sys.stderr)


# ── dépendances ──────────────────────────────────────────────────────────

try:
    from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
    HAS_CAPSTONE = True
except ImportError:
    HAS_CAPSTONE = False

try:
    from elftools.elf.elffile import ELFFile
    HAS_ELFTOOLS = True
except ImportError:
    HAS_ELFTOOLS = False


class ExtractionError(RuntimeError):
    """Erreur d'extraction avec contexte explicite."""
    pass


def _check_deps():
    missing = []
    if not HAS_CAPSTONE:
        missing.append("capstone  → pip install capstone")
    if not HAS_ELFTOOLS:
        missing.append("pyelftools → pip install pyelftools")
    if missing:
        raise ImportError(
            "Dépendances manquantes :\n" +
            "\n".join(f"  • {m}" for m in missing)
        )


# ── helpers ELF ──────────────────────────────────────────────────────────

def _require_section(elf: "ELFFile", name: str):
    sec = elf.get_section_by_name(name)
    if sec is None:
        raise ExtractionError(
            f"Section ELF '{name}' introuvable dans libflutter.so.\n"
            "  → Le fichier est peut-être strippé ou corrompu."
        )
    return sec


def _read_string(data: bytes, base_addr: int, addr: int) -> str:
    """Lit une chaîne ASCII depuis `data` à l'adresse virtuelle `addr`."""
    if addr < base_addr:
        raise ExtractionError(
            f"Adresse {addr:#x} inférieure à la base de .rodata {base_addr:#x}"
        )
    offset = addr - base_addr
    if offset >= len(data):
        raise ExtractionError(
            f"Adresse {addr:#x} hors limites de .rodata (taille={len(data):#x})"
        )
    end = data.find(b"\x00", offset)
    if end == -1:
        raise ExtractionError(
            f"Chaîne non terminée à l'offset {offset:#x} dans .rodata"
        )
    return data[offset:end].decode("ascii", errors="replace")


# ── extraction principale ─────────────────────────────────────────────────

def extract_libflutter_functions(
    libflutter_file: str,
) -> tuple[str, dict[str, int]]:
    """
    Extrait la version Dart et les adresses des fonctions Dart C API.

    Retourne (dart_version, {nom_fonction: adresse}).
    Lève ExtractionError avec un message précis en cas d'échec.
    """
    _check_deps()

    if not os.path.isfile(libflutter_file):
        raise FileNotFoundError(f"libflutter.so introuvable : {libflutter_file}")

    with open(libflutter_file, "rb") as f:
        elf = ELFFile(f)

        rodata_sec = _require_section(elf, ".rodata")
        rela_sec   = _require_section(elf, ".rela.dyn")
        text_sec   = _require_section(elf, ".text")

        rodata     = rodata_sec.data()
        rela_data  = rela_sec.data()
        rohdr      = rodata_sec.header
        text_hdr   = text_sec.header

        rodata_base = rohdr.sh_addr
        rela_base   = rela_sec.header.sh_addr
        RELA_ENTRY  = 24  # taille d'une entrée .rela.dyn

        # ── Cherche l'offset de "Platform_GetVersion" dans .rodata ────
        marker = b"\x00Platform_GetVersion\x00"
        marker_pos = rodata.find(marker)
        if marker_pos == -1:
            raise ExtractionError(
                "Marqueur 'Platform_GetVersion' introuvable dans .rodata.\n"
                "  → Ce libflutter.so n'est pas un build standard Flutter."
            )
        getver_text_addr = rodata_base + marker_pos + 1  # +1 pour sauter le \x00
        _dbg(f"Platform_GetVersion text @ {getver_text_addr:#x}")

        # Cherche cet adresse dans .rela.dyn
        needle     = pack("<Q", getver_text_addr)
        rela_match = rela_data.find(needle)
        if rela_match == -1:
            raise ExtractionError(
                f"Adresse {getver_text_addr:#x} (Platform_GetVersion) "
                "introuvable dans .rela.dyn."
            )
        # L'adresse est à l'offset +16 d'une entrée → l'entrée commence à -16
        rela_entry_offset = rela_match - 16
        if rela_entry_offset < 0 or rela_entry_offset % RELA_ENTRY != 0:
            raise ExtractionError(
                f"Offset rela incohérent : {rela_entry_offset} "
                f"(attendu multiple de {RELA_ENTRY})"
            )
        _dbg(f"Platform_GetVersion rela @ {rela_base + rela_entry_offset:#x}")

        # ── Remonte au début de la table des natives IO ─────────────────
        # On remonte 2 entrées à la fois (nom, adresse) jusqu'à trouver
        # "Crypto_GetRandomBytes"
        while rela_entry_offset >= RELA_ENTRY * 2:
            rela_entry_offset -= RELA_ENTRY * 2
            try:
                str_addr = unpack(
                    "<Q",
                    rela_data[rela_entry_offset + 16: rela_entry_offset + 24],
                )[0]
                name = _read_string(rodata, rodata_base, str_addr)
            except (ExtractionError, Exception) as e:
                _dbg(f"Remontée rela : {e}")
                break
            if name == "Crypto_GetRandomBytes":
                break

        # ── Parcours de la table des natives IO ─────────────────────────
        io_natives: dict[str, int] = {}
        max_entries = 200  # garde-fou contre une boucle infinie

        for _ in range(max_entries):
            if rela_entry_offset + RELA_ENTRY * 2 > len(rela_data):
                break

            try:
                str_addr = unpack(
                    "<Q",
                    rela_data[rela_entry_offset + 16: rela_entry_offset + 24],
                )[0]
                name = _read_string(rodata, rodata_base, str_addr)
            except ExtractionError as e:
                _dbg(f"Lecture nom : {e}")
                break

            rela_entry_offset += RELA_ENTRY

            try:
                fn_addr = unpack(
                    "<Q",
                    rela_data[rela_entry_offset + 16: rela_entry_offset + 24],
                )[0]
            except Exception:
                break

            rela_entry_offset += RELA_ENTRY
            io_natives[name] = fn_addr

            if name == "SystemEncodingToString":
                break
        else:
            raise ExtractionError(
                "Table des natives IO trop grande ou mal alignée "
                f"(arrêt après {max_entries} entrées)."
            )

        _dbg(f"io_natives trouvés : {list(io_natives.keys())[:5]}…")

        # ── Fonctions nécessaires présentes ? ───────────────────────────
        for required in ("Platform_GetVersion", "Stdout_GetTerminalSize"):
            if required not in io_natives:
                raise ExtractionError(
                    f"Native IO '{required}' manquante.\n"
                    f"  Natives trouvées : {', '.join(list(io_natives.keys())[:10])}"
                )

        # ── Lecture du code ARM64 ────────────────────────────────────────
        def _read_code(addr: int, size: int) -> bytes:
            text_offset = addr - text_hdr.sh_addr + text_hdr.sh_offset
            f.seek(text_offset)
            return f.read(size)

        md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
        dart_fns: dict[str, int] = {}

        # ── Platform_GetVersion → dart_version + Dart_NewStringFromCString ──
        fn_addr  = io_natives["Platform_GetVersion"]
        raw_code = _read_code(fn_addr, 48)
        code     = list(md.disasm_lite(raw_code, fn_addr))

        def _expect(idx: int, mnemonic: str):
            if idx >= len(code):
                raise ExtractionError(
                    f"Moins d'instructions que prévu dans Platform_GetVersion "
                    f"(attendu indice {idx}, seulement {len(code)} instructions)"
                )
            actual = code[idx][2]
            if actual != mnemonic:
                raise ExtractionError(
                    f"Platform_GetVersion instruction[{idx}] : "
                    f"attendu '{mnemonic}', trouvé '{actual}' @ {code[idx][0]:#x}"
                )

        _expect(0, "stp")
        _expect(1, "mov")
        _expect(2, "adrp")
        _expect(3, "add")
        _expect(4, "bl")
        _expect(8, "b")

        dart_version_page   = int(code[2][3][5:], 0)
        add_imm_str         = code[3][3]  # ex: "x0, x0, #0x1234"
        add_imm             = int(add_imm_str.split("#")[1], 0)
        dart_version_addr   = dart_version_page + add_imm
        dart_version        = _read_string(rodata, rodata_base, dart_version_addr)

        dart_fns["Dart_NewStringFromCString"] = int(code[4][3][1:], 0)
        dart_fns["Dart_SetReturnValue"]       = int(code[8][3][1:], 0)

        _dbg(f"dart_version={dart_version}")

        # ── Stdout_GetTerminalSize → Dart_NewList, Dart_NewInteger, Dart_ListSetAt ──
        fn_addr  = io_natives["Stdout_GetTerminalSize"]
        raw_code = _read_code(fn_addr, 0x100)

        # Cherche `mov w0, #2` (taille de la liste)
        LIST_SIZE_BYTES = b"\x40\x00\x80\x52"
        pos = raw_code.find(LIST_SIZE_BYTES)
        if pos == -1:
            raise ExtractionError(
                "Instruction 'mov w0, #2' introuvable dans Stdout_GetTerminalSize.\n"
                "  → La structure de cette fonction a peut-être changé."
            )
        pos += 4  # après le mov

        bl_count = 0
        bl_names = ["Dart_NewList", "Dart_NewInteger", "Dart_ListSetAt"]
        for _, _, mnemonic, op_str in md.disasm_lite(raw_code[pos:], fn_addr + pos):
            if mnemonic != "bl":
                continue
            if bl_count >= len(bl_names):
                break
            dart_fns[bl_names[bl_count]] = int(op_str[1:], 0)
            bl_count += 1

        if bl_count < len(bl_names):
            raise ExtractionError(
                f"Seulement {bl_count}/{len(bl_names)} fonctions extraites "
                "depuis Stdout_GetTerminalSize."
            )

    return dart_version, dart_fns


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extrait les adresses des fonctions Dart C API de libflutter.so"
    )
    parser.add_argument("libflutter", help="Chemin vers libflutter.so")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        os.environ["BLUTTER_VERBOSE"] = "1"
        global VERBOSE
        VERBOSE = True

    try:
        dart_version, dart_fns = extract_libflutter_functions(args.libflutter)
        print(f"Dart version : {dart_version}")
        for name, addr in dart_fns.items():
            print(f"  {name:<40} {addr:#x}")
    except (ExtractionError, FileNotFoundError, ImportError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
