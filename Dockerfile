# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — Ch-blutter
# Build multi-stage : dépendances système + Python + sources C++
#
# Usage :
#   docker build -t chblutter .
#   docker run --rm -v /path/to/app.apk:/app/input/app.apk:ro \
#                   -v /path/to/out:/app/output \
#                   chblutter /app/input/app.apk /app/output
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1 : base système ───────────────────────────────────────────────────
FROM ubuntu:22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Outils de build C++
    build-essential \
    cmake \
    ninja-build \
    clang \
    lld \
    pkg-config \
    # Bibliothèques Dart VM
    libicu-dev \
    libcapstone-dev \
    libfmt-dev \
    # Python
    python3 \
    python3-pip \
    python3-dev \
    # Git (clone SDK Dart)
    git \
    git-lfs \
    # Utilitaires
    curl \
    unzip \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2 : dépendances Python ─────────────────────────────────────────────
FROM base AS python-deps

COPY requirements.txt /tmp/requirements.txt

RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /tmp/requirements.txt

# ── Stage 3 : image finale ───────────────────────────────────────────────────
FROM python-deps AS final

WORKDIR /app

# Copier les scripts Python
COPY blutter.py                     ./
COPY dartvm_fetch_build.py          ./
COPY dartvm_create_srclist.py       ./
COPY dartvm_make_version.py         ./
COPY extract_dart_info.py           ./
COPY extract_libflutter_functions.py ./
COPY generate_thread_offsets_cpp.py ./
COPY requirements.txt               ./

# Copier les sources C++ blutter (si présentes en local)
# Si absentes, elles seront clonées au premier lancement
COPY blutter/ ./blutter/

# Répertoires de travail
RUN mkdir -p bin build packages dartsdk

# Variables d'environnement pour le build C++
ENV CMAKE=cmake \
    NINJA=ninja \
    CC=clang \
    CXX=clang++

# Vérification des dépendances au build
RUN python3 blutter.py --check-deps 2>&1 || true

# ── Point d'entrée ────────────────────────────────────────────────────────────
ENTRYPOINT ["python3", "/app/blutter.py"]

# Utilisation :
#   docker run --rm \
#     -v /host/app.apk:/app/input/app.apk:ro \
#     -v /host/out:/app/output \
#     chblutter /app/input/app.apk /app/output
#
# Options disponibles :
#   --auto              (sélection automatique des .so — recommandé en CI)
#   --rebuild           (force recompilation)
#   --dart-version VER  (version manuelle)
#   --no-analysis
#   --ida-fcn
CMD ["--help"]
