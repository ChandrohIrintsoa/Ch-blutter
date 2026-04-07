#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_termux.sh — Installation complète de blutter-termux sur Termux (Android)
#
# Ce script installe TOUTES les dépendances système et Python requises,
# configure l'environnement et vérifie l'installation.
#
# Usage :
#   chmod +x setup_termux.sh
#   ./setup_termux.sh
#
# Options :
#   --skip-pkg     Ignore l'installation des paquets système (déjà installés)
#   --skip-pip     Ignore l'installation des modules Python
#   --skip-clone   Ignore le clonage du sous-dépôt blutter (déjà présent)
#   --help         Affiche cette aide
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Couleurs ─────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_GRN='\033[92m' C_CYN='\033[96m' C_YLW='\033[93m'
    C_RED='\033[91m' C_DIM='\033[2m'  C_B='\033[1m' C_R='\033[0m'
else
    C_GRN='' C_CYN='' C_YLW='' C_RED='' C_DIM='' C_B='' C_R=''
fi

ok()   { echo -e "${C_GRN}  ✔  ${C_R}${1}"; }
info() { echo -e "${C_CYN}  ◈  ${C_R}${1}"; }
warn() { echo -e "${C_YLW}  ⚠  ${C_R}${C_YLW}${1}${C_R}" >&2; }
err()  { echo -e "${C_RED}  ✘  ${C_R}${C_RED}${1}${C_R}" >&2; exit 1; }
step() { echo -e "\n${C_B}${C_CYN}▸ ${1}${C_R}"; }

# ── Bannière ─────────────────────────────────────────────────────────────────
echo -e "${C_CYN}"
cat <<'EOF'
  ╔══════════════════════════════════════════════════════╗
  ║         blutter-termux  ·  Setup Termux              ║
  ║   Flutter Reverse Engineering — Natif sur Android    ║
  ╚══════════════════════════════════════════════════════╝
EOF
echo -e "${C_R}"

# ── Vérification Termux ───────────────────────────────────────────────────────
if [[ -z "${PREFIX:-}" ]] || [[ ! -d "${PREFIX}" ]]; then
    warn "Ce script est optimisé pour Termux (Android)."
    warn "Sur Linux/macOS, utilisez votre gestionnaire de paquets natif."
    read -r -p "  Continuer quand même ? [o/N] : " yn
    [[ "${yn,,}" =~ ^(o|oui|y|yes)$ ]] || exit 0
fi

# ── Parse args ────────────────────────────────────────────────────────────────
SKIP_PKG=0 SKIP_PIP=0 SKIP_CLONE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-pkg)   SKIP_PKG=1;   shift ;;
        --skip-pip)   SKIP_PIP=1;   shift ;;
        --skip-clone) SKIP_CLONE=1; shift ;;
        --help|-h)
            sed -n 's/^# //p' "${BASH_SOURCE[0]}" | head -20
            exit 0
            ;;
        *) warn "Option inconnue : $1"; shift ;;
    esac
done

# ═════════════════════════════════════════════════════════════════════════════
# Étape 1 — Paquets système via pkg
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${SKIP_PKG}" -eq 0 ]]; then
    step "Installation des paquets système (pkg)"

    # Mise à jour des dépôts
    info "Mise à jour des dépôts pkg…"
    pkg update -y 2>/dev/null || warn "pkg update a eu des avertissements (ignorés)"

    # Liste des paquets requis
    PKG_REQUIRED=(
        git
        cmake
        ninja
        clang
        binutils
        pkg-config
        python
    )

    # Bibliothèques natives pour le build C++
    PKG_LIBS=(
        libicu
        capstone
        fmt
    )

    info "Installation des outils de build…"
    pkg install -y "${PKG_REQUIRED[@]}" || err "Échec pkg install (outils)"

    info "Installation des bibliothèques natives…"
    pkg install -y "${PKG_LIBS[@]}" || {
        warn "Certaines libs n'ont pas pu être installées."
        warn "Essayez manuellement : pkg install libicu capstone fmt"
    }

    ok "Paquets système installés."
else
    info "Installation paquets système ignorée (--skip-pkg)."
fi

# ═════════════════════════════════════════════════════════════════════════════
# Étape 2 — Modules Python via pip
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${SKIP_PIP}" -eq 0 ]]; then
    step "Installation des modules Python"

    # Vérification Python
    if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
        err "python3 introuvable. Installez-le : pkg install python"
    fi

    PY_CMD="$(command -v python3 2>/dev/null || command -v python)"
    PY_VER="$(${PY_CMD} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    info "Python détecté : ${PY_VER}  (${PY_CMD})"

    # Mise à jour pip
    info "Mise à jour pip…"
    ${PY_CMD} -m pip install --upgrade pip --quiet 2>/dev/null || true

    # Installation des dépendances depuis requirements.txt
    if [[ -f "${SCRIPT_DIR}/requirements.txt" ]]; then
        info "Installation depuis requirements.txt…"
        ${PY_CMD} -m pip install -r "${SCRIPT_DIR}/requirements.txt" --quiet
    else
        info "requirements.txt absent — installation directe…"
        ${PY_CMD} -m pip install pyelftools requests --quiet
    fi

    ok "Modules Python installés."
else
    info "Installation modules Python ignorée (--skip-pip)."
fi

# ═════════════════════════════════════════════════════════════════════════════
# Étape 3 — Clonage du sous-dépôt blutter (sources C++)
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${SKIP_CLONE}" -eq 0 ]]; then
    step "Vérification du sous-dépôt blutter (sources C++)"
    BLUTTER_DIR="${SCRIPT_DIR}/blutter"

    if [[ -d "${BLUTTER_DIR}/.git" ]] || [[ -f "${BLUTTER_DIR}/CMakeLists.txt" ]]; then
        ok "Sources C++ déjà présentes dans ${BLUTTER_DIR}/"
    elif [[ -f "${SCRIPT_DIR}/.gitmodules" ]]; then
        info "Initialisation des submodules git…"
        git -C "${SCRIPT_DIR}" submodule update --init --recursive
        ok "Submodules initialisés."
    else
        info "Clonage du dépôt blutter principal…"
        git clone --depth=1 https://github.com/worawit/blutter.git "${BLUTTER_DIR}" || \
            warn "Clonage échoué — vérifiez votre connexion et clonez manuellement :"
            warn "  git clone https://github.com/worawit/blutter.git blutter"
    fi
else
    info "Clonage blutter ignoré (--skip-clone)."
fi

# ═════════════════════════════════════════════════════════════════════════════
# Étape 4 — Création des répertoires de travail
# ═════════════════════════════════════════════════════════════════════════════
step "Création des répertoires de travail"

for dir in bin build packages dartsdk; do
    full="${SCRIPT_DIR}/${dir}"
    if [[ ! -d "${full}" ]]; then
        mkdir -p "${full}"
        ok "Créé : ${dir}/"
    else
        info "Existant : ${dir}/"
    fi
done

# ═════════════════════════════════════════════════════════════════════════════
# Étape 5 — Vérification finale via blutter.py --check-deps
# ═════════════════════════════════════════════════════════════════════════════
step "Vérification finale des dépendances"

PY_CMD="$(command -v python3 2>/dev/null || command -v python)"
if [[ -f "${SCRIPT_DIR}/blutter.py" ]]; then
    ${PY_CMD} "${SCRIPT_DIR}/blutter.py" --check-deps || true
else
    warn "blutter.py introuvable dans ${SCRIPT_DIR} — vérification ignorée."
fi

# ── Résumé ────────────────────────────────────────────────────────────────────
echo
echo -e "${C_GRN}${C_B}  ✔  Installation terminée !${C_R}"
echo
echo -e "  ${C_CYN}Usage :${C_R}"
echo -e "    ${C_DIM}python blutter.py                      # TUI interactif${C_R}"
echo -e "    ${C_DIM}python blutter.py app.apk ./out        # CLI direct${C_R}"
echo -e "    ${C_DIM}python blutter.py --check-deps         # Vérifier les dépendances${C_R}"
echo
