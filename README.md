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

## Mode headless / remote (Rpi, Ubuntu Server, machine sans écran)

Sur une machine sans affichage (typiquement un Raspberry Pi sous Ubuntu Server), lancer
l'app avec `--headless` (ou `--remote`, alias identique) : aucune fenêtre pywebview n'est
ouverte, seul le serveur Flask/SocketIO tourne, accessible depuis le navigateur d'une
autre machine du réseau.

```bash
./mylab.sh --headless
# ou
./mylab.sh --remote
# avec logs verbeux :
./mylab.sh --headless-traces
```

Le terminal affiche l'URL d'accès au démarrage, par ex. `http://192.168.1.42:8080`.
Ouvrir cette adresse depuis n'importe quel navigateur du réseau local.

**Pré-requis config.ini** : `host` doit être `0.0.0.0` (déjà le cas par défaut) sinon le
serveur n'écoute qu'en local et n'est pas joignable depuis une autre machine :

```ini
[server]
host = 0.0.0.0
web_port = 8080
```

**Sélecteur de fichiers** : en fenêtre native, pywebview ouvre le sélecteur de fichiers du
système. Sans fenêtre (mode headless, ou même simplement un onglet de navigateur classique),
il n'y a pas de dialogue natif possible — l'app bascule automatiquement sur un envoi de
fichier via le navigateur (upload HTTP vers `/api/upload`), utilisé pour :
- flasher un firmware (bouton **Flash** dans Manual Control),
- ouvrir/ajouter un script ou un fichier `.s37` dans un scénario (Scenario Control),
- charger un script à exécuter depuis un Terminal.

Les fichiers envoyés sont stockés sous `uploads/<kind>/` (créé automatiquement) et le
chemin serveur obtenu est ensuite utilisé exactement comme un chemin choisi nativement.

> Limite connue : le choix d'un **dossier** (`pick_directory`, utilisé pour changer le
> dossier de travail des scénarios) n'a pas d'équivalent navigateur — en mode headless,
> l'app demande le chemin du dossier via une invite texte (le dossier doit donc déjà
> exister sur le Rpi).

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
├── static/              # CSS, JS (xterm, socket.io, upload-fallback)
├── templates/           # pages HTML (Flask)
│   ├── index.html
│   ├── maintenance.html
│   ├── manual_control.html
│   ├── script_control.html
│   └── terminal.html
├── scenari/             # scénarios YAML + scripts Python
│   └── railtest/
├── uploads/             # fichiers envoyés via le navigateur (mode headless/remote)
└── logs/                # logs générés à l'exécution
```
