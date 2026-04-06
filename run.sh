#!/usr/bin/env bash
# docker/run.sh
# ─────────────────────────────────────────────────────────────
# Lance l'analyse Blutter dans un conteneur Docker.
#
# Usage:
#   ./docker/run.sh <indir> <outdir> [options blutter...]
#
# Arguments:
#   indir    APK ou dossier contenant libapp.so + libflutter.so
#   outdir   Dossier de sortie (créé si absent)
#
# Options Blutter transmises:
#   --rebuild          Force recompilation
#   --no-analysis      Désactive l'analyse Dart
#   --ida-fcn          Noms de fonctions IDA
#   --dart-version V   Version Dart manuelle
#
# Options de ce script:
#   --image NAME       Nom de l'image Docker  [défaut: blutter:latest]
#   --build            Reconstruit l'image avant de lancer
#   --dry-run          Affiche la commande sans l'exécuter
#   -h, --help         Affiche cette aide

set -euo pipefail

# ── constantes ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
DEFAULT_IMAGE="blutter:latest"

# ── couleurs ──────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_GREEN='\033[92m'  C_CYAN='\033[96m'
    C_YELLOW='\033[93m' C_RED='\033[91m'
    C_RESET='\033[0m'   C_DIM='\033[2m'
else
    C_GREEN='' C_CYAN='' C_YELLOW='' C_RED='' C_RESET='' C_DIM=''
fi

ok()   { echo -e "${C_GREEN}  ✔  ${C_RESET}${1}"; }
info() { echo -e "${C_CYAN}  ◈  ${C_RESET}${1}"; }
warn() { echo -e "${C_YELLOW}  ⚠  ${C_RESET}${C_YELLOW}${1}${C_RESET}" >&2; }
err()  { echo -e "${C_RED}  ✘  ${C_RESET}${C_RED}${1}${C_RESET}" >&2; }

# ── aide ──────────────────────────────────────────────────────
usage() {
    sed -n 's/^# //p' "${BASH_SOURCE[0]}" | head -30
    exit 0
}

# ── parse args ────────────────────────────────────────────────
IMAGE_NAME="${DEFAULT_IMAGE}"
DO_BUILD=0
DRY_RUN=0
INDIR=""
OUTDIR=""
BLUTTER_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)    usage ;;
        --image)      IMAGE_NAME="$2"; shift 2 ;;
        --build)      DO_BUILD=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -*)
            # Options à passer à blutter.py
            BLUTTER_ARGS+=("$1")
            # Certaines options prennent un argument
            if [[ "$1" == "--dart-version" ]]; then
                BLUTTER_ARGS+=("$2"); shift
            fi
            shift
            ;;
        *)
            if [[ -z "${INDIR}" ]]; then
                INDIR="$1"
            elif [[ -z "${OUTDIR}" ]]; then
                OUTDIR="$1"
            else
                err "Argument inattendu : $1"
                exit 1
            fi
            shift
            ;;
    esac
done

# ── validation ────────────────────────────────────────────────
if [[ -z "${INDIR}" || -z "${OUTDIR}" ]]; then
    err "Arguments <indir> et <outdir> obligatoires."
    echo
    usage
fi

if ! command -v docker &>/dev/null; then
    err "Docker introuvable. Installez Docker : https://docs.docker.com/get-docker/"
    exit 1
fi

if ! docker info &>/dev/null 2>&1; then
    err "Le démon Docker n'est pas démarré (ou pas de permission)."
    err "Essayez : sudo systemctl start docker"
    exit 1
fi

# ── résolution des chemins ────────────────────────────────────
if ! INDIR="$(realpath "${INDIR}" 2>/dev/null)"; then
    err "Chemin introuvable : ${INDIR}"
    exit 1
fi

if [[ ! -e "${INDIR}" ]]; then
    err "Chemin introuvable : ${INDIR}"
    exit 1
fi

OUTDIR="$(realpath -m "${OUTDIR}")"
mkdir -p "${OUTDIR}"
chmod 777 "${OUTDIR}"

# ── build image si demandé ────────────────────────────────────
if [[ "${DO_BUILD}" -eq 1 ]]; then
    info "Construction de l'image Docker ${IMAGE_NAME}…"
    docker build -t "${IMAGE_NAME}" "${PROJECT_DIR}"
    ok "Image construite : ${IMAGE_NAME}"
fi

# ── vérification que l'image existe ──────────────────────────
if ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
    warn "Image '${IMAGE_NAME}' introuvable."
    echo -e "${C_CYAN}  → Construction automatique…${C_RESET}"
    docker build -t "${IMAGE_NAME}" "${PROJECT_DIR}"
    ok "Image construite : ${IMAGE_NAME}"
fi

# ── montage : APK ou dossier ──────────────────────────────────
if [[ -f "${INDIR}" ]]; then
    # APK → on monte le fichier directement
    INPUT_MOUNT="-v ${INDIR}:/app/input/$(basename "${INDIR}"):ro"
    CONTAINER_INPUT="/app/input/$(basename "${INDIR}")"
else
    # Dossier → on monte le dossier
    INPUT_MOUNT="-v ${INDIR}:/app/input:ro"
    CONTAINER_INPUT="/app/input"
fi

# ── commande Docker finale ────────────────────────────────────
DOCKER_CMD=(
    docker run --rm
    ${INPUT_MOUNT}
    -v "${OUTDIR}:/app/output"
    -w /app
    "${IMAGE_NAME}"
    "${CONTAINER_INPUT}"
    /app/output
    "${BLUTTER_ARGS[@]+"${BLUTTER_ARGS[@]}"}"
)

# ── affichage récap ───────────────────────────────────────────
info "Image  : ${IMAGE_NAME}"
info "Input  : ${INDIR}"
info "Output : ${OUTDIR}"
if [[ ${#BLUTTER_ARGS[@]} -gt 0 ]]; then
    info "Options Blutter : ${BLUTTER_ARGS[*]}"
fi

# ── dry-run ───────────────────────────────────────────────────
if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "Commande Docker (dry-run) :"
    echo -e "  ${C_DIM}${DOCKER_CMD[*]}${C_RESET}"
    exit 0
fi

echo
info "Lancement de l'analyse…"
echo

# ── exécution ─────────────────────────────────────────────────
if "${DOCKER_CMD[@]}"; then
    echo
    ok "Analyse terminée — résultats dans : ${OUTDIR}"
else
    echo
    err "L'analyse a échoué (code $?)."
    err "Consultez les logs ci-dessus pour le détail."
    exit 1
fi
