#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — Lance blutter (Docker ou natif Termux/Linux)
#
# Usage :
#   ./run.sh <indir> <outdir> [options blutter...]
#
# Arguments :
#   indir    APK ou dossier contenant libapp.so + libflutter.so
#   outdir   Dossier de sortie (créé si absent)
#
# Options blutter transmises :
#   --rebuild          Force recompilation
#   --no-analysis      Désactive l'analyse Dart
#   --ida-fcn          Noms de fonctions IDA
#   --dart-version V   Version Dart manuelle (ex: 3.4.2_android_arm64)
#
# Options de ce script :
#   --native           Force le mode natif (python blutter.py) même si Docker dispo
#   --docker           Force le mode Docker
#   --image NAME       Nom de l'image Docker  [défaut: blutter:latest]
#   --build            Reconstruit l'image Docker avant de lancer
#   --dry-run          Affiche la commande sans l'exécuter
#   -h, --help         Affiche cette aide
#
# Détection automatique :
#   • Termux détecté  → mode natif par défaut
#   • Docker dispo    → mode Docker par défaut
#   • Python seul     → mode natif
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_IMAGE="blutter:latest"

if [[ -t 1 ]]; then
    C_GRN='\033[92m' C_CYN='\033[96m' C_YLW='\033[93m'
    C_RED='\033[91m' C_DIM='\033[2m'  C_R='\033[0m'
else
    C_GRN='' C_CYN='' C_YLW='' C_RED='' C_DIM='' C_R=''
fi

ok()   { echo -e "${C_GRN}  ✔  ${C_R}${1}"; }
info() { echo -e "${C_CYN}  ◈  ${C_R}${1}"; }
warn() { echo -e "${C_YLW}  ⚠  ${C_R}${C_YLW}${1}${C_R}" >&2; }
err()  { echo -e "${C_RED}  ✘  ${C_R}${C_RED}${1}${C_R}" >&2; exit 1; }

_is_termux() {
    [[ -n "${TERMUX_VERSION:-}" ]] ||
    [[ -d "/data/data/com.termux" ]] ||
    [[ "${PREFIX:-}" == *"com.termux"* ]]
}
_has_docker() { command -v docker &>/dev/null && docker info &>/dev/null 2>&1; }
_has_python()  { command -v python3 &>/dev/null || command -v python &>/dev/null; }
_python_cmd()  { command -v python3 2>/dev/null || command -v python 2>/dev/null || echo ""; }

IMAGE_NAME="${DEFAULT_IMAGE}"
DO_BUILD=0 DRY_RUN=0 FORCE_NATIVE=0 FORCE_DOCKER=0
INDIR="" OUTDIR=""
BLUTTER_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)    sed -n 's/^# //p' "${BASH_SOURCE[0]}" | head -35; exit 0 ;;
        --native)     FORCE_NATIVE=1; shift ;;
        --docker)     FORCE_DOCKER=1; shift ;;
        --image)      IMAGE_NAME="$2"; shift 2 ;;
        --build)      DO_BUILD=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --dart-version) BLUTTER_ARGS+=("$1" "$2"); shift 2 ;;
        --rebuild|--no-analysis|--ida-fcn|--no-update|--debug)
                      BLUTTER_ARGS+=("$1"); shift ;;
        -*)           warn "Option inconnue : $1"; BLUTTER_ARGS+=("$1"); shift ;;
        *)
            if   [[ -z "${INDIR}" ]];  then INDIR="$1"
            elif [[ -z "${OUTDIR}" ]]; then OUTDIR="$1"
            else err "Argument inattendu : $1"; fi
            shift ;;
    esac
done

[[ -n "${INDIR}" && -n "${OUTDIR}" ]] || \
    err "Usage : ./run.sh <indir> <outdir> [options]"

INDIR="$(realpath "${INDIR}" 2>/dev/null)" || err "Chemin introuvable : ${INDIR}"
[[ -e "${INDIR}" ]] || err "Chemin introuvable : ${INDIR}"
OUTDIR="$(realpath -m "${OUTDIR}")"
mkdir -p "${OUTDIR}"

# ── Sélection du mode ─────────────────────────────────────────────────────────
if   [[ "${FORCE_NATIVE}" -eq 1 ]]; then RUN_MODE="native"
elif [[ "${FORCE_DOCKER}" -eq 1 ]]; then RUN_MODE="docker"
elif _is_termux;   then RUN_MODE="native"; info "Termux détecté → mode natif"
elif _has_docker;  then RUN_MODE="docker"; info "Docker détecté → mode Docker"
elif _has_python;  then RUN_MODE="native"; info "Python détecté → mode natif"
else err "Ni Docker ni Python trouvés.\n  Sur Termux : ./setup_termux.sh\n  Sur Linux  : voir README.md"; fi

# ═════════════════════════════════════════════════════════════════════════════
# MODE NATIF
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${RUN_MODE}" == "native" ]]; then
    PY="$(_python_cmd)"
    [[ -n "${PY}" ]] || err "python introuvable. Sur Termux : pkg install python"
    BLUTTER_PY="${SCRIPT_DIR}/blutter.py"
    [[ -f "${BLUTTER_PY}" ]] || err "blutter.py introuvable dans : ${SCRIPT_DIR}"

    CMD=("${PY}" "${BLUTTER_PY}" "${INDIR}" "${OUTDIR}")
    [[ ${#BLUTTER_ARGS[@]} -gt 0 ]] && CMD+=("${BLUTTER_ARGS[@]}")

    info "Mode   : natif (${PY})"
    info "Input  : ${INDIR}"
    info "Output : ${OUTDIR}"
    [[ ${#BLUTTER_ARGS[@]} -gt 0 ]] && info "Options: ${BLUTTER_ARGS[*]}"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "Commande (dry-run):"; echo -e "  ${C_DIM}${CMD[*]}${C_R}"; exit 0
    fi
    echo; info "Lancement…"; echo
    if "${CMD[@]}"; then echo; ok "Terminé — résultats : ${OUTDIR}"
    else echo; err "Analyse échouée. Voir logs ci-dessus."; fi
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
# MODE DOCKER
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${RUN_MODE}" == "docker" ]]; then
    command -v docker &>/dev/null || err "Docker introuvable : https://docs.docker.com/get-docker/"
    docker info &>/dev/null 2>&1  || err "Démon Docker non démarré : sudo systemctl start docker"

    if [[ "${DO_BUILD}" -eq 1 ]]; then
        info "Build image Docker ${IMAGE_NAME}…"
        docker build -t "${IMAGE_NAME}" "${SCRIPT_DIR}" && ok "Image construite."
    elif ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
        warn "Image '${IMAGE_NAME}' absente — build automatique…"
        docker build -t "${IMAGE_NAME}" "${SCRIPT_DIR}" && ok "Image construite."
    fi

    if [[ -f "${INDIR}" ]]; then
        INPUT_MOUNT="-v ${INDIR}:/app/input/$(basename "${INDIR}"):ro"
        CONTAINER_INPUT="/app/input/$(basename "${INDIR}")"
    else
        INPUT_MOUNT="-v ${INDIR}:/app/input:ro"
        CONTAINER_INPUT="/app/input"
    fi
    chmod 777 "${OUTDIR}"

    DOCKER_CMD=(docker run --rm ${INPUT_MOUNT}
        -v "${OUTDIR}:/app/output" -w /app "${IMAGE_NAME}"
        "${CONTAINER_INPUT}" /app/output)
    [[ ${#BLUTTER_ARGS[@]} -gt 0 ]] && DOCKER_CMD+=("${BLUTTER_ARGS[@]}")

    info "Mode   : Docker (${IMAGE_NAME})"
    info "Input  : ${INDIR}"
    info "Output : ${OUTDIR}"
    [[ ${#BLUTTER_ARGS[@]} -gt 0 ]] && info "Options: ${BLUTTER_ARGS[*]}"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "Commande Docker (dry-run):"; echo -e "  ${C_DIM}${DOCKER_CMD[*]}${C_R}"; exit 0
    fi
    echo; info "Lancement…"; echo
    if "${DOCKER_CMD[@]}"; then echo; ok "Terminé — résultats : ${OUTDIR}"
    else echo; err "Analyse échouée (Docker). Voir logs ci-dessus."; fi
    exit 0
fi
