#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  mylab.sh  —  macOS / Linux
#  Usage :
#    ./mylab.sh              → lancer l'application (fenêtre native)
#    ./mylab.sh --install    → créer/réparer le venv et installer les dépendances
#    ./mylab.sh --clean      → supprimer les logs et les groupes sauvegardés
#    ./mylab.sh --headless   → lancer sans fenêtre (serveur web seul, ex: Rpi Ubuntu Server)
#    ./mylab.sh --remote     → alias de --headless
#    ./mylab.sh --traces     → lancer avec logs verbeux
# ─────────────────────────────────────────────────────────────

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PYTHON="$VENV/bin/python"

# ── couleurs ──────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ $*${NC}"; }
info() { echo -e "${YELLOW}→ $*${NC}"; }
err()  { echo -e "${RED}✗ $*${NC}"; exit 1; }

have_cmd() { command -v "$1" >/dev/null 2>&1; }

find_jlink() {
    command -v JLinkExe 2>/dev/null ||
        find /opt/SEGGER /usr/local/SEGGER -type f -name JLinkExe \
            2>/dev/null | head -n1
}

install_jlink() {
    local installer="$1"
    [[ -f "$installer" ]] || err "J-Link installer not found: $installer"

    info "Installing SEGGER J-Link (sudo password may be requested)..."
    case "$installer" in
        *.deb)
            sudo dpkg -i "$installer" || sudo apt-get -f install -y
            ;;
        *.tgz|*.tar.gz)
            local tmp install_script
            tmp="$(mktemp -d)"
            tar -xf "$installer" -C "$tmp"
            install_script="$(find "$tmp" -type f -name install.sh -print -quit)"
            [[ -n "$install_script" ]] || err "No install.sh found in $installer"
            sudo bash "$install_script"
            rm -rf "$tmp"
            ;;
        *)
            err "Supported J-Link installers: .deb, .tgz, .tar.gz"
            ;;
    esac
}

ensure_jlink() {
    local jlink
    jlink="$(find_jlink || true)"

    [[ -n "$jlink" ]] || err \
        "SEGGER J-Link is required. Download its Linux ARM/AArch64 installer, then run: ./mylab.sh --install --jlink-installer /path/to/installer"

    "$jlink" -version >/dev/null 2>&1 ||
        err "SEGGER J-Link was found but cannot run: $jlink"

    ok "SEGGER J-Link ready: $jlink"
}

select_python() {
    if have_cmd python3.11; then
        echo "python3.11"
    elif have_cmd python3; then
        echo "python3"
    else
        err "python3 introuvable. Installer Python 3.10+ ou 3.11+."
    fi
}

# ── --install ─────────────────────────────────────────────────
cmd_install() {
    echo ""
    local SYS_PYTHON
    SYS_PYTHON="$(select_python)"

    if [[ -n "${JLINK_INSTALLER:-}" ]]; then
        install_jlink "$JLINK_INSTALLER"
    fi
    ensure_jlink

    if [[ -d "$VENV" ]]; then
        info "Suppression du virtualenv existant pour repartir proprement..."
        rm -rf "$VENV"
    fi

    info "Création du virtualenv avec $SYS_PYTHON..."
    "$SYS_PYTHON" -m venv "$VENV" || err "Impossible de créer le virtualenv"
    ok "Virtualenv créé : $VENV"

    info "Mise à jour de pip/setuptools/wheel..."
    "$PYTHON" -m pip install --quiet --upgrade pip setuptools wheel

    info "Installing required Ubuntu runtime libraries..."
    sudo apt-get update
    sudo apt-get install -y \
        ca-certificates \
        libpcre2-16-0 \
        libusb-1.0-0 \
        libudev1
    ok "Ubuntu runtime libraries ready"

    info "Installation des dépendances Python..."
    "$PYTHON" -m pip install --quiet --upgrade -r "$SCRIPT_DIR/requirements.txt"
    ok "Dépendances installées"

    if ! command -v JLinkExe >/dev/null 2>&1; then
        err "SEGGER J-Link is missing. Install the Linux ARM64 J-Link Software Pack, then rerun ./mylab.sh --install"
    fi

    JLinkExe -version >/dev/null ||
        err "SEGGER J-Link is installed but cannot load its shared library"

    if [[ -f /opt/SEGGER/JLink/99-jlink.rules ]]; then
        sudo install -m 644 /opt/SEGGER/JLink/99-jlink.rules \
            /etc/udev/rules.d/99-jlink.rules
        sudo udevadm control --reload-rules
        sudo udevadm trigger
    fi
    
    "$PYTHON" - <<'PY'
    from pycommander_cli import Commander
    
    commander = Commander()
    usb = commander.listAvailableAdapters(list_usb_adapters=True)
    net = commander.listAvailableAdapters(list_network_adapters=True)
    print(f"PyCommander OK — USB: {len(usb)}, network: {len(net)}")
    PY
    ok "PyCommander USB and network discovery verified"

    # Backend GUI pour pywebview.
    # Le dashboard reste HTML/CSS/JS/Plotly; ces paquets servent seulement
    # à ouvrir la fenêtre desktop pywebview selon l'OS.
    if [[ "${OSTYPE:-}" == "darwin"* ]]; then
        info "macOS détecté — installation/réparation complète de PyObjC..."
        "$PYTHON" -m pip uninstall -y pyobjc pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz pyobjc-framework-WebKit objc >/dev/null 2>&1 || true
        "$PYTHON" -m pip install --quiet --no-cache-dir --force-reinstall \
            pyobjc-core \
            pyobjc-framework-Cocoa \
            pyobjc-framework-Quartz \
            pyobjc-framework-WebKit

        info "Vérification PyObjC..."
        "$PYTHON" - <<'PY'        
        import objc
        import Foundation
        import AppKit
        import WebKit
        print("PyObjC OK")
        PY
        ok "Backend macOS prêt"
    elif [[ "${OSTYPE:-}" == "linux"* ]]; then
        info "Linux détecté — utilisation du backend GTK/WebKit système pour pywebview."
        info "Si la fenêtre ne s'ouvre pas, installer les paquets système suivants :"
        info "  Debian/Ubuntu : sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
        info "  Anciennes Ubuntu : sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.0"
        info "  Fedora : sudo dnf install python3-gobject gtk3 webkit2gtk4.1"
    else
        info "OS non reconnu par OSTYPE=$OSTYPE — dépendances Python installées."
    fi

    echo ""
    ok "Installation terminée. Lancer l'app avec : ./mylab.sh"
}

# ── --clean ───────────────────────────────────────────────────
cmd_clean() {
    echo ""
    LOG_DIR="$SCRIPT_DIR/logs"
    GRP_DIR="$SCRIPT_DIR/groups"

    find "$SCRIPT_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    find "$SCRIPT_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) -delete 2>/dev/null || true
    ok "Caches Python supprimés"

    if [[ -d "$LOG_DIR" ]] && compgen -G "$LOG_DIR/*.log" > /dev/null 2>&1; then
        rm -f "$LOG_DIR"/*.log
        ok "Logs supprimés ($LOG_DIR)"
    else
        info "Aucun log à supprimer"
    fi

    if [[ -d "$GRP_DIR" ]] && compgen -G "$GRP_DIR/*.group" > /dev/null 2>&1; then
        read -rp "Supprimer les groupes sauvegardés ? (o/N) " confirm
        if [[ "$confirm" =~ ^[oOyY]$ ]]; then
            rm -f "$GRP_DIR"/*.group
            ok "Groupes supprimés ($GRP_DIR)"
        else
            info "Groupes conservés"
        fi
    else
        info "Aucun groupe à supprimer"
    fi
    echo ""
}

# ── run (défaut) ──────────────────────────────────────────────
cmd_run() {
    if [[ ! -f "$PYTHON" ]]; then
        err "Virtualenv introuvable. Lancer d'abord : ./mylab.sh --install"
    fi
    echo ""
    info "Démarrage de My Lab..."
    cd "$SCRIPT_DIR"
    exec "$PYTHON" my_lab.py "$@"
}

# ── dispatch ──────────────────────────────────────────────────
case "${1:-}" in
    --install)
        JLINK_INSTALLER=""
        if [[ "${2:-}" == "--jlink-installer" ]]; then
            JLINK_INSTALLER="${3:-}"
        elif [[ "${2:-}" == --jlink-installer=* ]]; then
            JLINK_INSTALLER="${2#--jlink-installer=}"
        fi
        cmd_install
        ;;
    --clean)            cmd_clean   ;;
    "")                 cmd_run     ;;
    --traces)           cmd_run --traces ;;
    --headless)         cmd_run --headless ;;
    --remote)           cmd_run --remote ;;
    --headless-traces)  cmd_run --headless --traces ;;
    --remote-traces)    cmd_run --remote --traces ;;
    *)                  echo "Usage : ./mylab.sh [--install | --clean | --headless | --remote | --traces]"; exit 1 ;;
esac
