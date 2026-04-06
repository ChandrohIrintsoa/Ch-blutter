# ─────────────────────────────────────────────────────────────
# Blutter — Image Docker
# Usage : docker build -t blutter .
#         docker/run.sh /path/to/app.apk /path/to/output
# ─────────────────────────────────────────────────────────────

# ── Stage 1 : dépendances système ─────────────────────────────
FROM python:3.12-slim AS base

# Groupe / utilisateur non-root (reproductible par UID fixe)
RUN groupadd -g 999 blutter && \
    useradd  -r -u 999 -g blutter blutter

# Dépendances système en une seule couche (cache Docker efficace)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        git \
        cmake \
        ninja-build \
        build-essential \
        pkg-config \
        libicu-dev \
        libcapstone-dev \
        libfmt-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2 : dépendances Python (couche séparée pour le cache) ──
FROM base AS python-deps

RUN python -m venv --copies /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir \
        pyelftools \
        requests

# ── Stage 3 : application finale ──────────────────────────────
FROM base AS final

# Copie du venv pré-installé
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dossier de travail
WORKDIR /app

# Copie des sources (les layers lourds — dartsdk, build, bin — sont
# dans .dockerignore et ne seront jamais copiés)
COPY --chown=blutter:blutter . .

# Répertoires de travail accessibles en écriture pour l'utilisateur blutter
RUN mkdir -p bin build packages dartsdk && \
    chown -R blutter:blutter /app

USER blutter

# Point d'entrée : les arguments sont transmis à blutter.py
ENTRYPOINT ["python", "blutter.py"]
CMD ["--help"]

# ── Labels ────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="Blutter"
LABEL org.opencontainers.image.description="Flutter Reverse Engineering Tool"
LABEL org.opencontainers.image.source="https://github.com/dedshit/blutter-termux"
