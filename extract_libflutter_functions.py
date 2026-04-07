#!/usr/bin/env python3
"""
extract_libflutter_functions.py — Extraction des adresses des fonctions Dart C API
depuis libflutter.so via désassemblage ARM64.
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


# ── Dépendances ───────────────────────────────────────────────────────────────

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
        missing.append("capstone    → pip install capstone")
    if not HAS_ELFTOOLS:
        missing.append("pyelftools  → pip install pyelftools")
    if missing:
        raise ImportError(
            "Dépendances manquantes :\n" +
            "\n".join(f"  • {m}" for m in missing)
        )


# ── Helpers ELF ───────────────────────────────────────────────────────────────

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
            f"Adresse {addr:#x} inférieure à la base .rodata {base_addr:#x}"
        )
    offset = addr - base_addr
    if offset >= len(data):
        raise ExtractionError(
            f"Adresse {addr:#x} hors limites .rodata (taille={len(data):#x})"
        )
    end = data.find(b"\x00", offset)
    if end == -1:
        raise ExtractionError(
            f"Chaîne non terminée à l'offset {offset:#x} dans .rodata"
        )
    return data[offset:end].decode("ascii", errors="replace")


# ── Extraction principale ─────────────────────────────────────────────────────

def extract_libflutter_functions(
    libflutter_file: str,
) -> tuple[str, dict[str, int]]:
    """
    Extrait la version Dart et les adresses des fonctions Dart C API.

    Retourne (dart_version, {nom_fonction: adresse}).
    Lève ExtractionError avec message précis en cas d'échec.
    """
    _check_deps()

    if not os.path.isfile(libflutter_file):
        raise FileNotFoundError(f"libflutter.so introuvable : {libflutter_file}")

    with open(libflutter_file, "rb") as f:
        try:
            elf = ELFFile(f)
        except Exception as e:
            raise ExtractionError(
                f"Impossible de parser {libflutter_file} : {e}"
            )

        rodata_sec = _require_section(elf, ".rodata")
        rela_sec   = _require_section(elf, ".rela.dyn")
        text_sec   = _require_section(elf, ".text")

        rodata    = rodata_sec.data()
        rela_data = rela_sec.data()
        rohdr     = rodata_sec.header
        text_hdr  = text_sec.header

        rodata_base = rohdr.sh_addr
        RELA_ENTRY  = 24  # taille d'une entrée .rela.dyn (ARM64)

        # ── Localise "Platform_GetVersion" dans .rodata ───────────────────
        marker = b"\x00Platform_GetVersion\x00"
        marker_pos = rodata.find(marker)
        if marker_pos == -1:
            raise ExtractionError(
                "Marqueur 'Platform_GetVersion' introuvable dans .rodata.\n"
                "  → Ce libflutter.so n'est pas un build standard Flutter."
            )
        getver_text_addr = rodata_base + marker_pos + 1  # +1 pour sauter le \x00 initial
        _dbg(f"Platform_GetVersion text @ {getver_text_addr:#x}")

        # Cherche cet adresse dans .rela.dyn
        needle     = pack("<Q", getver_text_addr)
        rela_match = rela_data.find(needle)
        if rela_match == -1:
            raise ExtractionError(
                f"Adresse {getver_text_addr:#x} (Platform_GetVersion) "
                "introuvable dans .rela.dyn."
            )

        # L'adresse est à l'offset +16 dans une entrée → l'entrée commence à -16
        rela_entry_offset = rela_match - 16
        if rela_entry_offset < 0:
            raise ExtractionError(
                f"Offset rela négatif : {rela_entry_offset}"
            )
        # Aligner sur RELA_ENTRY
        if rela_entry_offset % RELA_ENTRY != 0:
            # Cherche l'entrée alignée la plus proche
            rela_entry_offset = (rela_entry_offset // RELA_ENTRY) * RELA_ENTRY
        _dbg(f"Platform_GetVersion rela @ offset {rela_entry_offset:#x}")

        # ── Remonte au début de la table des natives IO ───────────────────
        # "Crypto_GetRandomBytes" est la première entrée de la table IO
        for _ in range(200):
            if rela_entry_offset < RELA_ENTRY * 2:
                break
            rela_entry_offset -= RELA_ENTRY * 2
            try:
                str_addr_bytes = rela_data[rela_entry_offset + 16: rela_entry_offset + 24]
                if len(str_addr_bytes) < 8:
                    break
                str_addr = unpack("<Q", str_addr_bytes)[0]
                name = _read_string(rodata, rodata_base, str_addr)
            except (ExtractionError, Exception) as e:
                _dbg(f"Remontée : {e}")
                rela_entry_offset += RELA_ENTRY * 2  # annule le recul
                break
            if name == "Crypto_GetRandomBytes":
                _dbg(f"Début table IO trouvé @ {rela_entry_offset:#x}")
                break

        # ── Parcours de la table des natives IO ───────────────────────────
        io_natives: dict[str, int] = {}
        max_entries = 300

        for _ in range(max_entries):
            if rela_entry_offset + RELA_ENTRY * 2 > len(rela_data):
                break

            try:
                str_addr_bytes = rela_data[rela_entry_offset + 16: rela_entry_offset + 24]
                if len(str_addr_bytes) < 8:
                    break
                str_addr = unpack("<Q", str_addr_bytes)[0]
                name = _read_string(rodata, rodata_base, str_addr)
            except ExtractionError as e:
                _dbg(f"Lecture nom @ {rela_entry_offset:#x} : {e}")
                break

            rela_entry_offset += RELA_ENTRY

            try:
                fn_addr_bytes = rela_data[rela_entry_offset + 16: rela_entry_offset + 24]
                if len(fn_addr_bytes) < 8:
                    break
                fn_addr = unpack("<Q", fn_addr_bytes)[0]
            except Exception as e:
                _dbg(f"Lecture adresse fn : {e}")
                break

            rela_entry_offset += RELA_ENTRY
            io_natives[name] = fn_addr

            if name == "SystemEncodingToString":
                break
        else:
            _dbg(f"Table IO arrêtée après {max_entries} entrées max.")

        _dbg(f"Natives IO trouvées : {len(io_natives)}  premiers={list(io_natives.keys())[:5]}")

        for required in ("Platform_GetVersion", "Stdout_GetTerminalSize"):
            if required not in io_natives:
                raise ExtractionError(
                    f"Native IO '{required}' manquante.\n"
                    f"  Natives trouvées ({len(io_natives)}) : "
                    f"{', '.join(list(io_natives.keys())[:10])}"
                )

        # ── Lecture du code ARM64 ─────────────────────────────────────────
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

        if len(code) < 9:
            raise ExtractionError(
                f"Platform_GetVersion : seulement {len(code)} instructions (attendu ≥ 9)"
            )

        def _expect(idx: int, mnemonic: str):
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

        dart_version_page = int(code[2][3][5:], 0)  # ex: "x0, #0x1234" → 0x1234
        add_imm_str       = code[3][3]               # ex: "x0, x0, #0x5678"
        add_imm           = int(add_imm_str.split("#")[1].rstrip("]"), 0)
        dart_version_addr = dart_version_page + add_imm
        dart_version      = _read_string(rodata, rodata_base, dart_version_addr)

        dart_fns["Dart_NewStringFromCString"] = int(code[4][3].lstrip("#").rstrip("]"), 0)
        dart_fns["Dart_SetReturnValue"]       = int(code[8][3].lstrip("#").rstrip("]"), 0)

        _dbg(f"dart_version={dart_version}")

        # ── Stdout_GetTerminalSize → Dart_NewList, Dart_NewInteger, Dart_ListSetAt ──
        fn_addr  = io_natives["Stdout_GetTerminalSize"]
        raw_code = _read_code(fn_addr, 0x100)

        LIST_SIZE_BYTES = b"\x40\x00\x80\x52"  # mov w0, #2
        pos = raw_code.find(LIST_SIZE_BYTES)
        if pos == -1:
            raise ExtractionError(
                "Instruction 'mov w0, #2' introuvable dans Stdout_GetTerminalSize."
            )
        pos += 4  # instruction après le mov

        bl_names = ["Dart_NewList", "Dart_NewInteger", "Dart_ListSetAt"]
        bl_found = []
        for _, _, mnemonic, op_str in md.disasm_lite(raw_code[pos:], fn_addr + pos):
            if mnemonic == "bl" and len(bl_found) < len(bl_names):
                try:
                    bl_found.append(int(op_str.lstrip("#"), 0))
                except ValueError:
                    pass
            if len(bl_found) >= len(bl_names):
                break

        if len(bl_found) < len(bl_names):
            raise ExtractionError(
                f"Seulement {len(bl_found)}/{len(bl_names)} fonctions extraites "
                "depuis Stdout_GetTerminalSize."
            )

        for name, addr in zip(bl_names, bl_found):
            dart_fns[name] = addr

    return dart_version, dart_fns


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extrait les adresses des fonctions Dart C API depuis libflutter.so"
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
