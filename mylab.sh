#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  mylab.sh  —  macOS / Linux
#  Usage :
#    ./mylab.sh           → lancer l'application
#    ./mylab.sh --install → créer le venv et installer les dépendances
#    ./mylab.sh --clean   → supprimer les logs et les groupes sauvegardés
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

# ── --install ─────────────────────────────────────────────────
cmd_install() {
    echo ""
    info "Création du virtualenv..."
    python3 -m venv "$VENV" || err "python3 introuvable"
    ok "Virtualenv créé : $VENV"

    info "Installation des dépendances..."
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
    ok "Dépendances installées"

    # dépendance système pywebview
    if [[ "$OSTYPE" == "darwin"* ]]; then
        info "macOS détecté — installation de pyobjc-framework-WebKit..."
        "$PYTHON" -m pip install --quiet pyobjc-framework-WebKit
        ok "pyobjc-framework-WebKit installé"
    else
        info "Linux détecté — si la fenêtre ne s'ouvre pas, installer : sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.0"
    fi

    echo ""
    ok "Installation terminée. Lancer l'app avec : ./mylab.sh"
}

# ── --clean ───────────────────────────────────────────────────
cmd_clean() {
    echo ""
    LOG_DIR="$SCRIPT_DIR/logs"
    GRP_DIR="$SCRIPT_DIR/groups"

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
    --install) cmd_install ;;
    --clean)   cmd_clean   ;;
    "")        cmd_run     ;;
    --traces)  cmd_run --traces ;;
    *)         echo "Usage : ./mylab.sh [--install | --clean | --traces]"; exit 1 ;;
esac
