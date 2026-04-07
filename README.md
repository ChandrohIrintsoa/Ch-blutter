# Ch-blutter

**Flutter Reverse Engineering** — ARM64/ARM32 · Dart VM · Natif sur Termux (Android)

---

## Fonctionnalités

- ✅ **Détection automatique intelligente** des `.so` cibles par analyse ELF
- ✅ **Menu de sélection** quand plusieurs `.so` sont présents
- ✅ Mode TUI interactif (menu clair avec navigation fichiers)
- ✅ Mode CLI direct pour scripts/CI
- ✅ Extraction automatique depuis APK (scan de tous les `.so`)
- ✅ Détection automatique de la version Dart
- ✅ Historique des analyses (`~/.chblutter_history`)
- ✅ Vérification des dépendances (`--check-deps`)
- ✅ Compatible Docker (si disponible)
- ✅ Support ARM64 / ARM32 / x86_64

---

## Détection intelligente des `.so`

Ch-blutter **ne cherche plus uniquement `libapp.so` et `libflutter.so`** par nom.
Il analyse chaque fichier `.so` trouvé et identifie :

- **Dart app** (équivalent libapp) : présence des symboles ELF
  `_kDartVmSnapshotData`, `_kDartIsolateSnapshotData`, etc.
- **Flutter engine** (équivalent libflutter) : présence de `Platform_GetVersion`,
  des engine IDs SHA-1 dans `.rodata`, etc.

Si **plusieurs candidats** sont trouvés pour le `.so` Dart app, un menu interactif
vous propose de choisir. Utilisez `--auto` pour désactiver ce menu.

---

## Installation rapide

### Sur Termux (Android) — recommandé

```bash
# 1. Cloner ce dépôt
git clone https://github.com/dedshit/Ch-blutter.git
cd Ch-blutter

# 2. Lancer le script d'installation automatique
chmod +x setup_termux.sh
./setup_termux.sh
```

Le script installe : `cmake`, `ninja`, `clang`, `pkg-config`, `libicu`,
`capstone`, `fmt`, `pyelftools`, `requests`.

### Sur Linux (Debian/Ubuntu)

```bash
sudo apt install -y git cmake ninja-build clang pkg-config \
    libicu-dev libcapstone-dev libfmt-dev python3 python3-pip
pip3 install -r requirements.txt
git clone https://github.com/dedshit/Ch-blutter.git && cd Ch-blutter
```

### Via Docker

```bash
docker build -t blutter .
./run.sh app.apk ./out
```

---

## Usage

### Mode TUI interactif (recommandé)

```bash
python blutter.py
```

Lance un menu interactif avec navigation fichiers, choix des options et
résumé avant l'analyse.

### Mode CLI direct

```bash
# Depuis un APK
python blutter.py app.apk ./out

# Depuis un dossier de libs (noms quelconques)
python blutter.py ./libs/arm64-v8a ./out

# Avec sélection automatique (pas de menu)
python blutter.py app.apk ./out --auto

# Avec options
python blutter.py app.apk ./out --rebuild
python blutter.py app.apk ./out --dart-version 3.4.2_android_arm64
python blutter.py app.apk ./out --no-analysis --ida-fcn
```

### Via run.sh (détection automatique Docker / natif)

```bash
./run.sh app.apk ./out
./run.sh app.apk ./out --native     # Force mode natif
./run.sh app.apk ./out --docker     # Force mode Docker
./run.sh app.apk ./out --dry-run    # Affiche la commande sans exécuter
```

---

## Options

| Option | Description |
|---|---|
| `--dart-version VER` | Version Dart manuelle (ex: `3.4.2_android_arm64`) |
| `--rebuild` | Force la recompilation de l'exécutable blutter |
| `--no-analysis` | Désactive l'analyse Dart (plus rapide) |
| `--ida-fcn` | Génère les noms de fonctions pour IDA Pro |
| `--auto` | Sélection automatique des `.so` (pas de menu) |
| `--no-compressed-ptrs` | Désactive la compression des pointeurs Dart |
| `--no-update` | Ne pas vérifier les mises à jour git |
| `--check-deps` | Vérifie les dépendances et quitte |
| `--history` | Affiche l'historique des analyses |
| `--debug` | Affiche les tracebacks complets |

---

## Résultats produits

Après une analyse réussie, le dossier `<outdir>` contient :

| Fichier/Dossier | Description |
|---|---|
| `asm/` | Code assembleur Dart (un fichier par classe) |
| `blutter_frida.js` | Script Frida généré automatiquement |
| `objs.txt` | Liste des objets Dart |
| `pp.txt` | Pretty-print du code Dart |

---

## Vérifier les dépendances

```bash
python blutter.py --check-deps
```

Vérifie : cmake, ninja, clang/gcc, pkg-config, libicu, capstone, fmt,
pyelftools, requests, Python ≥ 3.9.

---

## Structure du dépôt

```
Ch-blutter/
├── blutter.py                      # Point d'entrée principal (TUI + CLI)
├── run.sh                          # Runner universel (Docker ou natif)
├── setup_termux.sh                 # Installateur Termux
├── requirements.txt                # Dépendances Python
├── Dockerfile                      # Image Docker multi-stage
├── dartvm_fetch_build.py           # Téléchargement et build Dart VM lib
├── dartvm_create_srclist.py        # Génération de la liste des sources Dart
├── dartvm_make_version.py          # Gestion des versions Dart VM
├── extract_dart_info.py            # Extraction des métadonnées Dart depuis .so
├── extract_libflutter_functions.py # Extraction des fonctions libflutter
├── generate_thread_offsets_cpp.py  # Génération des offsets de threads C++
├── init_env_win.py                 # Initialisation environnement Windows
└── blutter/                        # Sources C++ blutter (submodule)
    └── CMakeLists.txt
```

---

## Dépendances

### Système
- `git`, `cmake`, `ninja` (ou `ninja-build`)
- `clang` (ou `gcc`)
- `pkg-config`, `libicu`, `libcapstone`, `libfmt`

### Python (≥ 3.9)
- `pyelftools` — lecture des fichiers ELF
- `requests` — téléchargement des sources Dart VM
- `rich` — TUI amélioré (optionnel)
- `capstone` — désassemblage ARM64 (optionnel)
