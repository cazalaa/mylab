# My Lab

Application de gestion de boards Silicon Labs (SDM + Commander).

---

## Prérequis

- **Python 3.10+**
- **Simplicity Device Manager (SDM)** — fourni par Silabs SLT (Simplicity Studio ou standalone)
- **Simplicity Commander** — fourni par Simplicity Commander

> My Lab détecte automatiquement les deux binaires dans `~/.silabs/`. Si ce n'est pas le cas, renseigner les chemins manuellement dans `config.ini`.

---

## Installation

### 1. Décompresser l'archive

```bash
unzip Archive_clean.zip
cd projet
```

### 2. Créer un environnement virtuel (recommandé)

```bash
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 3. Installer les dépendances Python

```bash
pip install -r requirements.txt
```

Dépendances installées : `flask` · `flask-socketio` · `pywebview` · `pyserial` · `requests` · `pyyaml`

### 4. Dépendance système pywebview

**macOS**
```bash
pip install pyobjc-framework-WebKit
```

**Windows**
```bash
pip install pywebview[winforms]
```

---

## Configuration

Éditer `config.ini` si nécessaire :

```ini
[paths]
sdm       =   # laisser vide pour auto-détection dans ~/.silabs
commander =   # laisser vide pour auto-détection dans ~/.silabs

[server]
host     = 127.0.0.1
port     = 3129   # port SDM (ne pas modifier)
web_port = 8080   # port interne Flask
```

---

## Lancement

Les scripts `mylab.sh` (macOS/Linux) et `mylab.bat` (Windows) gèrent l'installation, le nettoyage et le démarrage.

### macOS / Linux

```bash
# Rendre le script exécutable (une seule fois)
chmod +x mylab.sh

# Installer le venv et les dépendances
./mylab.sh --install

# Lancer l'application
./mylab.sh

# Nettoyer les logs et groupes sauvegardés
./mylab.sh --clean
```

### Windows

```bat
:: Installer le venv et les dépendances
mylab.bat --install

:: Lancer l'application
mylab.bat

:: Nettoyer les logs et groupes sauvegardés
mylab.bat --clean
```

Une fenêtre native s'ouvre. My Lab démarre SDM automatiquement s'il n'est pas déjà actif.

---

## Structure du projet

```
projet/
├── my_lab.py            # application principale
├── mylab.sh             # script macOS / Linux (install, run, clean)
├── mylab.bat            # script Windows      (install, run, clean)
├── config.ini           # configuration chemins et ports
├── requirements.txt     # dépendances Python
├── groups/              # groupes d'adapters sauvegardés (*.group)
├── static/              # CSS, JS (xterm, socket.io)
├── templates/           # pages HTML (Flask)
│   ├── index.html
│   ├── maintenance.html
│   ├── manual_control.html
│   ├── script_control.html
│   └── terminal.html
├── scenari/             # scénarios YAML + scripts Python
│   └── railtest/
└── logs/                # logs générés à l'exécution
```
